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
            "fg_source": cap.get("fg_source", "dumpsys"),
            "labels": cap.get("labels", []),
            "node_count": cap.get("node_count", 0),
            "ts": cap.get("ts", ""),
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
def test_duplicate_reason_reports_pixels_when_the_dump_repeats():
    """Mesure run 44412970992 : quatre captures tombaient en « duplicate-state » sans
    qu'on puisse dire si l'écran était vraiment identique ou si le dump était périmé.
    La raison doit donc nommer la seule preuve qui tranche : les pixels.

    Même dump + pixels différents = le dump n'a pas suivi l'écran (défaut de dump).
    Même dump + mêmes pixels = l'écran est réellement le même (fait produit).
    """
    import report

    caps = [
        {"name": "35-driver-accepted-details", "point": "driver-accepted-details",
         "marker": "+Arrêt 1", "dump_sha256": "same", "png_sha256": "png-a"},
        {"name": "36-driver-pickup-arrived", "point": "driver-pickup-arrived",
         "marker": "+Prise en charge du client", "dump_sha256": "same",
         "png_sha256": "png-b"},
    ]
    out = make_out([("driver-accepted-details", "PASS", "ok"),
                    ("driver-pickup-arrived", "PASS", "ok")], caps)
    res = report.build(out, p0={"driver-accepted-details", "driver-pickup-arrived"})
    row = [p for p in res["points"] if p["point"] == "driver-pickup-arrived"][0]
    assert "premise" not in row["detail"], row
    assert "png=diff" in row["detail"], row
    assert "35-driver-accepted-details" in row["detail"], row


@case
def test_duplicate_reason_says_png_same_when_pixels_repeat():
    import report

    caps = [
        {"name": "37-driver-running-stop1", "point": "driver-running-stop1",
         "marker": "+Arrêt 1", "dump_sha256": "same", "png_sha256": "png-a"},
        {"name": "38-driver-stop1-done", "point": "driver-stop1-done",
         "marker": "+1/2 arrêt terminé", "dump_sha256": "same", "png_sha256": "png-a"},
    ]
    out = make_out([("driver-running-stop1", "PASS", "ok"),
                    ("driver-stop1-done", "PASS", "ok")], caps)
    res = report.build(out, p0={"driver-running-stop1", "driver-stop1-done"})
    row = [p for p in res["points"] if p["point"] == "driver-stop1-done"][0]
    assert row["result"] == "FAIL", row
    assert "png=same" in row["detail"], row


@case
def test_label_signature_duplicate_detected():
    """B5 secondary signal: same app + same visible labels + same node count is a
    duplicate even when the dump bytes drifted (timestamps, ids)."""
    import report

    caps = [
        {"name": "22-rider-stop1-active", "point": "rider-stop1-active",
         "marker": "+Arrêt 1", "dump_sha256": "a",
         "labels": ["En route", "Arrêt 1", "12 min"], "node_count": 42},
        {"name": "23-rider-stop1-done", "point": "rider-stop1-done",
         "marker": "+Arrêt terminé", "dump_sha256": "b",
         "labels": ["En route", "Arrêt 1", "13 min"], "node_count": 42},
    ]
    out = make_out([("rider-stop1-active", "PASS", "ok"), ("rider-stop1-done", "PASS", "ok")], caps)
    res = report.build(out, p0={"rider-stop1-active", "rider-stop1-done"})
    rows = {p["point"]: p for p in res["points"]}
    assert rows["rider-stop1-done"]["result"] == "FAIL", rows["rider-stop1-done"]
    assert "duplicate-state" in rows["rider-stop1-done"]["detail"]


@case
def test_rider_running_start_stop1_active_is_revisited_pair():
    """Même écran RUNNING jambe 1 par design : 21 = départ, 22 = carte horodatée en cours
    de jambe 1. XML/marqueurs textuels identiques possibles (pins/polyline = preuve PNG
    archivée GPG) => paire revisitée, pas un duplicate-state."""
    import report

    caps = [
        {"name": "21-rider-running-start", "point": "rider-running-start",
         "marker": "+En route vers|-Chargement", "dump_sha256": "same",
         "labels": ["En route vers", "12 min"], "node_count": 42},
        {"name": "22-rider-stop1-active", "point": "rider-stop1-active",
         "marker": "+En route vers|-Arrêt 2", "dump_sha256": "same",
         "labels": ["En route vers", "12 min"], "node_count": 42},
    ]
    out = make_out([("rider-running-start", "PASS", "ok"), ("rider-stop1-active", "PASS", "ok")], caps)
    res = report.build(out, p0={"rider-running-start", "rider-stop1-active"})
    rows = {p["point"]: p for p in res["points"]}
    assert rows["rider-stop1-active"]["result"] == "PASS", rows["rider-stop1-active"]
    assert not res["duplicates"], res["duplicates"]


@case
def test_duplicate_blames_the_later_capture_in_time_not_the_later_name():
    """Measure run 44441344957: `driver-running-stop1` (P0) fell in duplicate-state
    against `35b-driver-inride` — a capture taken TWO SECONDS LATER, whose name simply
    sorts first ("35b" < "37"). The "previous capture" is the earlier one in time.

    Same dump + same pixels => the two captures show the same screen: the point that
    came second added nothing, so it is the one whose distinctness is unproven.
    """
    import report

    caps = [
        {"name": "35b-driver-inride", "point": "driver-inride-stops",
         "marker": "+Arrêt 1|+Terminer la course", "dump_sha256": "same",
         "png_sha256": "png-same", "ts": "2026-11-19T02:20:02Z"},
        {"name": "37-driver-running-stop1", "point": "driver-running-stop1",
         "marker": "+Arrêt 1|+Terminer la course", "dump_sha256": "same",
         "png_sha256": "png-same", "ts": "2026-11-19T02:20:00Z"},
    ]
    out = make_out([("driver-inride-stops", "PASS", "ok"),
                    ("driver-running-stop1", "PASS", "ok")], caps)
    res = report.build(out, p0={"driver-inride-stops", "driver-running-stop1"})
    rows = {p["point"]: p for p in res["points"]}
    assert rows["driver-running-stop1"]["result"] == "PASS", rows["driver-running-stop1"]
    assert rows["driver-inride-stops"]["result"] == "FAIL", rows["driver-inride-stops"]
    assert res["duplicates"][0]["name"] == "35b-driver-inride", res["duplicates"]


@case
def test_label_signature_keeps_progress_counters_distinct():
    """Measure run 44441344957: `driver-stop1-done` (0/2 -> 1/2 stops done) was flagged
    as the same screen as `35b-driver-inride` (png=diff: the counter is rendered).

    Masking EVERY digit to catch a ticking timer also masks real progress: "0/2 arrêt
    terminé" and "1/2 arrêt terminé" are two different states. Only time-like tokens
    (durations, clocks) may be masked.
    """
    import report

    caps = [
        {"name": "35b-driver-inride", "point": "driver-inride-stops",
         "marker": "+0/2 arrêt terminé", "dump_sha256": "dump-a", "png_sha256": "png-a",
         "labels": ["Terminer la course", "0/2 arrêt terminé", "5 Min"],
         "node_count": 42, "ts": "2026-11-19T02:20:02Z"},
        {"name": "38-driver-stop1-done", "point": "driver-stop1-done",
         "marker": "+1/2 arrêt terminé", "dump_sha256": "dump-b", "png_sha256": "png-b",
         "labels": ["Terminer la course", "1/2 arrêt terminé", "5 Min"],
         "node_count": 42, "ts": "2026-11-19T02:21:30Z"},
    ]
    out = make_out([("driver-inride-stops", "PASS", "ok"),
                    ("driver-stop1-done", "PASS", "ok")], caps)
    res = report.build(out, p0={"driver-inride-stops", "driver-stop1-done"})
    rows = {p["point"]: p for p in res["points"]}
    assert rows["driver-stop1-done"]["result"] == "PASS", rows["driver-stop1-done"]
    assert not res["duplicates"], res["duplicates"]


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


# --------------------------------- B4b the reason must name what actually failed
@case
def test_timeout_capture_reports_the_marker_not_capture_invalid():
    """A capture that timed out is already a failure; calling it "capture invalid:
    foreground …" (run 44354825315) masked the marker/step that really failed."""
    import report

    out = make_out([("driver-cash-request", "FAIL", "marker absent")],
                   [{"name": "41-driver-cash-request", "point": "driver-cash-request",
                     "status": "TIMEOUT", "marker": "+Confirmer le paiement",
                     "failed_spec": "+Confirmer le paiement",
                     "fg_pkg": "com.google.android.apps.nexuslauncher"}])
    res = report.build(out, p0={"driver-cash-request"})
    row = [p for p in res["points"] if p["point"] == "driver-cash-request"][0]
    assert row["result"] == "FAIL", row
    assert "capture invalid" not in row["detail"], row
    assert "TIMEOUT" in row["detail"], row
    assert "Confirmer le paiement" in row["detail"], row


@case
def test_non_match_capture_is_never_promoted_to_evidence():
    """Same run, the other direction: a TIMEOUT capture must not close a P0 point."""
    import report

    out = make_out([("rider-home-after", "PASS", "row claims pass")],
                   [{"name": "31-rider-home-after", "point": "rider-home-after",
                     "status": "TIMEOUT", "marker": "+Confirmer le lieu"}])
    res = report.build(out, p0={"rider-home-after"})
    row = [p for p in res["points"] if p["point"] == "rider-home-after"][0]
    assert row["result"] == "FAIL", row
    assert "no MATCH capture" in row["detail"], row


@case
def test_match_capture_with_dump_foreground_proof_is_valid():
    """`fg_source=ui-dump` is the documented arbitration; it must not invalidate."""
    import report

    out = make_out([("rider-home-loaded", "PASS", "ok")],
                   [{"name": "14-rider-home", "point": "rider-home-loaded",
                     "marker": "+Où allez-vous", "fg_pkg": RIDER_PKG,
                     "fg_source": "ui-dump"}])
    res = report.build(out, p0={"rider-home-loaded"})
    row = [p for p in res["points"] if p["point"] == "rider-home-loaded"][0]
    assert row["result"] == "PASS", row


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


@case
def test_diag_rider_pusher_is_a_measurement_not_a_gate():
    """Run 44441344957 : le rider n'a jamais rendu la course (captures 26/27/29/30 =
    écran de composition) et le tail CI est borné à 80 lignes. `diag-rider-pusher` est émis
    comme résultat pour survivre au tail ; il ne doit être ni P0 ni un point UI, et
    report.py doit l'afficher sans le compter comme warn/skip."""
    import report

    assert "diag-rider-pusher" not in report.P0_DEFAULT, report.P0_DEFAULT
    assert "diag-rider-pusher" not in report.UI_POINTS, report.UI_POINTS
    row = "diag-rider-pusher|PASS|n=2 pusher=5 fg=sn.galass.galassrider"
    rows = report.parse_rows(row)
    assert rows == [{"point": "diag-rider-pusher", "result": "PASS",
                     "detail": "n=2 pusher=5 fg=sn.galass.galassrider"}], rows
    fail_row = "diag-rider-pusher|FAIL|n=0 pusher=0 fg=com.android.launcher"
    fail_rows = report.parse_rows(fail_row)
    assert fail_rows[0]["result"] == "FAIL" and "n=0" in fail_rows[0]["detail"], fail_rows
    out = make_out([("diag-rider-pusher", "PASS", "n=2 pusher=5 fg=sn.galass.galassrider")])
    res = report.build(out, p0={"rider-home-loaded"})
    diag = [p for p in res["points"] if p["point"] == "diag-rider-pusher"]
    assert diag and diag[0]["result"] == "PASS", res["points"]
    assert diag[0]["detail"] == "n=2 pusher=5 fg=sn.galass.galassrider", diag
    assert res["warn_count"] == 0 and res["skip_count"] == 0, res
    summary = open(os.path.join(out, "summary.md"), encoding="utf-8").read()
    assert "diag-rider-pusher" in summary, summary


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
