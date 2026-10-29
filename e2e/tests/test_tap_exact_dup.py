"""tap_exact: un libellé en double (titre + bouton) tape le bouton cliquable."""
import os, subprocess, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

XML = ('<hierarchy><node text="Trouver un chauffeur" clickable="false" visible-to-user="true" bounds="[40,1700][560,1786]"/>'
       '<node text="Trouver un chauffeur" clickable="true" visible-to-user="true" bounds="[40,2100][900,2220]"/></hierarchy>')


def _run(xml):
    tmp = tempfile.mkdtemp()
    os.makedirs(f"{tmp}/ui"); bindir = f"{tmp}/bin"; os.makedirs(bindir)
    open(f"{tmp}/d.xml", "w").write(xml)
    open(f"{bindir}/adb", "w").write(f'#!/bin/sh\necho "$@" >> {tmp}/adb.log\n'); os.chmod(f"{bindir}/adb", 0o755)
    sh = (f'export OUT={tmp} PATH={bindir}:$PATH; source {ROOT}/ui.sh; '
          f'ui_xml() {{ UI_XML_PATH={tmp}/d.xml; return 0; }}; _ulog() {{ :; }}; tap_exact "Trouver un chauffeur"')
    r = subprocess.run(["bash", "-c", sh], capture_output=True, text=True)
    return r, open(f"{tmp}/adb.log").read() if os.path.exists(f"{tmp}/adb.log") else ""


def test_duplicate_label_taps_the_clickable_button_not_the_title():
    r, log = _run(XML)
    assert "input tap 470 2160" in log, (r.stderr, log)


def test_single_non_clickable_label_still_tapped():
    r, log = _run('<hierarchy><node text="Trouver un chauffeur" visible-to-user="true" bounds="[0,0][100,100]"/></hierarchy>')
    assert "input tap 50 50" in log, (r.stderr, log)
