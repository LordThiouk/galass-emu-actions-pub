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

# INCONCLUSIVE = l'émulateur a planté pendant le point (jamais un FAIL de l'app). Pour un
# point P0 c'est un non-PASS : il fait échouer le verdict comme un FAIL, sans être imputé à l'app.
RANK = {"PASS": 1, "WARN": 2, "INCONCLUSIVE": 3, "SKIP": 4, "FAIL": 5}
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
RIDER_PKG = "sn.galass.galassrider"
DRIVER_PKG = "sn.galass.galassdriver"
# A screen the run legitimately visits twice (start and end of the same flow), or
# a deliberately re-timed capture of the same UI state whose evidence is non-XML.
REVISITED = {
    ("rider-home-loaded", "rider-home-after"),
    # Même écran RUNNING jambe 1 par design : 21 = départ, 22 = carte horodatée
    # en cours de jambe 1. Les pins/polyline ne sont pas visibles en XML (seule la
    # carte/PNG les porte, preuve pixel archivée GPG) et les marqueurs textuels sont
    # identiques ; l'état XML peut donc être le même sans invalider la capture 22.
    ("rider-running-start", "rider-stop1-active"),
}
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


# Une capture ne peut être "invalidée" que si elle prétend être une preuve. Une capture
# TIMEOUT/ACTION_MISS n'est déjà pas une preuve : sa raison doit dire ce qui a échoué
# (le marqueur, le tap), pas "foreground … != package" — c'était le faux positif du run
# 44354825315, où six points P0 semblaient casser le harnais alors que l'app était à l'écran.
EVIDENCE_STATUS = "MATCH"


def capture_reason(cap):
    """Short, true reason for a capture that is not evidence."""
    status = str(cap.get("status") or "UNKNOWN")
    parts = [status]
    if status == "ACTION_MISS" and cap.get("action") and not cap.get("action_ok"):
        parts.append(f"tap « {cap.get('action')} » manqué")
    failed = str(cap.get("failed_spec") or "").strip()
    if failed:
        parts.append(f"marqueur absent: {failed}")
    fg = str(cap.get("fg_pkg") or "").strip()
    if fg and fg not in ("", "UNKNOWN"):
        parts.append(f"fg={fg} ({cap.get('fg_source') or 'dumpsys'})")
    return f"{cap.get('name')} ({' ; '.join(parts)})"


def validate_captures(out_dir, caps):
    """B4: a sidecar is only evidence when the pixels exist and the app is the right one.

    Applied to evidence-grade captures (MATCH). A capture that never claimed the screen
    keeps its own status and is reported through `capture_reason`, so a FAIL always names
    the step that failed.
    """
    for cap in caps:
        if cap.get("status") != EVIDENCE_STATUS:
            continue
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


# Unité de temps : le seul jeton dont les chiffres tichent tout seuls.
_TIME_UNIT = (r"(?:min|mins|minute|minutes|s|sec|secs|seconde|secondes"
              r"|h|heure|heures|j|jour|jours)")


def _norm_label(label):
    """Normalise un libellé pour comparer deux écrans.

    Seuls les jetons qui tichent sont masqués (durée, horloge) : un minuteur qui avance ne
    doit pas masquer une copie d'écran. Un compteur de progression reste entier.

    Mesure run 44441344957 : masquer *tous* les chiffres faisait passer « 1/2 arrêt
    terminé » pour une copie de « 0/2 arrêt terminé » alors que les pixels diffèrent
    (driver-stop1-done en duplicate-state, png=diff). Un compteur qui change est un état
    qui change.
    """
    text = re.sub(r"\s+", " ", str(label)).strip().lower()
    text = re.sub(r"\b\d+(?=\s*" + _TIME_UNIT + r"\b)", "#", text)
    text = re.sub(r"\b\d{1,2}:\d{2}\b", "#:#", text)
    return text


def _revisited(prev_point, point):
    return (prev_point or "", point or "") in REVISITED


def _png_verdict(prev_png, cap):
    """Les pixels tranchent : même dump + pixels différents = dump périmé, pas même écran.

    Mesure run 44412970992 : quatre points tombaient en « duplicate-state » sans qu'on
    puisse distinguer un écran réellement identique d'un dump qui n'a pas suivi l'écran.
    La raison du FAIL nomme donc la preuve (`png=same` / `png=diff`) au lieu de la seule
    égalité du dump — aucun seuil, aucun point P0, aucune porte ne change.
    """
    cur_png = cap.get("png_sha256") or ""
    prev_png = prev_png or ""
    if not prev_png or not cur_png:
        return {"png": "unknown"}
    return {"png": "same" if prev_png == cur_png else "diff"}


def _time_order(sidecars):
    """Les captures dans l'ordre du TEMPS : « la capture précédente » est celle qui a été
    prise avant, jamais celle dont le nom de fichier se trie avant.

    Mesure run 44441344957 : 35b-driver-inride, prise 2 s APRÈS 37-driver-running-stop1,
    la faisait tomber en duplicate-state alors que les deux captures montrent le même
    écran (png=same) — c'est la seconde qui n'ajoute rien, pas la première.
    """
    if not any(cap.get("ts") for cap in sidecars):
        return list(sidecars)
    return sorted(sidecars, key=lambda c: (c.get("ts") or "", c.get("name") or ""))


def check_distinct(sidecars):
    """B5: same pixels (or same app + same labels + same node count) is a lie.

    A '-' clause is a reason to expect different labels, never a licence to reuse
    the same pixels; only the explicit revisited-screen pair is exempt.
    """
    duplicates, seen, sig_seen = [], {}, {}
    for cap in _time_order(sidecars):
        if cap.get("status") != "MATCH":
            continue
        point = cap.get("point") or ""
        digest = cap.get("dump_sha256") or ""
        if digest and digest in seen:
            prev_name, prev_point, prev_png = seen[digest]
            if not _revisited(prev_point, point):
                duplicates.append({"name": cap["name"], "point": point,
                                   "duplicate_of": prev_name, "signal": "dump",
                                   **_png_verdict(prev_png, cap)})
        if digest:
            seen.setdefault(digest, (cap["name"], point, cap.get("png_sha256") or ""))
        labels = [l for l in (cap.get("labels") or []) if str(l).strip()]
        if labels:
            sig = (cap.get("fg_pkg") or "",
                   tuple(sorted({_norm_label(l) for l in labels})),
                   int(cap.get("node_count") or 0))
            if sig in sig_seen:
                prev_name, prev_point, prev_png = sig_seen[sig]
                if (not _revisited(prev_point, point)
                        and not any(d["name"] == cap["name"] for d in duplicates)):
                    duplicates.append({"name": cap["name"], "point": point,
                                       "duplicate_of": prev_name, "signal": "labels",
                                       **_png_verdict(prev_png, cap)})
            sig_seen.setdefault(sig, (cap["name"], point, cap.get("png_sha256") or ""))
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

    # Points marqués INCONCLUSIVE emulator_crash par le harnais : le plantage de l'émulateur
    # est la raison, les contrôles « pas de capture MATCH » ne doivent pas les réécrire en FAIL.
    crashed = {r["point"] for r in points
               if r["result"] == "INCONCLUSIVE" and "emulator_crash" in r["detail"]}
    sidecars = validate_captures(out_dir, read_sidecars(out_dir))
    for cap in sidecars:
        if cap.get("status") == "INVALID" and cap.get("point") and cap["point"] not in crashed:
            _upsert(points, cap["point"], "FAIL",
                    f"capture invalid: {cap.get('name')} ({cap.get('invalid_reason')})")
    by_point = {}
    for cap in sidecars:
        by_point.setdefault(cap.get("point") or "", []).append(cap)

    # a P0 UI claim needs a MATCH capture, whatever the row says
    for point in p0:
        if point not in UI_POINTS or point in crashed:
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
                _upsert(points, point, "FAIL",
                        "no MATCH capture: "
                        + " ; ".join(capture_reason(c) for c in caps))

    duplicates = check_distinct(sidecars)
    for dup in duplicates:
        if dup["point"]:
            _upsert(points, dup["point"], "FAIL",
                    f"duplicate-state: {dup.get('signal', 'dump')} identical to "
                    f"{dup['duplicate_of']} (png={dup.get('png', 'unknown')})")

    b2 = check_b2(sidecars)
    if b2 is not None:
        if b2:
            _upsert(points, "b2-captures", "FAIL", "; ".join(b2))
        else:
            _upsert(points, "b2-captures", "PASS",
                    "offre et course: captures distinctes, -REFUSER sur la course")

    fails = [p for p in points if p["result"] == "FAIL"]
    warns = [p for p in points if p["result"] == "WARN"]
    skips = [p for p in points if p["result"] == "SKIP"]
    inconclusive = [p for p in points if p["result"] == "INCONCLUSIVE"]
    p0_rows = [p for p in points if p["point"] in p0_set]
    p0_nonpass = [p["point"] for p in p0_rows if p["result"] != "PASS"]
    verdict = ("FAIL" if fails or p0_nonpass or len(p0) == 0
               else ("PASS_WITH_WARNINGS" if warns or skips or inconclusive else "PASS"))

    flags = {}
    flags_path = os.path.join(out_dir, "ui-flags.txt")
    if os.path.isfile(flags_path):
        for line in open(flags_path, encoding="utf-8"):
            if "=" in line:
                key, _, value = line.strip().partition("=")
                flags[key] = value

    unstable = os.path.isfile(os.path.join(out_dir, "emulator-unstable.flag"))
    report = {
        "verdict": verdict,
        "emulator_unstable": unstable,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "points": points,
        "fail_count": len(fails),
        "warn_count": len(warns),
        "skip_count": len(skips),
        "inconclusive_count": len(inconclusive),
        "p0_total": len(p0),
        "p0_pass": sum(1 for p in p0_rows if p["result"] == "PASS"),
        "p0_nonpass": p0_nonpass,
        "ui_flags": flags,
        "captures": [{k: c.get(k) for k in
                      ("name", "point", "status", "marker", "marker_source",
                       "fg_pkg", "fg_source", "dump_sha256", "png_sha256",
                       "failed_spec", "node_count", "invalid_reason")}
                     for c in sidecars],
        "duplicates": duplicates,
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
        *(["> ⚠ ÉMULATEUR INSTABLE (ActivityManager has died / ANR in system) : "
           "les échecs UI de ce run ne sont pas imputables à l'app.", ""] if unstable else []),
        f"P0: {report['p0_pass']}/{report['p0_total']} PASS — "
        f"non-PASS: {', '.join(p0_nonpass) or 'none'}",
        "",
        "| point | result | detail |",
        "|---|---|---|",
    ]
    lines += [f"| {p['point']} | {p['result']} | {p['detail']} |" for p in points]
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
          f"inconclusive={report['inconclusive_count']} "
          f"p0={report['p0_pass']}/{report['p0_total']} "
          f"nonpass={','.join(report['p0_nonpass']) or 'none'}"
          + (" emulator_unstable=1" if report["emulator_unstable"] else ""))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
