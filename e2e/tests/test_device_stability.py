#!/usr/bin/env python3
"""Whitelist batterie, tap par nœud vérifié, détection d'émulateur instable."""
import os
import re
import subprocess
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = open(os.path.join(ROOT, "run.sh"), encoding="utf-8").read()
REPORT = open(os.path.join(ROOT, "report.py"), encoding="utf-8").read()
WF = open(os.path.join(os.path.dirname(ROOT), ".github/workflows/artemis-staging-e2e.yml"),
          encoding="utf-8").read()
UI, DP, TAP = (os.path.join(ROOT, n) for n in ("ui.sh", "device_prep.sh", "tap_node.py"))

# Libellé non cliquable dans un parent cliquable : le tap doit viser le parent.
XML = ('<?xml version="1.0"?><hierarchy>'
       '<node class="android.view.View" clickable="true" visible-to-user="true" bounds="[0,500][1080,700]">'
       '<node class="android.widget.TextView" clickable="false" visible-to-user="true" text="Où allez-vous ?" bounds="[100,560][400,640]"/>'
       '</node></hierarchy>')
XML_INPUT = ('<?xml version="1.0"?><hierarchy><node class="android.widget.EditText" '
             'bounds="[0,0][1080,100]" text=""/></hierarchy>')


def tmp(content, name="d.xml"):
    d = tempfile.mkdtemp()
    p = os.path.join(d, name)
    open(p, "w", encoding="utf-8").write(content)
    return p


def tap_node(xml, label):
    return subprocess.run(["python3", TAP, tmp(xml), label], capture_output=True, text=True).stdout.strip()


def test_tap_targets_clickable_parent_not_the_label():
    assert tap_node(XML, "Où allez-vous ?") == "540 600 clickable"


def test_tap_node_absent_label_returns_nothing():
    assert tap_node(XML, "Inexistant") == ""


def test_has_input_detects_edittext():
    assert subprocess.run(["python3", TAP, "--has-input", tmp(XML_INPUT)]).returncode == 0
    assert subprocess.run(["python3", TAP, "--has-input", tmp(XML)]).returncode == 1


def run_bash(script):
    d = tempfile.mkdtemp()
    env = dict(os.environ, OUT=d, UI_XML=os.path.join(d, "w.xml"))
    return subprocess.run(["bash", "-c", f'. "{UI}"; . "{DP}"; {script}'],
                          capture_output=True, text=True, env=env, cwd=d, timeout=60)


def test_whitelist_runs_deviceidle_and_appops_before_trace():
    r = run_bash('CALLS=; adb() { echo "$*" >> calls.txt; }; whitelist_battery sn.galass.galassdriver; cat calls.txt')
    assert "dumpsys deviceidle whitelist +sn.galass.galassdriver" in r.stdout
    assert "appops set sn.galass.galassdriver RUN_ANY_IN_BACKGROUND allow" in r.stdout


def test_tap_node_verified_gives_up_after_three_taps_when_screen_never_changes():
    r = run_bash(
        'adb() { case "$*" in *"input tap"*) echo TAP >> taps.txt;; esac; return 0; }; '
        'ui_xml() { cp "%s" "$UI_XML_PATH"; UI_DUMP_VALID=1; return 0; }; '
        'wait_ui() { return 0; }; sleep() { :; }; '
        'tap_node_verified sn.galass.galassrider "Où allez-vous ?"; echo rc=$?; wc -l < taps.txt'
        % tmp(XML))
    assert "rc=1" in r.stdout and r.stdout.strip().endswith("3"), r.stdout + r.stderr


def test_tap_node_verified_ok_when_input_appears():
    r = run_bash(
        'n=0; adb() { return 0; }; '
        'ui_xml() { n=$((n+1)); if [ $n -le 1 ]; then cp "%s" "$UI_XML_PATH"; else cp "%s" "$UI_XML_PATH"; fi; UI_DUMP_VALID=1; return 0; }; '
        'wait_ui() { return 0; }; sleep() { :; }; '
        'tap_node_verified sn.galass.galassrider "Où allez-vous ?"; echo rc=$?' % (tmp(XML), tmp(XML_INPUT)))
    assert "rc=0" in r.stdout, r.stdout + r.stderr


def test_dismiss_bg_dialog_taps_allow_found_in_dump():
    dlg = ('<?xml version="1.0"?><hierarchy><node package="com.android.settings" text="Let app always run in background?" bounds="[0,0][9,9]"/>'
           '<node package="com.android.settings" text="Allow" bounds="[100,100][200,200]"/>' + " " * 400 + '</hierarchy>')
    r = run_bash('adb() { return 0; }; ui_xml() { cp "%s" "$UI_XML_PATH"; UI_DUMP_VALID=1; return 0; }; '
                 'tap_exact() { echo TAPPED:$1 > tapped.txt; return 0; }; sleep() { :; }; dismiss_bg_dialog; echo rc=$?; cat tapped.txt' % tmp(dlg))
    assert "TAPPED:Allow" in r.stdout and "rc=0" in r.stdout, r.stdout + r.stderr


def test_unstable_scan_flags_activitymanager_died_and_anr_system():
    bad = tmp("11-17 05:15:54 W ActivityManager has died\n", "l.log")
    r = run_bash(f'emu_unstable_scan "{bad}"; echo rc=$?')
    assert "rc=0" in r.stdout
    bad2 = tmp("ANR in system (broadcast UPDATE_LANG_ID)\n", "l2.log")
    assert "rc=0" in run_bash(f'emu_unstable_scan "{bad2}"; echo rc=$?').stdout


def test_unstable_scan_clean_log_is_not_unstable():
    ok = tmp("I normal logcat line\nANR in com.google.android.gms.persistent\n", "ok.log")
    assert "rc=1" in run_bash(f'emu_unstable_scan "{ok}"; echo rc=$?').stdout


def test_report_labels_unstable_emulator():
    d = tempfile.mkdtemp()
    open(os.path.join(d, "emulator-unstable.flag"), "w").close()
    r = subprocess.run(["python3", os.path.join(ROOT, "report.py"), d], capture_output=True, text=True)
    assert "emulator_unstable=1" in r.stdout
    assert "ÉMULATEUR INSTABLE" in open(os.path.join(d, "summary.md"), encoding="utf-8").read()


def test_run_sh_ordering_calm_then_whitelist_then_launch():
    calm = RUN.find("emu_wait_calm")
    wl = RUN.find('whitelist_battery "$DRIVER_PKG"')
    launch = RUN.find('launch "$RIDER_PKG"; wait_fg')
    assert -1 < calm < wl < launch


def test_workflow_gives_avd_more_resources():
    assert "ram-size: 6144" in WF and "cores: 3" in WF


CASES = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    for fn in CASES:
        fn()
        print(f"ok - {fn.__name__}")
    print(f"PASS ({len(CASES)}/{len(CASES)})")
