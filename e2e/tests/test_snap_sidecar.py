#!/usr/bin/env python3
"""TDD spec — the sidecar carries the foreground proof of the screen that was captured.

Run with `python3 e2e/tests/test_snap_sidecar.py`. No emulator, no network: `adb`,
`ui_xml` and `fg_pkg` are stubs, the dump is a fixture.

Why this file exists (run 44354825315): `report.py` marks a capture INVALID when
`fg_pkg != expected_pkg(point)`. The sidecar took its `fg_pkg` from a *fresh*
`dumpsys` read taken after the second dump — so

  * a transient re-dump failure (UI_DUMP_VALID=0) left `xml_pkg()` empty, the
    arbitration never applied, and a capture `snap` had just accepted went out with
    the launcher as its foreground — plus its uiautomator dump truncated to 0 byte;
  * a capture that legitimately failed its marker (TIMEOUT) also carried the raw
    launcher, so `report.py` reported "capture invalid: foreground … != package"
    instead of "marker absent", which reads like a harness bug.

Contract under test: the sidecar records the app proven by the dump that was used
(the marker dump first, the post-capture dump second), says where that proof came
from (`fg_source`), and never destroys the dump it captured.
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "ui.sh")
RIDER_PKG = "sn.galass.galassrider"

# Le package de l'app est dans le dump, pas dans le nom du fixture : xml_pkg le lit.
XML_APP = ('<?xml version="1.0"?><hierarchy rotation="0">'
           '<node package="com.android.systemui" text="" bounds="[0,0][1,1]" visible-to-user="true"/>'
           '<node package="sn.galass.galassrider" text="Où allez-vous ?" bounds="[0,0][900,120]" visible-to-user="true"/>'
           '<node package="sn.galass.galassrider" text="Confirmer l\'arrêt" bounds="[0,120][900,240]" visible-to-user="true"/>'
           + " " * 400 + '</hierarchy>')
XML_LAUNCHER = XML_APP.replace("sn.galass.galassrider", "com.google.android.apps.nexuslauncher")

LAUNCHER = "com.google.android.apps.nexuslauncher"
CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def run_snap(xml, spec, dumpsys=LAUNCHER, redump_fails=False, tmo=3):
    """Source ui.sh with stubs and run one snap(); return (sidecar, out_dir, xml_bytes)."""
    d = tempfile.mkdtemp(prefix="e2e-snap-")
    xml_path = os.path.join(d, "w.xml")
    with open(xml_path, "w", encoding="utf-8") as fh:
        fh.write(xml)
    counter = os.path.join(d, "dumps")
    stubs = f'''
adb() {{ case "$*" in *screencap*) printf '\\x89PNG\\r\\n\\x1a\\n0123456789' ;; *) return 0 ;; esac; }}
clear_sys() {{ :; }}
fg_pkg() {{ echo {dumpsys}; }}
ui_xml() {{
  local n; n=$(cat "{counter}" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "{counter}"
  if [ "{'1' if redump_fails else '0'}" = "1" ] && [ "$n" -gt 1 ]; then
    UI_DUMP_VALID=0; UI_DUMP_HASH=""; rm -f "$UI_XML_PATH"; return 4
  fi
  UI_DUMP_VALID=1; UI_DUMP_HASH=hash$n; UI_DUMP_BYTES=$(wc -c < "$UI_XML_PATH"); return 0
}}
'''
    script = (f'. "{UI}"\n{stubs}\n'
              f'snap cap1 {RIDER_PKG} {tmo} \'{spec}\' "" rider-home-loaded\n'
              'echo rc=$?')
    env = dict(os.environ, OUT=d, UI_XML=xml_path)
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env=env, cwd=d)
    path = os.path.join(d, "screens", "cap1.json")
    side = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            side = json.load(fh)
    ui_path = os.path.join(d, "ui", "cap1.xml")
    xml_bytes = os.path.getsize(ui_path) if os.path.isfile(ui_path) else -1
    return side, d, xml_bytes, proc


@case
def test_match_keeps_the_dump_when_the_redump_fails():
    """A transient re-dump failure must not erase the screen that just matched."""
    side, _, xml_bytes, proc = run_snap(XML_APP, "+Où allez-vous", redump_fails=True)
    assert side.get("status") == "MATCH", (side, proc.stderr, proc.stdout)
    assert xml_bytes > 400, f"dump effacé par le re-dump raté ({xml_bytes} octets)"
    assert side.get("labels"), "les libellés de l'écran capturé sont perdus"


@case
def test_match_records_the_app_proven_by_the_dump_not_the_launcher():
    side, _, _, proc = run_snap(XML_APP, "+Où allez-vous", redump_fails=True)
    assert side.get("fg_pkg") == RIDER_PKG, (side.get("fg_pkg"), proc.stderr)
    assert side.get("fg_source") == "ui-dump", side.get("fg_source")


@case
def test_match_with_a_clean_redump_still_records_the_app():
    side, _, _, _ = run_snap(XML_APP, "+Où allez-vous")
    assert side.get("status") == "MATCH", side.get("status")
    assert side.get("fg_pkg") == RIDER_PKG, side.get("fg_pkg")
    assert side.get("fg_source") == "ui-dump", side.get("fg_source")


@case
def test_timeout_records_the_app_so_the_reason_stays_the_marker():
    """The marker missed but the app IS on screen: fg must say the app, status stays TIMEOUT."""
    side, _, _, _ = run_snap(XML_APP, "+libelle absent de ecran", tmo=2)
    assert side.get("status") == "TIMEOUT", side.get("status")
    assert side.get("fg_pkg") == RIDER_PKG, side.get("fg_pkg")
    assert side.get("fg_source") == "ui-dump", side.get("fg_source")


@case
def test_other_app_is_never_relabelled():
    """Honesty guard: when the dump shows another app, the sidecar says so."""
    side, _, _, _ = run_snap(XML_LAUNCHER, "+Où allez-vous", tmo=2)
    assert side.get("fg_pkg") == LAUNCHER, side.get("fg_pkg")
    assert side.get("fg_source") == "dumpsys", side.get("fg_source")


@case
def test_dumpsys_app_needs_no_arbitration():
    side, _, _, _ = run_snap(XML_APP, "+Où allez-vous", dumpsys=RIDER_PKG)
    assert side.get("fg_pkg") == RIDER_PKG, side.get("fg_pkg")
    assert side.get("fg_source") == "dumpsys", side.get("fg_source")


def main():
    failed = 0
    for fn in CASES:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
        else:
            print(f"ok   {fn.__name__}")
    print(f"{len(CASES) - failed}/{len(CASES)} ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
