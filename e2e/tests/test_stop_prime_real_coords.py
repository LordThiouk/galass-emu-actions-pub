#!/usr/bin/env python3
"""Tests : stop_prime géolocalise l'émulateur sur les coordonnées RÉELLES de l'arrêt (serveur,
api stop_coords), pas sur le tableau STOP_LAT/STOP_LNG codé en dur — qui diffère dès que la
course est composée par l'UI (Google Places). Repli sur le tableau codé en dur seulement si
l'appel API ne rend rien.
Sans émulateur : `adb` est un faux script (journal `taps`), `api` est un faux qui rejoue des réponses."""
import os
import subprocess
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES = []


def case(fn):
    CASES.append(fn)
    return fn


BODY = r'''
STOP_LAT=(14.71 14.702); STOP_LNG=(-17.462 -17.455)
api() {
  echo "api $* idx=$STOP_INDEX" >> "$OUT/taps"
  case "$1" in
    stop_coords) cat "$OUT/coords_reply" ;;
    stop_ping_check) echo "PING updated_at=T1 dist_m=5 age_s=1 PING_OK" ;;
  esac
}
stop_prime 1
echo rc=$?
'''


def go(coords_reply):
    tmp = tempfile.mkdtemp(prefix="e2e-coords-")
    open(os.path.join(tmp, "coords_reply"), "w").write(coords_reply)
    taps_path = os.path.join(tmp, "taps")
    open(taps_path, "w").close()
    fake = os.path.join(tmp, "bin")
    os.makedirs(fake, exist_ok=True)
    with open(os.path.join(fake, "adb"), "w") as fh:
        fh.write('#!/bin/bash\necho "$@" >> %s\n' % taps_path)
    os.chmod(os.path.join(fake, "adb"), 0o755)
    script = ('set -u\nexport OUT="%s"; export PATH="%s:$PATH"; mkdir -p "$OUT/screens" "$OUT/diag"\n'
               '. "%s"\nsleep() { :; }\n%s') % (tmp, fake, os.path.join(ROOT, "ui.sh"), BODY)
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    return open(taps_path).read(), p.stdout + p.stderr


@case
def test_geo_fix_uses_real_server_stop_coords_not_the_hardcoded_table():
    taps, out = go("14.6821999 -17.450951\n")
    assert "emu geo fix -17.450951 14.6821999" in taps, taps
    assert "emu geo fix -17.462 14.71" not in taps, taps


@case
def test_falls_back_to_hardcoded_table_when_api_returns_nothing():
    taps, out = go("\n")
    assert "emu geo fix -17.462 14.71" in taps, taps


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
