#!/usr/bin/env python3
"""Tests : confirmation d'arrêt Driver par l'UI (preuve serveur) et sortie de la notation Rider.
Sans émulateur : `adb` est un faux script sur PATH, ui_xml/ride_state sont rejoués depuis des fixtures."""
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


RUNNING = dump(node(0, "0/2 arrêt terminé", "[42,1428][1038,1473]"),
               node(1, "Confirmer l'arrêt", "[42,1938][1038,2085]", clickable="true"))
DIALOG = dump(node(0, "Annuler", "[100,1400][400,1520]", clickable="true"),
              node(1, "Confirmer", "[500,1400][900,1520]", clickable="true"))
HOME = dump(node(0, "Où allez-vous ?", "[84,509][996,645]", clickable="true"))
RATING = dump(node(0, "Avis", "[84,300][996,360]"), node(1, "Noter la course", "[84,900][996,1000]"))
HOME_OFF = dump(node(0, "Accueil", "[81,2182][378,2277]", clickable="true"))


def harness(tmp, frames, states, body):
    """frames : dumps servis dans l'ordre (le dernier se répète) ; states : stops_done servis par ride_state."""
    for i, f in enumerate(frames):
        open(os.path.join(tmp, "f%d.xml" % i), "w").write(f)
    open(os.path.join(tmp, "states"), "w").write("\n".join(str(s) for s in states) + "\n")
    taps = os.path.join(tmp, "taps")
    open(taps, "w").close()
    fake = os.path.join(tmp, "bin")
    os.makedirs(fake, exist_ok=True)
    with open(os.path.join(fake, "adb"), "w") as fh:
        fh.write('#!/bin/bash\necho "$@" >> %s\n' % taps)
    os.chmod(os.path.join(fake, "adb"), 0o755)
    script = r'''
set -u
export OUT="%(tmp)s"; export PATH="%(fake)s:$PATH"; mkdir -p "$OUT/screens" "$OUT/diag"
. "%(ui)s"
sleep() { :; }
FX=0; SX=0
ui_xml() { local f="%(tmp)s/f$FX.xml"; [ -f "$f" ] || f="%(tmp)s/f%(last)d.xml"; FX=$((FX+1)); UI_XML_PATH="$f"; return 0; }
ride_state() { local v; v=$(sed -n "$((SX+1))p" "%(tmp)s/states"); [ -n "$v" ] || v=$(tail -1 "%(tmp)s/states"); SX=$((SX+1)); echo "ride_details status=3 stops_done=$v" > "$OUT/last-ride-state.txt"; }
%(body)s
''' % dict(tmp=tmp, fake=fake, ui=os.path.join(ROOT, "ui.sh"), last=len(frames) - 1, body=body)
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    rc = [l for l in p.stdout.splitlines() if l.startswith("rc=")]
    return (int(rc[-1][3:]) if rc else None), open(taps).read(), p.stdout + p.stderr


def run(frames, states, body):
    return harness(tempfile.mkdtemp(prefix="e2e-stop-"), frames, states, body)


CALL = 'driver_stop_confirm_ui pkg 1\necho "rc=$?"'


@case
def test_stop_ui_taps_clickable_parent_then_proves_on_server():
    rc, taps, out = run([RUNNING, DIALOG, DIALOG, RUNNING], [0, 0, 1], CALL)
    assert rc == 0, out
    assert "input tap 540 2011" in taps, taps      # « Confirmer l'arrêt »
    assert "input tap 700 1460" in taps, taps      # « Confirmer » du dialogue (parent cliquable)
    assert "input tap 300 1460" not in taps, taps  # jamais « Annuler »


@case
def test_stop_ui_without_server_effect_is_failure_with_diag():
    tmp = tempfile.mkdtemp(prefix="e2e-stop-")
    rc, taps, out = harness(tmp, [RUNNING, DIALOG, RUNNING], [0], CALL)
    assert rc == 1, out
    assert os.path.isfile(os.path.join(tmp, "diag", "dbg-stop1-a3.xml")), os.listdir(os.path.join(tmp, "diag"))
    assert taps.count("input tap 540 2011") == 3, taps


@case
def test_stop_ui_never_retaps_once_server_saw_the_stop():
    # essai 1 sans effet immédiat, mais le serveur voit l'arrêt avant l'essai 2 : pas de 2e tap
    rc, taps, out = run([RUNNING, DIALOG, RUNNING], [0, 0, 1], CALL)
    assert rc == 0, out
    assert taps.count("input tap 540 2011") == 1, taps


@case
def test_stop_ui_no_button_returns_2_without_tap():
    rc, taps, out = run([HOME_OFF], [0], CALL)
    assert rc == 2, out
    assert "input tap" not in taps, taps


@case
def test_stop_ui_refuses_stop_already_done_before_any_tap():
    rc, taps, out = run([RUNNING], [1], CALL)
    assert rc == 3, out
    assert "input tap" not in taps, taps


@case
def test_rider_leave_rating_does_not_press_back_when_home_already_there():
    rc, taps, out = run([HOME], [0], 'rider_leave_rating pkg\necho "rc=$?"')
    assert rc == 0, out
    assert "keyevent 4" not in taps, taps


@case
def test_rider_leave_rating_presses_back_only_on_non_home_screen():
    rc, taps, out = run([RATING, HOME], [0], 'rider_leave_rating pkg\necho "rc=$?"')
    assert rc == 0, out
    assert taps.count("keyevent 4") == 1, taps


@case
def test_rider_leave_rating_fails_when_home_never_reached():
    rc, taps, out = run([RATING], [0], 'rider_leave_rating pkg\necho "rc=$?"')
    assert rc == 1, out


if __name__ == "__main__":
    failed = 0
    for fn in CASES:
        try:
            fn()
            print("ok  ", fn.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", fn.__name__, str(e)[:300])
    raise SystemExit(1 if failed else 0)
