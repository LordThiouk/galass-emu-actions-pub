#!/usr/bin/env python3
"""Tests : avant chaque tap « Confirmer l'arrêt », ordre geo fix -> ping Driver frais lu sur le serveur -> tap.
Sans émulateur : `adb` est un faux script (journal `taps`), `api` est un faux qui rejoue des réponses."""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_stop_confirm_ui as base  # noqa: E402

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


# réponses successives de `api stop_ping_check` (la 1re est la lecture de base, avant le geo fix)
STALE = "PING updated_at=T0 dist_m=9000 age_s=30 PING_WAIT"
FRESH = "PING updated_at=T1 dist_m=40 age_s=3 PING_OK"
BODY = r'''
STOP_LAT=(14.71 14.702); STOP_LNG=(-17.462 -17.455)
export STOP_PING_WAIT=%(wait)d
echo 0 > "$OUT/cnt"
api() {
  echo "api $1 prev=${PREV_UPDATED_AT:-}" >> "$OUT/taps"
  [ "$1" = "stop_ping_check" ] || return 0
  local n; n=$(( $(cat "$OUT/cnt") + 1 )); echo $n > "$OUT/cnt"; local r; r=$(sed -n "${n}p" "$OUT/replies"); [ -n "$r" ] || r=$(tail -1 "$OUT/replies"); echo "$r"
}
driver_stop_confirm_ui pkg 1
echo "rc=$?"
'''


def go(replies, wait=20):
    tmp = tempfile.mkdtemp(prefix="e2e-prime-")
    open(os.path.join(tmp, "replies"), "w").write("\n".join(replies) + "\n")
    rc, taps, out = base.harness(tmp, [base.RUNNING, base.DIALOG, base.RUNNING], [0, 1], BODY % dict(wait=wait))
    return rc, [l for l in taps.splitlines() if l.strip()], out


def idx(lines, pat, start=0):
    for i in range(start, len(lines)):
        if pat in lines[i]:
            return i
    return -1


@case
def test_geo_fix_then_fresh_server_ping_then_tap():
    rc, lines, out = go([STALE, STALE, FRESH])
    assert rc == 0, out
    geo = idx(lines, "emu geo fix -17.462 14.71")  # longitude d'abord
    assert geo >= 0, lines
    check = idx(lines, "api stop_ping_check", geo)
    tap = idx(lines, "input tap 540 2011")
    assert 0 <= geo < check < tap, lines
    # le dernier contrôle avant le tap est celui qui a rendu PING_OK : deux contrôles après le geo fix
    after = [l for l in lines[geo:tap] if "api stop_ping_check" in l]
    assert len(after) == 2, lines
    assert all("prev=T0" in l for l in after), lines     # fraîcheur = updated_at différent de la lecture de base
    assert not any("api loc_stop" in l for l in lines), lines  # pas de ping API qui masquerait le ping du Driver


@case
def test_baseline_read_happens_before_geo_fix():
    rc, lines, out = go([STALE, FRESH])
    first_check = idx(lines, "api stop_ping_check")
    geo = idx(lines, "emu geo fix")
    assert 0 <= first_check < geo, lines


@case
def test_never_taps_before_ping_is_fresh():
    rc, lines, out = go([STALE, STALE, STALE, FRESH])
    tap = idx(lines, "input tap 540 2011")
    ok_pos = [i for i, l in enumerate(lines) if "api stop_ping_check" in l]
    assert len(ok_pos) >= 4 and ok_pos[3] < tap, lines


@case
def test_wait_is_bounded_then_taps_anyway_with_api_fallback_ping():
    rc, lines, out = go([STALE], wait=8)
    checks = [l for l in lines if "api stop_ping_check" in l]
    assert 2 <= len(checks) <= 4, lines           # base + au plus 8s / 4s de pas, jamais infini
    assert idx(lines, "api loc_stop") > idx(lines, "api stop_ping_check"), lines
    assert idx(lines, "input tap 540 2011") > idx(lines, "api loc_stop"), lines
    assert "WARN" in out and "ping" in out.lower(), out


if __name__ == "__main__":
    failed = 0
    for fn in CASES:
        try:
            fn()
            print("ok  ", fn.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", fn.__name__, str(e)[:400])
    raise SystemExit(1 if failed else 0)
