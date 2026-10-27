#!/usr/bin/env python3
"""TDD spec for e2e/report.py — run with `python3 e2e/tests/test_report.py`.

Covers the strict verdict rules the run must obey:
  * worst-wins aggregation per point (FAIL > SKIP > WARN > PASS)
  * any P0 WARN/SKIP => verdict FAIL
  * a P0 point that was never executed => FAIL "not attempted (harness)"
  * a P0 UI point with no MATCH sidecar => FAIL "not attempted (harness)"
  * a MATCH capture whose dump repeats an earlier one without a negative
    spec => FAIL "duplicate-state"
Nothing here touches adb, the network, or the real run.
"""
import hashlib
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

RIDER_PKG = "sn.galass.galassrider"
DRIVER_PKG = "sn.galass.galassdriver"
EMPTY_SHA = hashlib.sha256(b"").hexdigest()
PNG8 = b"\x89PNG\r\n\x1a\n"

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def default_fg(point):
    return DRIVER_PKG if point.startswith("driver-") else RIDER_PKG


def make_out(rows, captures=()):
    """Build a fake $OUT: results-raw.txt + screens/*.json sidecars."""
    out = tempfile.mkdtemp(prefix="e2e-report-")
    os.makedirs(os.path.join(out, "screens"), exist_ok=True)
    with open(os.path.join(out, "results-raw.txt"), "w") as fh:
        for r in rows:
            fh.write("|".join(r) + "\n")
    for i, cap in enumerate(captures):
        name = cap.get("name") or f"{i:02d}-capture"
        point = cap.get("point", "")
        body = {
            "name": name,
            "point": point,
            "status": cap.get("status", "MATCH"),
            "marker": cap.get("marker", "+Anything"),
            "dump_sha256": cap.get("dump_sha256", f"hash{i}"),
            "png_sha256": cap.get("png_sha256", f"png{i}"),
            "marker_source": cap.get("marker_source", "grounded"),
            "fg_pkg": cap.get("fg_pkg") or default_fg(point),
            "labels": cap.get("labels", []),
            "node_count": cap.get("node_count", 0),
            "failed_spec": cap.get("failed_spec", ""),
        }
        with open(os.path.join(out, "screens", name + ".json"), "w") as fh:
            json.dump(body, fh)
        # png_bytes=None simulates a capture with no PNG at all
        size = cap.get("png_bytes", 8)
        if size is not None:
            with open(os.path.join(out, "screens", name + ".png"), "wb") as fh:
                fh.write(PNG8[:size] if size <= 8 else PNG8 + b"0" * (size - 8))
    return out


# ------------------------------------------------------------------ cases
@case
def test_worst_wins_per_point():
    import report

    rows = [{"point": "a", "result": "PASS", "detail": "ok"},
            {"point": "a", "result": "WARN", "detail": "late warn"},
            {"point": "b", "result": "PASS", "detail": "ok"},
            {"point": "b", "result": "FAIL", "detail": "boom"},
            {"point": "c", "result": "SKIP", "detail": "skip"},
            {"point": "c", "result": "PASS", "detail": "late pass"}]
    agg = {r["point"]: r["result"] for r in report.aggregate(rows)}
    assert agg == {"a": "WARN", "b": "FAIL", "c": "SKIP"}, agg


@case
def test_p0_warn_and_skip_make_verdict_fail():
    import report

    out = make_out([
        ("rider-home-loaded", "FAIL", "not attempted (harness)"),
        ("rider-rating", "WARN", "marker absent"),
        ("driver-summary", "SKIP", "artemis disabled"),
        ("no-prod-calls", "PASS", "0 hits"),
    ], captures=[
        {"name": "30-rider-rating", "point": "rider-rating", "marker": "+Notez"},
        {"name": "43-driver-summary", "point": "driver-summary", "marker": "+Gains"},
    ])
    res = report.build(out, p0={"rider-home-loaded", "rider-rating", "driver-summary", "no-prod-calls"})
    assert res["verdict"] == "FAIL", res["verdict"]
    assert set(res["p0_nonpass"]) == {"rider-home-loaded", "rider-rating", "driver-summary"}, res["p0_nonpass"]
    assert res["fail_count"] == 1 and res["warn_count"] == 1 and res["skip_count"] == 1


@case
def test_unexecuted_p0_point_synthesized_fail():
    import report

    out = make_out([("no-prod-calls", "PASS", "0 hits")])
    res = report.build(out, p0={"no-prod-calls", "rider-stop1-done"})
    row = [p for p in res["points"] if p["point"] == "rider-stop1-done"]
    assert row, res["points"]
    assert row[0]["result"] == "FAIL", row[0]
    assert "not attempted" in row[0]["detail"]
    assert res["verdict"] == "FAIL"


@case
def test_p0_ui_point_without_match_sidecar_is_fail():
    import report

    out = make_out([("rider-stop-picker", "PASS", "ui cap=16.png marker ok")],
                   captures=[{"name": "16-rider-stop-picker", "point": "rider-stop-picker",
                              "status": "TIMEOUT", "marker": "+Arrêt 1"}])
    res = report.build(out, p0={"rider-stop-picker"})
    # PASS was claimed but the only sidecar for that point is TIMEOUT: strict FAIL
    row = [p for p in res["points"] if p["point"] == "rider-stop-picker"][0]
    assert row["result"] == "FAIL", row
    assert "capture" in row["detail"], row


@case
def test_p0_ui_point_with_no_sidecar_is_not_attempted():
    import report

    out = make_out([("rider-rating", "PASS", "ui cap=30.png marker ok")])
    res = report.build(out, p0={"rider-rating"})
    row = [p for p in res["points"] if p["point"] == "rider-rating"][0]
    assert row["result"] == "FAIL", row
    assert "not attempted (harness)" in row["detail"], row


@case
def test_duplicate_dump_without_negative_spec_is_fail():
    import report

    caps = [
        {"name": "14-rider-home", "point": "rider-home-loaded", "marker": "+Où allez-vous|-Chargement",
         "dump_sha256": "same"},
        {"name": "22-rider-stop1-active", "point": "rider-stop1-active", "marker": "+Arrêt 1",
         "dump_sha256": "same"},
    ]
    out = make_out([("rider-home-loaded", "PASS", "ok"), ("rider-stop1-active", "PASS", "ok")], caps)
    res = report.build(out, p0={"rider-home-loaded", "rider-stop1-active"})
    rows = {p["point"]: p for p in res["points"]}
    assert rows["rider-stop1-active"]["result"] == "FAIL", rows["rider-stop1-active"]
    assert "duplicate-state" in rows["rider-stop1-active"]["detail"]
    assert rows["rider-home-loaded"]["result"] == "PASS", rows["rider-home-loaded"]
    assert res["duplicates"] and res["duplicates"][0]["name"] == "22-rider-stop1-active"


@case
def test_duplicate_dump_with_negative_spec_still_fails():
    """B5: a '-' clause is a reason to expect different labels, never a licence
    to reuse the same pixels."""
    import report

    caps = [
        {"name": "22-rider-stop1-active", "point": "rider-stop1-active",
         "marker": "+Arrêt en cours|-Arrêt 1", "dump_sha256": "same"},
        {"name": "23-rider-stop1-done", "point": "rider-stop1-done",
         "marker": "+2/2|-Destination", "dump_sha256": "same"},
    ]
    out = make_out([("rider-stop1-active", "PASS", "ok"), ("rider-stop1-done", "PASS", "ok")], caps)
    res = report.build(out, p0={"rider-stop1-active", "rider-stop1-done"})
    rows = {p["point"]: p for p in res["points"]}
    assert rows["rider-stop1-done"]["result"] == "FAIL", rows["rider-stop1-done"]
    assert "duplicate-state" in rows["rider-stop1-done"]["detail"]
    assert res["duplicates"], res["duplicates"]


@case
def test_revisited_screen_pair_is_allowlisted():
    """B5: only the explicit revisited-screen pair may share a dump digest."""
    import report

    caps = [
        {"name": "14-rider-home", "point": "rider-home-loaded", "marker": "+Où allez-vous|-Chargement",
         "dump_sha256": "same"},
        {"name": "31-rider-home-after", "point": "rider-home-after",
         "marker": "+Où allez-vous|-Course terminée", "dump_sha256": "same"},
    ]
    out = make_out([("rider-home-loaded", "PASS", "ok"), ("rider-home-after", "PASS", "ok")], caps)
    res = report.build(out, p0={"rider-home-loaded", "rider-home-after"})
    rows = {p["point"]: p for p in res["points"]}
    assert rows["rider-home-after"]["result"] == "PASS", rows["rider-home-after"]
    assert not res["duplicates"], res["duplicates"]


@case
def test_label_signature_duplicate_detected():
    """B5 secondary signal: same app + same visible labels + same node count is a
    duplicate even when the dump bytes drifted (timestamps, ids)."""
    import report

    caps = [
        {"name": "21-rider-running-start", "point": "rider-running-start",
         "marker": "+En course|-Arrêt 1", "dump_sha256": "a",
         "labels": ["En course", "Terminez votre course", "12 min"], "node_count": 42},
        {"name": "22-rider-stop1-active", "point": "rider-stop1-active",
         "marker": "+Arrêt en cours|-Arrêt 1", "dump_sha256": "b",
         "labels": ["En course", "Terminez votre course", "13 min"], "node_count": 42},
    ]
    out = make_out([("rider-running-start", "PASS", "ok"), ("rider-stop1-active", "PASS", "ok")], caps)
    res = report.build(out, p0={"rider-running-start", "rider-stop1-active"})
    rows = {p["point"]: p for p in res["points"]}
    assert rows["rider-stop1-active"]["result"] == "FAIL", rows["rider-stop1-active"]
    assert "duplicate-state" in rows["rider-stop1-active"]["detail"]


# ------------------------------------------------- B4 capture integrity
@case
def test_capture_without_png_is_invalid():
    import report

    out = make_out([("rider-home-loaded", "PASS", "ok")],
                   [{"name": "14-rider-home", "point": "rider-home-loaded",
                     "marker": "+Où allez-vous", "png_bytes": None}])
    res = report.build(out, p0={"rider-home-loaded"})
    row = [p for p in res["points"] if p["point"] == "rider-home-loaded"][0]
    assert row["result"] == "FAIL", row
    assert "capture invalid" in row["detail"], row


@case
def test_capture_with_tiny_png_is_invalid():
    import report

    out = make_out([("rider-home-loaded", "PASS", "ok")],
                   [{"name": "14-rider-home", "point": "rider-home-loaded",
                     "marker": "+Où allez-vous", "png_bytes": 4}])
    res = report.build(out, p0={"rider-home-loaded"})
    row = [p for p in res["points"] if p["point"] == "rider-home-loaded"][0]
    assert row["result"] == "FAIL", row
    assert "capture invalid" in row["detail"], row


@case
def test_capture_with_empty_file_hash_is_invalid():
    import report

    out = make_out([("rider-home-loaded", "PASS", "ok")],
                   [{"name": "14-rider-home", "point": "rider-home-loaded",
                     "marker": "+Où allez-vous", "png_sha256": EMPTY_SHA}])
    res = report.build(out, p0={"rider-home-loaded"})
    row = [p for p in res["points"] if p["point"] == "rider-home-loaded"][0]
    assert row["result"] == "FAIL", row
    assert "capture invalid" in row["detail"], row


@case
def test_capture_with_wrong_foreground_is_invalid():
    import report

    out = make_out([("rider-home-loaded", "PASS", "ok")],
                   [{"name": "14-rider-home", "point": "rider-home-loaded",
                     "marker": "+Où allez-vous", "fg_pkg": DRIVER_PKG}])
    res = report.build(out, p0={"rider-home-loaded"})
    row = [p for p in res["points"] if p["point"] == "rider-home-loaded"][0]
    assert row["result"] == "FAIL", row
    assert "capture invalid" in row["detail"], row
    assert "foreground" in row["detail"], row


# ------------------------------------------------- S1 zero-P0 guard
@case
def test_zero_p0_run_falls_back_and_fails():
    import report

    out = make_out([("rider-home-loaded", "PASS", "ok")])
    res = report.build(out, p0=[])
    assert res["p0_total"] == len(report.P0_DEFAULT), res["p0_total"]
    assert res["verdict"] == "FAIL", res["verdict"]


# ------------------------------------------------- S3 B2 distinctness
def _b2_caps(**over):
    offer = {"name": "34-driver-offer-with-stops", "point": "driver-offer-with-stops",
             "marker": "+Accepter\\s+la\\s+course|-Accueil", "dump_sha256": "offer-dump",
             "png_sha256": "offer-png"}
    ride = {"name": "35b-driver-inride", "point": "driver-inride-stops",
            "marker": "+Terminer la course|+Arrêt 1|-Accepter\\s+la\\s+course|-REFUSER",
            "dump_sha256": "ride-dump", "png_sha256": "ride-png"}
    for key, value in over.items():
        target = offer if key.startswith("offer_") else ride
        target[key.split("_", 1)[1]] = value
    return [offer, ride]


@case
def test_b2_captures_require_distinct_evidence():
    import report

    out = make_out([("driver-offer-with-stops", "PASS", "ok")], _b2_caps())
    res = report.build(out, p0={"driver-offer-with-stops"})
    rows = {p["point"]: p for p in res["points"]}
    assert rows["b2-captures"]["result"] == "PASS", rows["b2-captures"]

    out = make_out([("driver-offer-with-stops", "PASS", "ok")],
                   _b2_caps(ride_png_sha256="offer-png"))
    res = report.build(out, p0={"driver-offer-with-stops"})
    rows = {p["point"]: p for p in res["points"]}
    assert rows["b2-captures"]["result"] == "FAIL", rows["b2-captures"]
    assert "same png" in rows["b2-captures"]["detail"]

    out = make_out([("driver-offer-with-stops", "PASS", "ok")],
                   _b2_caps(ride_marker="+Terminer la course|+Arrêt 1|-Accepter\\s+la\\s+course"))
    res = report.build(out, p0={"driver-offer-with-stops"})
    rows = {p["point"]: p for p in res["points"]}
    assert rows["b2-captures"]["result"] == "FAIL", rows["b2-captures"]
    assert "REFUSER" in rows["b2-captures"]["detail"]


# ------------------------------------------------- B7 notification attribution
@case
def test_notif_rejects_numeric_uid_and_decoy():
    import notif

    dump = (
        "NotificationRecord(0x1: pkg=sn.galass.galassdriver user=0)\n"
        "  extras: Bundle[{title=Course 12 min away, ride_id=12}]\n"
        "NotificationRecord(0x2: pkg=sn.galass.galassdriver user=0)\n"
        "  extras: Bundle[{title=Nouvelle course, uid=8f3c1d9a4b2e}]\n"
    )
    res = notif.probe(dump, "sn.galass.galassdriver", "12")
    assert res["records_token"] == 0, res
    assert res["token_valid"] is False, res
    assert res["records_pkg"] == 2, res


@case
def test_notif_rejects_short_uid():
    import notif

    dump = "NotificationRecord(0x1: pkg=sn.galass.galassdriver)\n  extras: Bundle[{uid=8f3c}]\n"
    res = notif.probe(dump, "sn.galass.galassdriver", "8f3c")
    assert res["token_valid"] is False, res
    assert res["records_token"] == 0, res


@case
def test_notif_matches_uid_on_word_boundary():
    import notif

    dump = (
        "NotificationRecord(0x1: pkg=sn.galass.galassdriver)\n"
        "  extras: Bundle[{uid=x8f3c1d9a4b2e}]\n"
        "NotificationRecord(0x2: pkg=sn.galass.galassdriver)\n"
        "  extras: Bundle[{uid=8f3c1d9a4b2e}]\n"
    )
    res = notif.probe(dump, "sn.galass.galassdriver", "8f3c1d9a4b2e")
    assert res["token_valid"] is True, res
    assert res["records_token"] == 1, res


@case
def test_not_attempted_fixture_has_zero_p0_pass():
    import report

    out = make_out([])
    res = report.build(out, p0={"rider-home-loaded", "driver-summary"})
    assert all(p["result"] == "FAIL" for p in res["points"])
    assert res["p0_pass"] == 0
    assert res["verdict"] == "FAIL"


def _artemis_out(usage, attempts=None, chain=None, requested=None):
    out = make_out([("no-prod-calls", "PASS", "0 hit")])
    adir = os.path.join(out, "artemis")
    os.makedirs(adir, exist_ok=True)
    with open(os.path.join(adir, "model-usage.txt"), "w") as fh:
        fh.write(usage)
    with open(os.path.join(adir, "attempts.txt"), "w") as fh:
        fh.write("\n".join(attempts or []) + ("\n" if attempts else ""))
    if chain is not None:
        with open(os.path.join(adir, "vision-chain.txt"), "w") as fh:
            fh.write("\n".join(chain) + "\n")
    if requested is not None:
        with open(os.path.join(adir, "vision-chain-requested.txt"), "w") as fh:
            fh.write("\n".join(requested) + ("\n" if requested else ""))
    return out


@case
def test_artemis_section_reports_served_model_and_attempts():
    import report

    out = _artemis_out(
        "driver-accept|2|6|head=gpt-4.1|primary=gemini-2.5-flash|fallback=gpt-4o"
        "|fallback_events=0|served=primary|signals=429|rc=0|served_model=gemini-2.5-flash"
        "|chain=gpt-4.1,gemini-2.5-flash\n",
        ["driver-accept|1|gpt-4.1|1|15|429|retryable",
         "driver-accept|2|gemini-2.5-flash|0|15|15|success"],
        ["gpt-4.1", "gemini-2.5-flash"],
        requested=["gpt-4.1", "gemini-2.5-flash"])
    res = report.build(out, p0={"no-prod-calls"})
    art = res.get("artemis")
    assert art, "report['artemis'] absent"
    assert art["runs"][0]["name"] == "driver-accept", art
    assert art["runs"][0]["served_model"] == "gemini-2.5-flash", art["runs"][0]
    assert art["runs"][0]["attempts"] == 2, art["runs"][0]
    assert art["chain"] == ["gpt-4.1", "gemini-2.5-flash"], art["chain"]
    assert len(art["attempts"]) == 2, art["attempts"]
    assert art["attempts"][0]["outcome"] == "retryable", art["attempts"][0]
    summary = open(os.path.join(out, "summary.md"), encoding="utf-8").read()
    assert "## Artemis" in summary, summary
    assert "gemini-2.5-flash" in summary, summary


@case
def test_artemis_keeps_every_allowlisted_signal_token():
    """run.sh joins signals with ',' (`signals=429,timeout`): report both."""
    import report

    out = _artemis_out(
        "driver-accept|2|6|served=primary|signals=429,timeout|rc=0"
        "|served_model=gpt-4.1|chain=gpt-4.1\n",
        ["driver-accept|1|gpt-4.1|1|15|429|retryable",
         "driver-accept|2|gpt-4.1|0|15|timeout|success"])
    res = report.build(out, p0={"no-prod-calls"})
    assert res["artemis"]["runs"][0]["signals"] == "429,timeout", res["artemis"]["runs"][0]
    summary = open(os.path.join(out, "summary.md"), encoding="utf-8").read()
    assert "signals=429,timeout" in summary, summary

    # a single signal is unchanged
    single = _artemis_out(
        "driver-accept|1|1|served=primary|signals=429|rc=0|served_model=gpt-4.1\n")
    res = report.build(single, p0={"no-prod-calls"})
    assert res["artemis"]["runs"][0]["signals"] == "429", res["artemis"]["runs"][0]


@case
def test_artemis_a_poisoned_signal_token_drops_alone():
    """One bad token must not blank the whole row, and must not reach the report."""
    import report

    bad = "<svg/onload=alert(1)>"
    out = _artemis_out(
        f"driver-accept|2|6|served=primary|signals=429,{bad},timeout|rc=0"
        f"|served_model=gpt-4.1\n",
        ["driver-accept|1|gpt-4.1|1|15|429|retryable",
         "driver-accept|2|gpt-4.1|0|15|timeout|success"])
    res = report.build(out, p0={"no-prod-calls"})
    assert res["artemis"]["runs"][0]["signals"] == "429,timeout", res["artemis"]["runs"][0]
    blob = json.dumps(res, ensure_ascii=False)
    summary = open(os.path.join(out, "summary.md"), encoding="utf-8").read()
    for needle in ("<svg", "onload"):
        assert needle not in blob, (needle, blob)
        assert needle not in summary, (needle, summary)


@case
def test_artemis_served_model_is_unknown_on_failure():
    import report

    out = _artemis_out(
        "driver-accept|1|6|head=gpt-4.1|primary=gpt-4.1|fallback=gemini-2.5-flash"
        "|fallback_events=0|served=unknown|signals=-|rc=2|served_model=unknown"
        "|chain=gpt-4.1\n",
        ["driver-accept|1|gpt-4.1|2|15|-|terminal"])
    res = report.build(out, p0={"no-prod-calls"})
    assert res["artemis"]["runs"][0]["served_model"] == "unknown", res["artemis"]
    # a legacy row without served_model still derives honestly from rc
    out2 = _artemis_out(
        "driver-accept|1|6|head=gpt-4.1|primary=gpt-4.1|fallback=none"
        "|fallback_events=0|served=unknown|signals=-|rc=2\n")
    res2 = report.build(out2, p0={"no-prod-calls"})
    assert res2["artemis"]["runs"][0]["served_model"] == "unknown", res2["artemis"]


@case
def test_artemis_drops_ids_outside_the_allowlist():
    """A poisoned id must never reach report.json or summary.md."""
    import report

    bad = "evil;rm -rf /tmp/x"
    out = _artemis_out(
        f"driver-accept|1|1|head={bad}|primary={bad}|fallback=none"
        f"|fallback_events=0|served=unknown|signals=-|rc=1|served_model={bad}"
        f"|chain={bad},gpt-4.1\n",
        [f"driver-accept|1|{bad}|1|15|-|terminal"],
        [bad, "gpt-4.1"],
        requested=[bad, "gpt-4.1"])
    res = report.build(out, p0={"no-prod-calls"})
    blob = json.dumps(res, ensure_ascii=False)
    assert "rm -rf" not in blob, blob
    assert "evil" not in blob, blob
    assert res["artemis"]["chain"] == ["gpt-4.1"], res["artemis"]["chain"]
    assert res["artemis"]["requested_chain"] == ["gpt-4.1"], res["artemis"]["requested_chain"]
    summary = open(os.path.join(out, "summary.md"), encoding="utf-8").read()
    assert "rm -rf" not in summary and "evil" not in summary, summary


@case
def test_artemis_poisoned_name_and_signals_cannot_reach_the_summary():
    """A poisoned model-usage.txt/attempts.txt must not inject into summary.md."""
    import report

    out = _artemis_out(
        "driver-<img src=x onerror=alert(1)>|1|1|head=gpt-4.1|primary=gpt-4.1"
        "|fallback=none|fallback_events=0|served=<script>alert(1)</script>"
        "|signals=<svg/onload=alert(1)>|rc=2|served_model=unknown"
        "|chain=gpt-4.1\n",
        ["bad<script>|1|gpt-4.1|2|15|<b>x</b>|</td><script>alert(1)</script>"])
    res = report.build(out, p0={"no-prod-calls"})
    blob = json.dumps(res, ensure_ascii=False)
    summary = open(os.path.join(out, "summary.md"), encoding="utf-8").read()
    for needle in ("<script", "<img", "<svg", "<b>", "</td"):
        assert needle not in blob, (needle, blob)
        assert needle not in summary, (needle, summary)
    assert "<" not in summary, summary
    assert res["artemis"]["runs"][0]["served"] == "unknown", res["artemis"]["runs"][0]
    assert res["artemis"]["attempts"][0]["outcome"] == "unknown", res["artemis"]["attempts"][0]


@case
def test_artemis_outcome_is_a_strict_enum():
    import report

    out = _artemis_out(
        "driver-accept|1|1|served=primary|signals=-|rc=0|served_model=gpt-4.1\n",
        ["driver-accept|1|gpt-4.1|0|15|-|success",
         "driver-accept|1|gpt-4.1|2|15|-|terminal",
         "driver-accept|1|gpt-4.1|1|15|429|retryable",
         "driver-accept|1|-|124|0|-|budget",
         "driver-accept|1|-|1|0|-|injected|extra"])
    res = report.build(out, p0={"no-prod-calls"})
    assert [a["outcome"] for a in res["artemis"]["attempts"]] == \
        ["success", "terminal", "retryable", "budget"], res["artemis"]["attempts"]
    # served is an enum too: an unknown word is not evidence
    bad = _artemis_out("driver-accept|1|1|served=primary-ish|signals=-|rc=0"
                       "|served_model=gpt-4.1\n")
    res = report.build(bad, p0={"no-prod-calls"})
    assert res["artemis"]["runs"][0]["served"] == "unknown", res["artemis"]["runs"][0]


@case
def test_artemis_absent_files_are_an_empty_section():
    import report

    out = make_out([("no-prod-calls", "PASS", "0 hit")])
    res = report.build(out, p0={"no-prod-calls"})
    assert res["artemis"] == {"runs": [], "attempts": [], "chain": [],
                              "requested_chain": [], "budget_exhausted": []}, res["artemis"]


@case
def test_artemis_reports_the_requested_chain():
    """ISC-6: the chain run.sh asked for is reported, not only the survivors."""
    import report

    out = _artemis_out(
        "driver-accept|1|1|head=gpt-4o|primary=gpt-4.1|fallback=none|fallback_events=0"
        "|served=primary|signals=-|rc=0|served_model=gpt-4.1|chain=gpt-4.1\n",
        ["driver-accept|1|gpt-4.1|0|15|-|success"],
        ["gpt-4.1"],
        requested=["gpt-4o", "gpt-4.1", "gemini-2.5-flash"])
    res = report.build(out, p0={"no-prod-calls"})
    art = res["artemis"]
    assert art["requested_chain"] == ["gpt-4o", "gpt-4.1", "gemini-2.5-flash"], art
    assert art["chain"] == ["gpt-4.1"], art
    summary = open(os.path.join(out, "summary.md"), encoding="utf-8").read()
    assert "chaîne demandée: gpt-4o, gpt-4.1, gemini-2.5-flash" in summary, summary


@case
def test_artemis_requested_chain_is_empty_without_the_requested_file():
    """No requested file => nothing was asked for. A hand-built $OUT with only the
    legacy probe chain must not be reported as `chaîne demandée`."""
    import report

    out = _artemis_out(
        "driver-accept|1|1|served=primary|signals=-|rc=0|served_model=gpt-4.1\n",
        ["driver-accept|1|gpt-4.1|0|15|-|success"],
        ["gpt-4.1", "gemini-2.5-flash"])
    res = report.build(out, p0={"no-prod-calls"})
    assert res["artemis"]["requested_chain"] == [], res["artemis"]
    assert res["artemis"]["chain"] == ["gpt-4.1", "gemini-2.5-flash"], res["artemis"]
    summary = open(os.path.join(out, "summary.md"), encoding="utf-8").read()
    assert "chaîne demandée" not in summary, summary


@case
def test_artemis_empty_requested_chain_file_does_not_crash():
    import report

    out = _artemis_out(
        "driver-accept|1|1|served=primary|signals=-|rc=0|served_model=gpt-4.1\n",
        ["driver-accept|1|gpt-4.1|0|15|-|success"],
        ["gpt-4.1"],
        requested=[])
    res = report.build(out, p0={"no-prod-calls"})
    assert res["artemis"]["requested_chain"] == [], res["artemis"]
    assert res["artemis"]["chain"] == ["gpt-4.1"], res["artemis"]


@case
def test_artemis_requested_chain_ids_are_allowlisted():
    """A poisoned vision-chain-requested.txt must not reach report.json/summary.md."""
    import report

    bad = "evil;rm -rf /tmp/x"
    out = _artemis_out(
        "driver-accept|1|1|served=primary|signals=-|rc=0|served_model=gpt-4.1\n",
        ["driver-accept|1|gpt-4.1|0|15|-|success"],
        ["gpt-4.1"],
        requested=[bad, "gpt-4o", "<img src=x onerror=alert(1)>"])
    res = report.build(out, p0={"no-prod-calls"})
    assert res["artemis"]["requested_chain"] == ["gpt-4o"], res["artemis"]
    blob = json.dumps(res, ensure_ascii=False)
    summary = open(os.path.join(out, "summary.md"), encoding="utf-8").read()
    for needle in ("rm -rf", "evil", "<img"):
        assert needle not in blob, (needle, blob)
        assert needle not in summary, (needle, summary)


@case
def test_artemis_budget_exhaustion_is_visible():
    import report

    out = _artemis_out(
        "driver-accept|1|6|head=gpt-4.1|primary=none|fallback=none|fallback_events=0"
        "|served=unknown|signals=-|rc=2|served_model=unknown|chain=gpt-4.1\n",
        ["driver-accept|1|-|-|0|budget|budget"])
    res = report.build(out, p0={"no-prod-calls"})
    assert res["artemis"]["budget_exhausted"] == ["driver-accept"], res["artemis"]
    row = res["artemis"]["attempts"][0]
    assert row["rc"] is None, row
    assert row["signal"] == "budget" and row["outcome"] == "budget", row


@case
def test_artifacts_are_written():
    import report

    out = make_out([("rider-home-loaded", "PASS", "ok")],
                   [{"name": "14-rider-home", "point": "rider-home-loaded",
                     "marker": "+Où allez-vous|-Chargement"}])
    res = report.build(out, p0={"rider-home-loaded"})
    assert res["verdict"] == "PASS", res
    for name in ("results.txt", "results-raw.txt", "report.json", "summary.md"):
        assert os.path.isfile(os.path.join(out, name)), name
    agg = open(os.path.join(out, "results.txt")).read().strip().splitlines()
    assert agg == ["rider-home-loaded|PASS|ok"], agg
    on_disk = json.load(open(os.path.join(out, "report.json")))
    assert on_disk["verdict"] == "PASS"
    assert "rider-home-loaded" in open(os.path.join(out, "summary.md")).read()


@case
def test_p0_default_matches_run_sh():
    import report

    run = open(os.path.join(ROOT, "run.sh")).read()
    m = __import__("re").search(r'P0_POINTS="([^"]+)"', run, __import__("re").S)
    assert m, "P0_POINTS introuvable dans run.sh"
    declared = set(m.group(1).replace("\\\n", " ").split())
    assert set(report.P0_DEFAULT) == declared, (set(report.P0_DEFAULT) ^ declared)
    for point in ("rider-destination-selected", "rider-cash-methods"):
        assert point in declared, point


@case
def test_build_cli_exit_code():
    import subprocess

    out = make_out([("rider-home-loaded", "WARN", "marker absent")])
    proc = subprocess.run([sys.executable, os.path.join(ROOT, "report.py"), out,
                           "--p0", "rider-home-loaded"], capture_output=True, text=True)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert "verdict=FAIL" in proc.stdout, proc.stdout


# ------------------------------------------------------------------- main
def main():
    failed = 0
    for fn in CASES:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - test harness
            failed += 1
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {fn.__name__}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} report cases passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
