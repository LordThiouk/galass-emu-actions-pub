#!/usr/bin/env python3
"""Tests : filet après un repli API d'acceptation — tape la carte « Course en cours » du
tableau de bord Driver pour forcer l'ouverture de l'écran détail (RideDetailsController),
puis attend un appel driver/rides/details dans le logcat avant de continuer.
Sans émulateur : `adb` est un faux script (journal `taps`), le logcat est rejoué depuis un fichier."""
import os
import subprocess
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def node(i, text, bounds, clickable="false"):
    return ('<node index="%d" text="%s" class="android.widget.TextView" clickable="%s" '
            'visible-to-user="true" bounds="%s" />' % (i, text, clickable, bounds))


def dump(*nodes):
    return "<?xml version='1.0'?><hierarchy>" + "".join(nodes) + "</hierarchy>"


CARD = dump(node(0, "En ligne", "[0,0][100,100]"),
            node(1, "Course en cours", "[42,900][1038,1050]", clickable="true"))
HOME_NO_CARD = dump(node(0, "En ligne", "[0,0][100,100]"))

LC_WITH_DETAILS = ("11-23 00:14:00.000  6964  6964 I flutter : "
                    "url--------------http://x/api/driver/rides/details/313\n")
LC_WITHOUT_DETAILS = ("11-23 00:14:00.000  6964  6964 I flutter : "
                       "url--------------http://x/api/driver/dashboard?page=1\n")


def harness(tmp, frames, logcat_lines, body, wait=20):
    for i, f in enumerate(frames):
        open(os.path.join(tmp, "f%d.xml" % i), "w").write(f)
    open(os.path.join(tmp, "lc.txt"), "w").write(logcat_lines)
    taps = os.path.join(tmp, "taps")
    open(taps, "w").close()
    fake = os.path.join(tmp, "bin")
    os.makedirs(fake, exist_ok=True)
    with open(os.path.join(fake, "adb"), "w") as fh:
        fh.write('#!/bin/bash\necho "$@" >> %s\n' % taps)
    os.chmod(os.path.join(fake, "adb"), 0o755)
    script = r'''
set -u
export OUT="%(tmp)s"; export DRIVER_DETAILS_WAIT=%(wait)d
export PATH="%(fake)s:$PATH"; mkdir -p "$OUT/screens" "$OUT/diag"
. "%(ui)s"
sleep() { :; }
FX=0
ui_xml() { local f="%(tmp)s/f$FX.xml"; [ -f "$f" ] || f="%(tmp)s/f%(last)d.xml"; FX=$((FX+1)); UI_XML_PATH="$f"; return 0; }
app_logcat_since() { cat "%(tmp)s/lc.txt"; }
%(body)s
''' % dict(tmp=tmp, fake=fake, ui=os.path.join(ROOT, "ui.sh"), last=len(frames) - 1,
           wait=wait, body=body)
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    rc = [l for l in p.stdout.splitlines() if l.startswith("rc=")]
    return (int(rc[-1][3:]) if rc else None), open(taps).read(), p.stdout + p.stderr


CALL = 'driver_open_ride_via_card pkg\necho "rc=$?"'


@case
def test_taps_card_then_sees_rides_details_and_returns_0():
    tmp = tempfile.mkdtemp(prefix="e2e-card-")
    rc, taps, out = harness(tmp, [CARD], LC_WITH_DETAILS, CALL)
    assert rc == 0, out
    assert "input tap 540 975" in taps, taps
    assert "driver/rides/details vu" in out, out


@case
def test_card_absent_returns_2_without_waiting_for_logcat():
    tmp = tempfile.mkdtemp(prefix="e2e-card-")
    rc, taps, out = harness(tmp, [HOME_NO_CARD], LC_WITH_DETAILS, CALL)
    assert rc == 2, out
    assert "input tap" not in taps, taps


@case
def test_card_tapped_but_no_rides_details_warns_and_returns_1():
    tmp = tempfile.mkdtemp(prefix="e2e-card-")
    rc, taps, out = harness(tmp, [CARD], LC_WITHOUT_DETAILS, CALL, wait=8)
    assert rc == 1, out
    assert "input tap 540 975" in taps, taps
    assert "WARN" in out and "Course en cours" in out, out


@case
def test_run_sh_sets_accept_via_api_flag_in_both_fallback_branches():
    run = open(os.path.join(ROOT, "run.sh"), encoding="utf-8").read()
    marks = [i for i in range(len(run)) if run.startswith('mark driver-accepted-details FAIL "api fallback: accept"', i)]
    assert len(marks) == 2, marks
    for i in marks:
        assert "ACCEPT_VIA_API=1" in run[i:i + 200], run[i:i + 200]


@case
def test_run_sh_opens_ride_via_card_before_final_accepted_details_capture():
    run = open(os.path.join(ROOT, "run.sh"), encoding="utf-8").read()
    i = run.index('cap driver-accepted-details 35-driver-accepted-details "$DRIVER_PKG" 30')
    window = run[i - 400:i]
    assert "driver_open_ride_via_card" in window, window
    assert 'bring_fg "$DRIVER_PKG" || true' in window, window
    assert window.index('bring_fg "$DRIVER_PKG" || true') < window.index("driver_open_ride_via_card"), window


if __name__ == "__main__":
    failed = 0
    for fn in CASES:
        try:
            fn()
            print("ok  ", fn.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", fn.__name__, str(e)[:400])
    print("%d/%d cas passés" % (len(CASES) - failed, len(CASES)))
    raise SystemExit(1 if failed else 0)
