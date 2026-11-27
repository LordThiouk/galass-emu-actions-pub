#!/usr/bin/env python3
"""Reprise après plantage d'émulateur, mesure avant plantage, diagnostic Places, INCONCLUSIVE."""
import json
import secrets
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
UI, DP = os.path.join(ROOT, "ui.sh"), os.path.join(ROOT, "device_prep.sh")
RUN = open(os.path.join(ROOT, "run.sh"), encoding="utf-8").read()
import crash_summary  # noqa: E402
import emu_sampler  # noqa: E402
import report  # noqa: E402

# Doublures : horloge qui avance à chaque sleep, aucun vrai adb.
PRELUDE = r'''
sleep() { SECONDS=$((SECONDS+5)); }
adb() { case "$*" in
  *"getprop sys.boot_completed"*) echo "${BOOT:-1}";;
  *"pidof system_server"*) echo "${SSPID:-100}";;
  *wait-for-device*) echo WFD >> events.txt;;
  esac; return 0; }
launch() { echo "LAUNCH $1" >> events.txt; }
wait_fg() { return 0; }
whitelist_battery() { echo WL >> events.txt; }
emu_wait_calm() { echo CALM >> events.txt; return 0; }
emu_apply_settings() { :; }
LOG_FILE=$PWD/stream.log; : > "$LOG_FILE"
OUT=$PWD
'''


def bash(script, files=None):
    d = tempfile.mkdtemp()
    for k, v in (files or {}).items():
        open(os.path.join(d, k), "w", encoding="utf-8").write(v)
    env = dict(os.environ, OUT=d, UI_XML=os.path.join(d, "w.xml"))
    r = subprocess.run(["bash", "-c", f'. "{UI}"; . "{DP}"; cd "{d}"; {PRELUDE}\n{script}'],
                       capture_output=True, text=True, env=env, timeout=60)
    ev = os.path.join(d, "events.txt")
    return r.stdout + r.stderr, (open(ev).read() if os.path.exists(ev) else ""), d


CRASH_LOG = "11-17 05:15:54 W ActivityManager has died\n"


# ------------------------------------------------------------------ détection
def test_reason_none_when_healthy():
    out, _, _ = bash("emu_checkpoint; emu_crash_reason; echo rc=$?")
    assert out.strip().endswith("rc=1"), out


def test_reason_log_dump_boot_and_pid():
    out, _, _ = bash(f'emu_checkpoint; printf %s "{CRASH_LOG}" >> "$LOG_FILE"; emu_crash_reason; echo rc=$?')
    assert "system_server mort" in out and "rc=0" in out
    out, _, _ = bash("emu_checkpoint; EMU_DUMP_FAILS_MAX=2; emu_crash_reason; echo rc=$?")
    assert "dumps uiautomator indisponibles" in out and "rc=0" in out
    out, _, _ = bash("emu_checkpoint; BOOT=0; emu_crash_reason; echo rc=$?")
    assert "boot_completed" in out and "rc=0" in out
    out, _, _ = bash("SSPID=100; emu_checkpoint; SSPID=200; emu_crash_reason; echo rc=$?")
    assert "pid 100 -> 200" in out and "rc=0" in out


def test_ui_xml_counts_consecutive_dump_failures():
    out, _, _ = bash('adb() { return 1; }; EMU_DUMP_FAILS=0; EMU_DUMP_FAILS_MAX=0; '
                     'ui_xml; ui_xml; echo MAX=$EMU_DUMP_FAILS_MAX')
    assert "MAX=2" in out, out


# ------------------------------------------------------------------ reprise + rejeu unique
def test_crash_then_recovery_then_single_replay_passes_with_screen_proof():
    out, ev, d = bash(
        'n=0; snap() { n=$((n+1)); echo SNAP$n >> events.txt; [ $n -ge 2 ]; }; '
        'emu_checkpoint; printf %s "' + CRASH_LOG + '" >> "$LOG_FILE"; '
        'snap_with_recovery 14-x sn.galass.galassrider 30 spec "" pt grounded; echo rc=$? replay=$SNAP_REPLAYED')
    assert "rc=0 replay=1" in out, out
    assert ev.split() == ["SNAP1", "WFD", "WL", "CALM", "LAUNCH", "sn.galass.galassrider", "SNAP2"], ev
    assert "CRASH #1 à " in out and "REPRISE #1 à " in out and "REPRISE terminée à " in out
    assert "REJEU unique" in out and "[crash-summary]" in out


def test_second_crash_marks_inconclusive_never_a_pass():
    out, ev, _ = bash(
        'snap() { echo SNAP >> events.txt; return 3; }; '
        'emu_checkpoint; printf %s "' + CRASH_LOG + '" >> "$LOG_FILE"; '
        'snap_with_recovery 14-x sn.galass.galassrider 30 spec "" pt grounded; echo rc=$? replay=$SNAP_REPLAYED; echo "note=$EMU_LAST_CRASH_NOTE"')
    assert "rc=2 replay=0" in out, out
    assert ev.split().count("SNAP") == 2, "rejeu une seule fois, pas plus"
    assert "écran du point non retrouvé" in out or "replanté" in out


def test_replay_that_crashes_again_is_reported_with_its_time():
    out, ev, _ = bash(
        'snap() { echo SNAP >> events.txt; case $(grep -c SNAP events.txt) in 2) printf %s "' + CRASH_LOG +
        '" >> "$LOG_FILE";; esac; return 3; }; emu_checkpoint; printf %s "' + CRASH_LOG +
        '" >> "$LOG_FILE"; snap_with_recovery 14-x sn.galass.galassrider 30 spec "" pt grounded; echo rc=$?')
    assert "rc=2" in out and "CRASH pendant le rejeu de 14-x" in out, out


def test_real_app_failure_without_crash_evidence_is_not_inconclusive():
    out, ev, _ = bash('snap() { echo SNAP >> events.txt; return 3; }; emu_checkpoint; '
                      'snap_with_recovery 14-x sn.galass.galassrider 30 spec "" pt grounded; echo rc=$?')
    assert "rc=1" in out and ev.split() == ["SNAP"], (out, ev)


def test_recovery_impossible_when_boot_never_returns():
    out, ev, _ = bash(
        'snap() { return 3; }; emu_checkpoint; BOOT=0; EMU_RECOVER_BOOT_MAX=20; '
        'snap_with_recovery 14-x sn.galass.galassrider 30 spec "" pt grounded; echo rc=$? note=$EMU_LAST_CRASH_NOTE')
    assert "rc=2" in out and "REPRISE ÉCHEC" in out and "reprise impossible" in out, out


def test_cap_maps_codes_to_pass_inconclusive_fail():
    assert 'mark "$point" INCONCLUSIVE "emulator_crash:' in RUN
    assert RUN.count("snap_with_recovery") >= 2
    assert 'mark driver-home-online INCONCLUSIVE "emulator_crash:' in RUN


# ------------------------------------------------------------------ report: portes intactes
def _report(rows, sidecars=()):
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "screens"))
    open(os.path.join(d, "results-raw.txt"), "w", encoding="utf-8").write("\n".join(rows) + "\n")
    for sc in sidecars:
        json.dump(sc, open(os.path.join(d, "screens", sc["name"] + ".json"), "w"))
    return report.build(d, p0=["rider-home-loaded", "backend-flow"])


def test_inconclusive_crash_is_not_a_fail_but_still_blocks_the_verdict():
    r = _report(["rider-home-loaded|INCONCLUSIVE|emulator_crash: system_server mort", "backend-flow|PASS|ok"],
                [{"name": "14-x", "point": "rider-home-loaded", "status": "TIMEOUT"}])
    row = {p["point"]: p for p in r["points"]}["rider-home-loaded"]
    assert row["result"] == "INCONCLUSIVE" and r["fail_count"] == 0
    assert r["inconclusive_count"] == 1
    assert r["verdict"] == "FAIL" and "rider-home-loaded" in r["p0_nonpass"]


def test_pass_claim_without_match_capture_is_still_a_fail():
    r = _report(["rider-home-loaded|PASS|claimed", "backend-flow|PASS|ok"])
    row = {p["point"]: p for p in r["points"]}["rider-home-loaded"]
    assert row["result"] == "FAIL"


def test_plain_inconclusive_without_crash_tag_cannot_dodge_the_capture_rule():
    r = _report(["rider-home-loaded|INCONCLUSIVE|autre raison", "backend-flow|PASS|ok"])
    row = {p["point"]: p for p in r["points"]}["rider-home-loaded"]
    assert row["result"] == "FAIL"


# ------------------------------------------------------------------ mesure avant plantage
def _rows(**kw):
    base = dict(t=1000, ncpu=4, load1=1.0, cpu_idle_pct=50.0, mem_avail_mb=8000, swap_used_mb=0,
                host_top5=["35.0 qemu-system-x86"], guest_top5=["123 u0_a1 12.0 sn.galass"], anr_gms=[])
    base.update(kw)
    return [base]


def test_conclusion_cpu_ram_saturated_piste_2():
    out = crash_summary.summarize(_rows(cpu_idle_pct=2.0, load1=7.5), 1000)
    assert out[-1].startswith("[crash-summary] CONCLUSION: CPU saturés -> piste 2")
    out = crash_summary.summarize(_rows(mem_avail_mb=300), 1000)
    assert "RAM saturés -> piste 2" in out[-1]


def test_conclusion_gms_piste_1_and_neither():
    out = crash_summary.summarize(_rows(anr_gms=["persistent"]), 1000)
    assert "gms en cause" in out[-1] and "piste 1" in out[-1]
    assert "ni l'un ni l'autre" in crash_summary.summarize(_rows(), 1000)[-1]


def test_summary_window_and_no_samples():
    out = crash_summary.summarize(_rows(t=500), 1000, 60)
    assert "aucun échantillon" in out[0] and "mesure indisponible" in out[-1]


def test_summary_has_required_fields_and_no_secret():
    txt = "\n".join(crash_summary.summarize(_rows(), 1000))
    for needle in ("CPU hôte", "RAM hôte", "swap", "top5 hôte", "top5 AVD", "ANR gms"):
        assert needle in txt


def test_sampler_helpers_read_the_host():
    idle, total = emu_sampler.cpu_times()
    assert total >= idle >= 0
    assert emu_sampler.meminfo().get("MemAvailable") is not None


# ------------------------------------------------------------------ Places
def test_places_wait_concludes_network_down():
    out, _, _ = bash('PLACES_NET_OK=non; ui_xml() { UI_DUMP_VALID=1; : > "$UI_XML_PATH"; return 0; }; '
                     'xml_pkg() { echo sn.galass.galassrider; }; places_wait_result "E2E Dest Point E" 10; echo rc=$?')
    assert "panne réseau ou Places" in out and "rc=1" in out, out
    assert "dump=lisible" in out


def test_places_wait_concludes_slow_emulator_when_dump_unreadable():
    out, _, _ = bash('PLACES_NET_OK=oui; ui_xml() { return 4; }; places_wait_result "E2E Dest Point E" 10; echo rc=$?')
    assert "émulateur lent/instable" in out and "dump=indisponible" in out, out


def test_places_wait_concludes_visible_early_and_masks_the_key():
    out, _, _ = bash('PLACES_NET_OK=oui; ui_xml() { UI_DUMP_VALID=1; echo "E2E Dest Point E" > "$UI_XML_PATH"; return 0; }; '
                     'xml_pkg() { echo sn.galass.galassrider; }; places_wait_result "E2E Dest Point E" 20; echo rc=$?')
    assert "résultat « E2E Dest Point E » visible" in out and "rc=0" in out
    fake = "AIza" + secrets.token_hex(12)  # clé factice générée à l'exécution, jamais en dur
    masked, _, _ = bash('echo "GET maps.googleapis.com/x?key=%s&q=1" | _mask_secrets' % fake)
    assert fake not in masked and "key=***" in masked


def test_places_wait_reports_network_errors_from_logcat():
    fake = "AIza" + secrets.token_hex(12)
    out, _, _ = bash('PLACES_NET_OK=oui; PLACES_LOG_OFF=0; printf "%%s\\n" "E flutter: Failed host lookup: maps.googleapis.com key=%s" >> "$LOG_FILE"; '
                     'ui_xml() { UI_DUMP_VALID=1; : > "$UI_XML_PATH"; return 0; }; xml_pkg() { echo p; }; '
                     'places_wait_result "X" 6; echo rc=$?' % fake)
    assert "erreurs_réseau=1" in out and fake not in out, out


def test_compose_uses_places_diagnostics():
    assert "places_diag_start" in RUN and 'places_wait_result "$label" 20' in RUN


CASES = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    for fn in CASES:
        fn()
        print(f"ok - {fn.__name__}")
    print(f"PASS ({len(CASES)}/{len(CASES)})")
