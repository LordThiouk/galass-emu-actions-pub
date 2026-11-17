#!/usr/bin/env python3
"""Steps 14/33 : dumpsys peut renvoyer le launcher alors que le dump uiautomator montre
l'app. Contrat : le dump fait foi ; le driver est mis en ligne AVANT le lancement ; la
course est composée par l'UI (ride_source=ui), l'API n'est qu'un repli marqué FAIL."""
import os
import re
import subprocess
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "ui.sh")
RUN = open(os.path.join(ROOT, "run.sh"), encoding="utf-8").read()
API = open(os.path.join(ROOT, "api_flow.py"), encoding="utf-8").read()

XML_APP = ('<?xml version="1.0"?><hierarchy rotation="0">'
           '<node package="com.android.systemui" text="" bounds="[0,0][1,1]"/>'
           '<node package="sn.galass.galassrider" text="Où allez-vous ?" bounds="[0,0][9,9]"/>'
           + " " * 400 + '</hierarchy>')


def sh(script, xml):
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "w.xml")
        open(p, "w", encoding="utf-8").write(xml)
        env = dict(os.environ, OUT=d, UI_XML=p)
        return subprocess.run(
            ["bash", "-c", f'. "{UI}"; UI_DUMP_VALID=1; {script}'],
            capture_output=True, text=True, env=env, cwd=d).stdout.strip()


def test_xml_pkg_ignores_systemui():
    assert sh("xml_pkg", XML_APP) == "sn.galass.galassrider"


def test_xml_pkg_empty_without_valid_dump():
    out = sh("UI_DUMP_VALID=0; xml_pkg", XML_APP)
    assert out == ""


def test_wait_ui_accepts_app_when_dumpsys_says_launcher():
    script = ('adb() { return 0; }; ui_xml() { UI_DUMP_VALID=1; return 0; }; '
              'clear_sys() { :; }; fg_pkg() { echo com.google.android.apps.nexuslauncher; }; '
              'fg_diag() { :; }; '
              'wait_ui sn.galass.galassrider 3 "+Où allez-vous"; echo rc=$?')
    assert sh(script, XML_APP).endswith("rc=0")


def test_wait_ui_still_fails_when_xml_shows_other_app():
    xml = XML_APP.replace("sn.galass.galassrider", "com.google.android.apps.nexuslauncher")
    script = ('adb() { return 0; }; ui_xml() { UI_DUMP_VALID=1; return 0; }; '
              'clear_sys() { :; }; fg_pkg() { echo com.google.android.apps.nexuslauncher; }; '
              'fg_diag() { :; }; '
              'wait_ui sn.galass.galassrider 3 "+Où allez-vous"; echo rc=$?')
    assert sh(script, xml).endswith("rc=3")


def test_driver_online_before_launch():
    online = RUN.find("api online")
    launch = RUN.find('launch "$RIDER_PKG"; wait_fg')
    assert -1 < online < launch, "le driver doit être mis en ligne avant le lancement des apps"


def test_online_step_requires_backend_confirmation():
    m = re.search(r'elif step == "online":.*?(?=\nelif step ==)', API, re.S)
    assert m and "sys.exit(0 if ok else 3)" in m.group(0)


def test_ui_compose_runs_before_api_pick_and_never_creates():
    assert RUN.find("compose_ride_ui ||") < RUN.find("api pick >>")
    body = re.search(r"^compose_ride_ui\(\) \{.*?^\}", RUN, re.M | re.S).group(0)
    assert "api create" not in body and "api pick" not in body


def test_markers_match_rider_strings():
    assert "Où allez-vous" in RUN and "Partons pour une course" in RUN


CASES = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    for fn in CASES:
        fn()
        print(f"ok - {fn.__name__}")
    print(f"PASS ({len(CASES)}/{len(CASES)})")
