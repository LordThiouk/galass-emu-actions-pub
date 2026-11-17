#!/usr/bin/env python3
"""Tests : fenêtre de prise en charge (dialog_confirm) et diagnostic du champ Places.
Sans émulateur : `adb` est un faux script sur PATH qui rejoue des dumps uiautomator."""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import dump_diag  # noqa: E402

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def node(i, text, bounds, cls="android.widget.TextView", clickable="false"):
    return ('<node index="%d" text="%s" class="%s" clickable="%s" visible-to-user="true" '
            'bounds="%s" />' % (i, text, cls, clickable, bounds))


def dump(*nodes):
    return ("<?xml version='1.0'?><hierarchy>" + "".join(nodes) + "</hierarchy>")


DIALOG = dump(node(0, "Prise en charge du client", "[100,800][900,900]"),
              node(1, "Annuler", "[100,1000][400,1100]", clickable="true"),
              node(2, "Confirmer", "[500,1000][900,1100]", clickable="true"))
AFTER = dump(node(0, "Terminer l'arrêt", "[100,1800][900,1900]", clickable="true"))


def run_dialog(frames, appear=6, gone=8):
    """frames : dumps servis dans l'ordre (le dernier se répète) ; retourne (rc, taps)."""
    tmp = tempfile.mkdtemp(prefix="e2e-dlg-")
    for i, f in enumerate(frames):
        open(os.path.join(tmp, "f%d.xml" % i), "w").write(f)
    taps = os.path.join(tmp, "taps")
    open(taps, "w").close()
    fake = os.path.join(tmp, "bin")
    os.mkdir(fake)
    with open(os.path.join(fake, "adb"), "w") as fh:
        fh.write('#!/bin/bash\necho "$@" >> %s\n' % taps)
    os.chmod(os.path.join(fake, "adb"), 0o755)
    script = r'''
set -u
export OUT="%(tmp)s"; export PATH="%(fake)s:$PATH"
. "%(ui)s"
n=0
ui_xml() { local f="%(tmp)s/f$n.xml"; [ -f "$f" ] || f="%(tmp)s/f%(last)d.xml"; n=$((n+1)); UI_XML_PATH="$f"; return 0; }
dialog_confirm pkg Confirmer Annuler %(appear)d %(gone)d
echo "rc=$?"
''' % dict(tmp=tmp, fake=fake, ui=os.path.join(ROOT, "ui.sh"), last=len(frames) - 1,
           appear=appear, gone=gone)
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    rc = int([l for l in p.stdout.splitlines() if l.startswith("rc=")][-1][3:])
    return rc, open(taps).read(), p.stdout + p.stderr


@case
def test_dialog_confirm_taps_confirmer_then_waits_for_it_to_vanish():
    rc, taps, out = run_dialog([DIALOG, DIALOG, AFTER])
    assert rc == 0, out
    assert "input tap 700 1050" in taps, taps      # centre de « Confirmer », pas « Annuler »
    assert "input tap 250 1050" not in taps, taps


@case
def test_dialog_confirm_reports_stuck_dialog():
    rc, taps, out = run_dialog([DIALOG], appear=4, gone=6)
    assert rc == 1, out


@case
def test_dialog_confirm_returns_2_when_no_dialog():
    rc, taps, out = run_dialog([AFTER], appear=3, gone=4)
    assert rc == 2, out
    assert taps.strip() == "", taps


@case
def test_dialog_confirm_ignores_lone_confirmer_without_annuler():
    only = dump(node(0, "Confirmer", "[500,1000][900,1100]", clickable="true"))
    rc, taps, out = run_dialog([only], appear=3, gone=4)
    assert rc == 2, out


@case
def test_dump_diag_reports_field_text_rows_and_nodes():
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "d.xml")
    open(p, "w").write(dump(
        node(0, "E2E Stop 1 Médina", "[50,200][900,300]", cls="android.widget.EditText"),
        node(1, "Médina, Dakar", "[50,400][900,500]", clickable="true"),
        node(2, "Fann, Dakar", "[50,520][900,620]", clickable="true"),
        node(3, "Où allez-vous ?", "[50,100][900,180]")))
    d = dump_diag.describe(p)
    assert d["fields"] == ["E2E Stop 1 Médina"], d
    assert d["rows"] == 2, d
    assert "Médina, Dakar" in d["texts"], d


@case
def test_dump_diag_no_results_and_unreadable():
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "d.xml")
    open(p, "w").write(dump(node(0, "", "[50,200][900,300]", cls="android.widget.EditText")))
    assert dump_diag.describe(p)["rows"] == 0
    open(p, "w").write("<not xml")
    assert dump_diag.describe(p)["readable"] is False


@case
def test_places_wait_result_logs_field_diagnostic():
    t = open(os.path.join(ROOT, "device_prep.sh"), encoding="utf-8").read()
    assert t.count("dump_diag.py") >= 2 and "places champ" in t


if __name__ == "__main__":
    ok = 0
    for c in CASES:
        try:
            c()
            print("ok  ", c.__name__)
            ok += 1
        except Exception as e:  # noqa: BLE001
            print("FAIL", c.__name__ + ":", type(e).__name__, e)
    print("%d/%d ui-label cases passed" % (ok, len(CASES)))
    sys.exit(0 if ok == len(CASES) else 1)
