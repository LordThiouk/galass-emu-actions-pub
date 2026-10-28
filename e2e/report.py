#!/usr/bin/env python3
"""Strict E2E verdict for the Galass staging harness.

Input  : $OUT/results-raw.txt (point|result|detail, written by run.sh)
         $OUT/screens/*.json   (one sidecar per capture)
Output : results.txt (aggregated, one row per point), results-raw.txt,
         report.json, summary.md

Rules
  * worst-wins per point: FAIL > SKIP > WARN > PASS
  * a P0 point that never ran is FAIL "not attempted (harness)"
  * a P0 UI point with no MATCH capture is FAIL, even if a row claimed PASS
  * a capture with no usable PNG, an empty-file hash, or the wrong foreground
    app is INVALID and cannot close a P0 point
  * a capture whose dump repeats an earlier capture is FAIL "duplicate-state"
    (the stale-dump false positive) unless it is the explicit revisited pair
  * B2 needs two distinct Driver captures (offer vs in-ride), else FAIL
"""
import argparse
import glob
import hashlib
import json
import os
import re
import sys
import time

RANK = {"PASS": 1, "WARN": 2, "SKIP": 3, "FAIL": 4}
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
RIDER_PKG = "sn.galass.galassrider"
DRIVER_PKG = "sn.galass.galassdriver"
# A screen the run legitimately visits twice (start and end of the same flow).
REVISITED = {("rider-home-loaded", "rider-home-after")}
B2_POINTS = ("driver-offer-with-stops", "driver-inride-stops")

# Declared here and mirrored verbatim in run.sh (test_p0_default_matches_run_sh).
P0_DEFAULT = """api-preflight apk-base-url emulator-boot apk-install
rider-home-loaded rider-destination-selected rider-stop-picker rider-multistop-summary
rider-driver-assigned rider-running-start rider-stop1-active rider-stop1-done
rider-stop2-done rider-destination-arrived rider-cash-methods rider-cash-waiting
rider-final-itinerary rider-rating rider-home-after
driver-offer-with-stops driver-accepted-details driver-pickup-arrived driver-running-stop1
driver-stop1-done driver-stop2-done driver-cash-request driver-cash-confirmed driver-summary
backend-flow no-prod-calls""".split()

# Every P0 point whose truth claim rests on a screenshot.
UI_POINTS = {
    "rider-home-loaded", "rider-destination-selected", "rider-stop-picker",
    "rider-multistop-summary", "rider-driver-assigned", "rider-running-start",
    "rider-stop1-active", "rider-stop1-done", "rider-stop2-done",
    "rider-destination-arrived", "rider-cash-methods", "rider-cash-waiting",
    "rider-final-itinerary", "rider-rating", "rider-home-after",
    "driver-offer-with-stops", "driver-accepted-details", "driver-pickup-arrived",
    "driver-running-stop1", "driver-stop1-done", "driver-stop2-done",
    "driver-cash-request", "driver-cash-confirmed", "driver-summary",
}


def parse_rows(text):
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) < 2:
            continue
        point, result = parts[0].strip(), parts[1].strip().upper()
        detail = parts[2] if len(parts) > 2 else ""
        if result not in RANK:
            result, detail = "FAIL", f"unknown result {result!r}: {detail}"
        rows.append({"point": point, "result": result, "detail": detail})
    return rows


def aggregate(rows):
    """Collapse rows to one per point, worst-wins, first-seen order."""
    out, index = [], {}
    for row in rows:
        point = row["point"]
        if point not in index:
            index[point] = len(out)
            out.append(dict(row))
            continue
        cur = out[index[point]]
        if RANK[row["result"]] > RANK[cur["result"]]:
            cur["result"], cur["detail"] = row["result"], row["detail"]
    return out


def read_sidecars(out_dir):
    caps = []
    for path in sorted(glob.glob(os.path.join(out_dir, "screens", "*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                cap = json.load(fh)
        except (OSError, ValueError):
            continue
        cap.setdefault("name", os.path.basename(path)[:-5])
        cap.setdefault("status", "UNKNOWN")
        cap.setdefault("marker", "")
        cap.setdefault("dump_sha256", "")
        cap.setdefault("labels", [])
        cap.setdefault("node_count", 0)
        caps.append(cap)
    return caps


def expected_pkg(point):
    if str(point).startswith("rider-"):
        return RIDER_PKG
    if str(point).startswith("driver-"):
        return DRIVER_PKG
    return ""


def validate_captures(out_dir, caps):
    """B4: a sidecar is only evidence when the pixels exist and the app is the right one."""
    for cap in caps:
        reasons = []
        png = os.path.join(out_dir, "screens", f"{cap.get('name')}.png")
        try:
            size = os.path.getsize(png)
        except OSError:
            size = -1
        if size < 0:
            reasons.append("png missing")
        elif size < 8:
            reasons.append(f"png {size}B < 8B")
        if (cap.get("png_sha256") or "") in ("", EMPTY_SHA256):
            reasons.append("png_sha256 empty")
        want = expected_pkg(cap.get("point") or "")
        fg = cap.get("fg_pkg") or ""
        if want and fg not in ("", "UNKNOWN", want):
            reasons.append(f"foreground {fg} != {want}")
        if reasons:
            cap["invalid_reason"] = "; ".join(reasons)
            cap["status"] = "INVALID"
    return caps


def _norm_label(label):
    """Normalise a visible label, masking digits so a ticking timer cannot hide a copy."""
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", str(label))).strip().lower()


def _revisited(prev_point, point):
    return (prev_point or "", point or "") in REVISITED


def check_distinct(sidecars):
    """B5: same pixels (or same app + same labels + same node count) is a lie.

    A '-' clause is a reason to expect different labels, never a licence to reuse
    the same pixels; only the explicit revisited-screen pair is exempt.
    """
    duplicates, seen, sig_seen = [], {}, {}
    for cap in sidecars:
        if cap.get("status") != "MATCH":
            continue
        point = cap.get("point") or ""
        digest = cap.get("dump_sha256") or ""
        if digest and digest in seen:
            prev_name, prev_point = seen[digest]
            if not _revisited(prev_point, point):
                duplicates.append({"name": cap["name"], "point": point,
                                   "duplicate_of": prev_name, "signal": "dump"})
        if digest:
            seen.setdefault(digest, (cap["name"], point))
        labels = [l for l in (cap.get("labels") or []) if str(l).strip()]
        if labels:
            sig = (cap.get("fg_pkg") or "",
                   tuple(sorted({_norm_label(l) for l in labels})),
                   int(cap.get("node_count") or 0))
            if sig in sig_seen:
                prev_name, prev_point = sig_seen[sig]
                if (not _revisited(prev_point, point)
                        and not any(d["name"] == cap["name"] for d in duplicates)):
                    duplicates.append({"name": cap["name"], "point": point,
                                       "duplicate_of": prev_name, "signal": "labels"})
            sig_seen.setdefault(sig, (cap["name"], point))
    return duplicates


def check_b2(caps):
    """S3: the two B2 captures must be provably different screens of the same ride."""
    by_point = {}
    for cap in caps:
        by_point.setdefault(cap.get("point") or "", []).append(cap)
    if not any(by_point.get(p) for p in B2_POINTS):
        return None
    offer = (by_point.get(B2_POINTS[0]) or [None])[0]
    ride = (by_point.get(B2_POINTS[1]) or [None])[0]
    problems = []
    for point, cap in zip(B2_POINTS, (offer, ride)):
        if cap is None:
            problems.append(f"{point} missing")
        elif cap.get("status") != "MATCH":
            problems.append(f"{point} {cap.get('status')}")
    if offer and ride:
        if offer.get("dump_sha256") and offer.get("dump_sha256") == ride.get("dump_sha256"):
            problems.append("same dump")
        if (offer.get("png_sha256") or "") and offer.get("png_sha256") == ride.get("png_sha256"):
            problems.append("same png")
    if ride and "-refuser" not in str(ride.get("marker", "")).lower():
        problems.append("in-ride marker lacks -REFUSER")
    return problems


def _upsert(points, point, result, detail):
    for row in points:
        if row["point"] == point:
            if RANK[result] >= RANK[row["result"]]:
                row["result"], row["detail"] = result, detail
            return
    points.append({"point": point, "result": result, "detail": detail})


# Artemis ids are model names: an allowlist, never a free-form string. A poisoned
# row must not be able to inject anything into report.json or summary.md.
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,80}$")
# Artemis writes free-ish text into model-usage.txt / attempts.txt. Everything
# that reaches report.json or summary.md goes through one of these allowlists:
# a poisoned row must not be able to inject markdown/HTML into the verdict.
SERVED_VALUES = ("primary", "fallback", "none", "unknown")
# `budget` and `latency_floor` are the two "no attempt ran" outcomes: run.sh
# refuses the attempt and records why, instead of burning a certain timeout.
OUTCOME_VALUES = ("success", "retryable", "terminal", "budget", "latency_floor")
# `%OUT/artemis/vision-floor.txt`: one measured floor per live route (run.sh
# vision_floor_map). Strict shape: a poisoned line contributes no floor at all.
FLOOR_ROW_RE = re.compile(r"^model=([A-Za-z0-9._:/-]{1,80}) floor=([0-9]{1,6})$")
# run.sh `log()` prefixes every line with `[e2e HH:MM:SS] `, so these are searched
# (not matched) and only ever yield a run name + a bounded integer.
_CONFIG_BOUND_RE = re.compile(
    r"artemis config: (\S+) .*request_timeout=([0-9]{1,6})s")
_REFUSAL_BOUND_RE = re.compile(
    r"artemis timeout: (\S+) timeout par requête ([0-9]{1,6})s")


def _model_id(value):
    value = (value or "").strip()
    return value if MODEL_ID_RE.match(value) else None


def _safe_token(value, fallback="-"):
    """Allowlisted token for a run/attempt name or a signal, else `fallback`."""
    token = _model_id(value)
    return token if token is not None else fallback


def _signals(value):
    """Allowlisted comma-joined signal list (`429,timeout`), token by token.

    run.sh joins every retry signal with ','; a poisoned token drops alone
    instead of blanking the whole (measured) list.
    """
    tokens = [t for t in (_model_id(x) for x in (value or "").split(",")) if t]
    return ",".join(tokens) if tokens else "-"


def _enum(value, allowed, fallback="unknown"):
    """Strict enum for a field whose value set is known."""
    token = (value or "").strip().lower()
    return token if token in allowed else fallback


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _read_pipe_rows(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return [line.rstrip("\n") for line in fh if line.strip()]
    except OSError:
        return []


def _read_floors(out_dir):
    """Measured latency floor per model, from artemis/vision-floor.txt only."""
    floors = {}
    for row in _read_pipe_rows(os.path.join(out_dir, "artemis", "vision-floor.txt")):
        m = FLOOR_ROW_RE.match(row.strip())
        if m:
            floors[m.group(1)] = int(m.group(2))
    return floors


def _read_run_bounds(out_dir):
    """Funded per-request bound per run, read from $OUT/run.log.

    Nothing is recomputed: run.sh logs the bound it actually configured
    (`artemis config: … request_timeout=Ns`) or the one it compared against the
    floor (`artemis timeout: … timeout par requête Ns`). Last line wins, so a
    refusal followed by a funded retry reports the retry's real bound.
    """
    bounds = {}
    try:
        with open(os.path.join(out_dir, "run.log"), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return bounds
    for line in lines:
        m = _CONFIG_BOUND_RE.search(line) or _REFUSAL_BOUND_RE.search(line)
        if m:
            bounds[_safe_token(m.group(1))] = int(m.group(2))
    return bounds


def run_floor(run, artemis):
    """`(model, floor)` that explains this run, from the measured map only.

    Priority: the served model, then the run's attempts (last first), then the
    chain it was given. A model absent from vision-floor.txt has no measurement,
    so nothing is printed for it — the report never invents a floor.
    """
    floors = artemis["floors"]
    candidates = []
    if run["served_model"] != "unknown":
        candidates.append(run["served_model"])
    candidates += [a["model"] for a in reversed(artemis["attempts"])
                   if a["name"] == run["name"] and a["model"] != "-"]
    candidates += run["chain"]
    for model in candidates:
        if model in floors:
            return model, floors[model]
    return None


def read_artemis(out_dir):
    """Artemis audit trail: which model really served, with which budget.

    `model-usage.txt` is one row per run (legacy rows have no `served_model`);
    `attempts.txt` is one row per attempt. Both are rebuilt every run, so a
    missing file simply means Artemis never ran.
    """
    adir = os.path.join(out_dir, "artemis")
    runs = []
    for row in _read_pipe_rows(os.path.join(adir, "model-usage.txt")):
        fields = row.split("|")
        if len(fields) < 3:
            continue
        data = {"name": _safe_token(fields[0]), "attempts": _int(fields[1]),
                "max_attempts": _int(fields[2])}
        kv = {}
        for field in fields[3:]:
            if "=" in field:
                key, _, value = field.partition("=")
                kv[key] = value
        served_model = _model_id(kv.get("served_model"))
        if served_model is None:
            # legacy row: derive honestly, never assume a model ran
            if kv.get("rc") not in ("0", None):
                served_model = "unknown"
            elif kv.get("fallback_events", "0") not in ("0", ""):
                served_model = _model_id(kv.get("fallback"))
            else:
                served_model = _model_id(kv.get("primary"))
        runs.append({
            "name": data["name"],
            "attempts": data["attempts"],
            "max_attempts": data["max_attempts"],
            "served": _enum(kv.get("served"), SERVED_VALUES),
            "served_model": served_model or "unknown",
            "rc": _int(kv.get("rc")),
            "signals": _signals(kv.get("signals")),
            "chain": [m for m in (_model_id(x) for x in (kv.get("chain") or "").split(",")) if m],
        })

    attempts = []
    budget_exhausted = []
    latency_floor_refused = []
    for row in _read_pipe_rows(os.path.join(adir, "attempts.txt")):
        fields = row.split("|")
        if len(fields) != 7:
            continue
        name, attempt, model, rc, timeout_sec, signal, outcome = fields
        safe_name = _safe_token(name)
        attempts.append({"name": safe_name, "attempt": _int(attempt),
                         "model": _model_id(model) or "-", "rc": _int(rc),
                         "timeout_sec": _int(timeout_sec),
                         "signal": _safe_token(signal, "-"),
                         "outcome": _enum(outcome, OUTCOME_VALUES)})
        if outcome == "budget" and safe_name not in budget_exhausted:
            budget_exhausted.append(safe_name)
        # Same family, different cause: the route was measured slower than the
        # per-attempt budget, so the run refused to launch a doomed attempt.
        if outcome == "latency_floor" and safe_name not in latency_floor_refused:
            latency_floor_refused.append(safe_name)

    chain = [m for m in (_model_id(x) for x in
                         _read_pipe_rows(os.path.join(adir, "vision-chain.txt"))) if m]
    # ISC-6: run.sh writes the chain it ASKED for at preflight, before probing.
    # Reporting only the survivors makes a rate-limited model look unrequested.
    # `chain` is the probe result and stays separate: without the requested file
    # nothing was asked for, so the summary must not claim a `chaîne demandée`.
    requested_path = os.path.join(adir, "vision-chain-requested.txt")
    requested_chain = [m for m in (_model_id(x) for x in _read_pipe_rows(requested_path))
                       if m]
    return {"runs": runs, "attempts": attempts, "chain": chain,
            "requested_chain": requested_chain,
            "budget_exhausted": budget_exhausted,
            "latency_floor_refused": latency_floor_refused,
            "floors": _read_floors(out_dir),
            "bounds": _read_run_bounds(out_dir)}


def build(out_dir, p0=None):
    p0 = [p for p in (p0 if p0 is not None else P0_DEFAULT)]
    if not p0:
        # S1: a run that declares no P0 points proves nothing, and must not pass.
        p0 = list(P0_DEFAULT)
    p0_set = set(p0)
    raw_path = os.path.join(out_dir, "results-raw.txt")
    try:
        with open(raw_path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        raw = ""

    rows = parse_rows(raw)
    seen = {r["point"] for r in rows}
    points = aggregate(rows)
    # every unexecuted P0 point is a FAIL, never a silence
    for point in p0:
        if point not in seen:
            _upsert(points, point, "FAIL", "not attempted (harness)")

    sidecars = validate_captures(out_dir, read_sidecars(out_dir))
    for cap in sidecars:
        if cap.get("status") == "INVALID" and cap.get("point"):
            _upsert(points, cap["point"], "FAIL",
                    f"capture invalid: {cap.get('name')} ({cap.get('invalid_reason')})")
    by_point = {}
    for cap in sidecars:
        by_point.setdefault(cap.get("point") or "", []).append(cap)

    # a P0 UI claim needs a MATCH capture, whatever the row says
    for point in p0:
        if point not in UI_POINTS:
            continue
        caps = by_point.get(point, [])
        if not caps:
            _upsert(points, point, "FAIL", "not attempted (harness)")
            continue
        match = [c for c in caps if c.get("status") == "MATCH"]
        if not match:
            invalid = [c for c in caps if c.get("status") == "INVALID"]
            if invalid:
                _upsert(points, point, "FAIL",
                        "capture invalid: "
                        + "; ".join(f"{c['name']} ({c.get('invalid_reason')})"
                                    for c in invalid))
            else:
                names = ",".join(f"{c['name']}({c.get('status')})" for c in caps)
                _upsert(points, point, "FAIL", f"no MATCH capture: {names}")

    duplicates = check_distinct(sidecars)
    for dup in duplicates:
        if dup["point"]:
            _upsert(points, dup["point"], "FAIL",
                    f"duplicate-state: {dup.get('signal', 'dump')} identical to "
                    f"{dup['duplicate_of']}")

    b2 = check_b2(sidecars)
    if b2 is not None:
        if b2:
            _upsert(points, "b2-captures", "FAIL", "; ".join(b2))
        else:
            _upsert(points, "b2-captures", "PASS",
                    "offre et course: captures distinctes, -REFUSER sur la course")

    # accept-via PASS is only credible with a real credit. `unknown` means the ride
    # was ours but the tap was never proven: the flow may continue, the claim may not.
    for row in points:
        if row["point"] != "accept-via" or row["result"] != "PASS":
            continue
        match = re.search(r"accept_via=([A-Za-z0-9_-]+)", row.get("detail") or "")
        via = match.group(1).lower() if match else ""
        if via not in ("api", "ui"):
            row["result"] = "WARN"
            row["detail"] = (
                f"{row['detail']} [crédit non prouvé: accept_via={via or 'absent'}]"
            ).strip()

    fails = [p for p in points if p["result"] == "FAIL"]
    warns = [p for p in points if p["result"] == "WARN"]
    skips = [p for p in points if p["result"] == "SKIP"]
    p0_rows = [p for p in points if p["point"] in p0_set]
    p0_nonpass = [p["point"] for p in p0_rows if p["result"] != "PASS"]
    verdict = ("FAIL" if fails or p0_nonpass or len(p0) == 0
               else ("PASS_WITH_WARNINGS" if warns or skips else "PASS"))

    flags = {}
    flags_path = os.path.join(out_dir, "ui-flags.txt")
    if os.path.isfile(flags_path):
        for line in open(flags_path, encoding="utf-8"):
            if "=" in line:
                key, _, value = line.strip().partition("=")
                flags[key] = value

    report = {
        "verdict": verdict,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "points": points,
        "fail_count": len(fails),
        "warn_count": len(warns),
        "skip_count": len(skips),
        "p0_total": len(p0),
        "p0_pass": sum(1 for p in p0_rows if p["result"] == "PASS"),
        "p0_nonpass": p0_nonpass,
        "ui_flags": flags,
        "captures": [{k: c.get(k) for k in
                      ("name", "point", "status", "marker", "marker_source",
                       "fg_pkg", "dump_sha256", "png_sha256", "failed_spec",
                       "node_count", "invalid_reason")}
                     for c in sidecars],
        "duplicates": duplicates,
        "artemis": read_artemis(out_dir),
    }

    if not os.path.isfile(raw_path):
        open(raw_path, "w", encoding="utf-8").close()
    with open(os.path.join(out_dir, "results.txt"), "w", encoding="utf-8") as fh:
        for row in points:
            fh.write(f"{row['point']}|{row['result']}|{row['detail']}\n")
    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    lines = [
        "# E2E staging report",
        "",
        f"Verdict: **{verdict}**",
        "",
        f"P0: {report['p0_pass']}/{report['p0_total']} PASS — "
        f"non-PASS: {', '.join(p0_nonpass) or 'none'}",
        "",
        "| point | result | detail |",
        "|---|---|---|",
    ]
    lines += [f"| {p['point']} | {p['result']} | {p['detail']} |" for p in points]
    artemis = report["artemis"]
    if artemis["runs"] or artemis["attempts"] or artemis["chain"] \
            or artemis["requested_chain"]:
        lines += ["", "## Artemis", ""]
        if artemis["requested_chain"]:
            lines += [f"- chaîne demandée: {', '.join(artemis['requested_chain'])}"]
        if artemis["chain"]:
            lines += [f"- chaîne: {', '.join(artemis['chain'])}"]
        for run in artemis["runs"]:
            lines += [f"- {run['name']}: served_model={run['served_model']} "
                      f"attempts={run['attempts']}/{run['max_attempts']} "
                      f"signals={run['signals']} rc={run['rc']}"]
            # Max M2: a run that DID attempt and still reads served=unknown can
            # look causeless. Next to it, the floor that was measured and the
            # bound that funded the attempt tell the reader why it timed out.
            # Each half prints only when a file actually carries it.
            measured = run_floor(run, artemis)
            bound = artemis["bounds"].get(run["name"])
            facts = []
            if measured is not None:
                facts.append(f"latence mesurée {measured[1]}s ({measured[0]})")
            if bound is not None:
                facts.append(f"borne {bound}s")
            if facts:
                lines += [f"  - {', '.join(facts)}"]
        if artemis["budget_exhausted"]:
            # Factual only: the budget stopped the step, which can happen AFTER a
            # real attempt ran. "(aucune tentative)" asserted zero attempts that
            # the `attempts=`/`rc=` lines above contradict.
            lines += [f"- budget épuisé: {', '.join(artemis['budget_exhausted'])}"]
        if artemis["latency_floor_refused"]:
            # Name the REFUSED ROUTES (3rd ledger field, parsed into the attempt
            # rows), never the step: "(2 route(s) trop lente(s): smoke)" named a
            # step and zero routes. The count is the distinct refused routes and
            # makes no claim about how many attempts ran.
            routes = []
            for a in artemis["attempts"]:
                if a["outcome"] == "latency_floor" and a["model"] != "-" \
                        and a["model"] not in routes:
                    routes.append(a["model"])
            if routes:
                lines += [f"- plancher de latence refusé ({len(routes)}): "
                          f"{', '.join(routes)}"]
            else:
                # Field 3 carried no route name: publish the count only. Falling
                # back to the step names put step names on a route-naming line
                # (Max m2).
                lines += [f"- plancher de latence refusé "
                          f"({len(artemis['latency_floor_refused'])})"]
    if duplicates:
        lines += ["", "## Duplicate captures", ""]
        lines += [f"- {d['name']} repeats {d['duplicate_of']}" for d in duplicates]
    if flags:
        lines += ["", "## UI flags", ""]
        lines += [f"- {k}={v}" for k, v in sorted(flags.items())]
    with open(os.path.join(out_dir, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Strict verdict for the Galass E2E harness")
    parser.add_argument("out_dir")
    parser.add_argument("--p0", default=None,
                        help="comma/space separated P0 points (defaults to P0_DEFAULT)")
    args = parser.parse_args(argv)
    p0 = None
    if args.p0:
        p0 = [p for p in args.p0.replace(",", " ").split() if p]
    report = build(args.out_dir, p0=p0)
    print(f"verdict={report['verdict']} fails={report['fail_count']} "
          f"warns={report['warn_count']} skips={report['skip_count']} "
          f"p0={report['p0_pass']}/{report['p0_total']} "
          f"nonpass={','.join(report['p0_nonpass']) or 'none'}")
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
