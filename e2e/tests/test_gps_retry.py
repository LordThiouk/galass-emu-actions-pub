#!/usr/bin/env python3
"""TDD spec — the GPS fix must be RE-READ, not assumed after one settle.

Run 40602473815: stop 1 never got a verified fix. The log had no
« adb emu geo fix a échoué » line, so the injection ran rc=0 and the SINGLE
`dumpsys location` read three seconds later was still too early — one shot at
the check, one failure, no second look.

Pins:
  * `geofence_stop` is a bounded multi-round loop: per round RE-INJECT
    `adb emu geo fix`, settle, take a FRESH `dumpsys location`, run the
    `geo.py check`, and capture the console to `diag/geo-fix-stopN-rK.txt`;
  * `GEO_RETRY_ROUNDS` (default 2 extra rounds) is clamped, never unbounded;
  * a definitive failure logs an informative line and still blocks every
    complete-stop;
  * the 500 m gate and the `gps` provider stay exactly as `geo.py` defines them
    — the retry never buys a pass by widening the fence.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = os.path.join(ROOT, "run.sh")
GEO = os.path.join(ROOT, "geo.py")
REPO = os.path.dirname(ROOT)
sys.path.insert(0, ROOT)

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def run_text():
    with open(RUN, encoding="utf-8") as fh:
        return fh.read()


def extract_fn(name):
    """Whole `name() { ... }` definition, brace-counted (braces are balanced here)."""
    body = run_text()
    start = re.search(r"(?m)^%s\(\) \{" % re.escape(name), body)
    assert start, f"{name}() introuvable dans {RUN}"
    depth = 0
    for i in range(start.end() - 1, len(body)):
        if body[i] == "{":
            depth += 1
        elif body[i] == "}":
            depth -= 1
            if depth == 0:
                return body[start.start():i + 1]
    raise AssertionError(f"{name}() non fermée")


# Fake emulator: the fix only lands on the Nth dumpsys read (N = FIX_FROM_ROUND,
# default 2). Every read is counted so the test can prove the re-read happened.
FAKE_ADB = r"""
adb() {
  if [ "$1" = emu ] && [ "$2" = geo ] && [ "$3" = fix ]; then
    printf '%s %s\n' "$5" "$4" >> "$OUT/injected.txt"
    printf '%s %s\n' "$5" "$4" > "$OUT/applied-last.txt"
    return 0
  fi
  if [ "$1" = shell ] && [ "$2" = dumpsys ]; then
    calls=$(cat "$OUT/dumpsys-calls.txt" 2>/dev/null || echo 0)
    calls=$((calls + 1))
    printf '%s' "$calls" > "$OUT/dumpsys-calls.txt"
    if [ -f "$OUT/dump.txt" ]; then
      cat "$OUT/dump.txt"
      return 0
    fi
    if [ "$calls" -ge "${FIX_FROM_ROUND:-2}" ]; then
      read -r lat lng < "$OUT/applied-last.txt"
      printf '  last location=Location[gps %s,%s hAcc=5]\n' "$lat" "$lng"
    else
      printf '  last location=Location[gps 14.600000,-17.300000 hAcc=5]\n'
    fi
    return 0
  fi
  return 0
}
api() { printf '%s\n' "$*" >> "$OUT/api-calls.txt"; }
"""


def run_geofence(index="0", extra_env=None, fix_from_round=2):
    tmp = tempfile.mkdtemp(prefix="e2e-gps-retry-")
    with open(os.path.join(tmp, "state.json"), "w", encoding="utf-8") as fh:
        json.dump({"stops": [
            {"latitude": 14.7100, "longitude": -17.4620},
            {"latitude": 14.7020, "longitude": -17.4550},
        ], "stops_ride": "R1"}, fh)
    # `geofence_stop` injects `geo fix <lng> <lat>`; the fake adb records it in
    # this exact order, and the dumpsys read replays the LAST injected pair.
    env = dict(os.environ)
    env.update({"OUT": tmp, "PY": sys.executable, "GEO_SETTLE_SEC": "0",
                "RIDE_ID": "R1", "FIX_FROM_ROUND": str(fix_from_round)})
    env.update(extra_env or {})
    script = (
        "set -uo pipefail\n"
        'log() { printf "%s\\n" "$*" >> "$OUT/run.log"; }\n'
        + FAKE_ADB +
        'eval "$1"\n'
        'geofence_stop ' + index + '\n'
        'printf "RC=%s\\n" "$?"\n'
    )
    proc = subprocess.run(["bash", "-c", script, "_", extract_fn("geofence_stop")],
                          capture_output=True, text=True, env=env, cwd=REPO)
    return tmp, proc


def captures(tmp, stop="1"):
    diag = os.path.join(tmp, "diag")
    try:
        names = sorted(n for n in os.listdir(diag)
                       if re.fullmatch(r"geo-fix-stop%s-r\d+\.txt" % stop, n))
    except OSError:
        return []
    return names


@case
def test_geofence_stop_retries_until_the_fix_lands():
    """Round 1 reads a stale fix, round 2 reads the injected one: PASS at round 2."""
    tmp, proc = run_geofence(fix_from_round=2)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout.strip().endswith("RC=0"), (proc.stdout, proc.stderr)
    assert captures(tmp) == ["geo-fix-stop1-r1.txt", "geo-fix-stop1-r2.txt"], \
        captures(tmp)
    for name in captures(tmp):
        assert os.path.getsize(os.path.join(tmp, "diag", name)) > 0, name
    assert "Location[gps 14.71" in open(
        os.path.join(tmp, "diag", "geo-fix-stop1-r2.txt"), encoding="utf-8").read()
    # the legacy artifact the report/evidence publishes still exists
    assert os.path.isfile(os.path.join(tmp, "diag", "geo-stop1.txt")), "diag/geo-stop1.txt perdu"
    # re-injected, not injected once
    injections = [l for l in open(os.path.join(tmp, "injected.txt"),
                                 encoding="utf-8").read().splitlines() if l.strip()]
    assert len(injections) == 2, injections
    assert open(os.path.join(tmp, "api-calls.txt"), encoding="utf-8").read().strip() \
        == "loc_stop", "loc_stop doit être posté exactement une fois, après le round 2"
    log = open(os.path.join(tmp, "run.log"), encoding="utf-8").read()
    assert "geofence" in log and "r1" in log, log


@case
def test_geofence_stop_rounds_are_bounded_by_env():
    """GEO_RETRY_ROUNDS=3 => 4 reads (1 + 3 retries), then an informative stop."""
    tmp, proc = run_geofence(extra_env={"GEO_RETRY_ROUNDS": "3"}, fix_from_round=99)
    assert proc.stdout.strip().endswith("RC=1"), (proc.stdout, proc.stderr)
    assert captures(tmp) == [f"geo-fix-stop1-r{k}.txt" for k in range(1, 5)], captures(tmp)
    assert not os.path.exists(os.path.join(tmp, "api-calls.txt")), \
        "loc_stop appelé sans fix vérifié"
    log = open(os.path.join(tmp, "run.log"), encoding="utf-8").read()
    assert "geofence" in log, log
    assert re.search(r"round|essai", log), log
    assert "fix GPS non appliqué" in log, log


@case
def test_geofence_stop_zero_extra_rounds_is_the_legacy_single_shot():
    tmp, proc = run_geofence(extra_env={"GEO_RETRY_ROUNDS": "0"}, fix_from_round=99)
    assert proc.stdout.strip().endswith("RC=1"), (proc.stdout, proc.stderr)
    assert captures(tmp) == ["geo-fix-stop1-r1.txt"], captures(tmp)
    assert not os.path.exists(os.path.join(tmp, "api-calls.txt"))


@case
def test_geofence_stop_clamps_the_rounds():
    """Non-numeric -> default 2 (3 reads); 99 -> 5 (6 reads); -1 -> default."""
    for preset, want in (("abc", 3), ("99", 6), ("-1", 3), ("", 3)):
        env = {"FIX_FROM_ROUND": "99"}
        if preset:
            env["GEO_RETRY_ROUNDS"] = preset
        tmp, proc = run_geofence(extra_env=env, fix_from_round=99)
        assert proc.stdout.strip().endswith("RC=1"), (preset, proc.stdout, proc.stderr)
        assert len(captures(tmp)) == want, (preset, captures(tmp))
        assert "integer expression" not in proc.stderr, (preset, proc.stderr)


@case
def test_geofence_stop_never_widens_the_fence_to_pass_a_retry():
    """geo.py is intact: gps provider, 500 m default, 100..2000 clamp, same call."""
    fn = extract_fn("geofence_stop")
    assert "--provider gps" in fn, fn
    assert re.search(r'limit="\$\{GEO_STOP_LIMIT_M:-500\}"', fn), fn
    assert re.search(r'\[ "\$limit" -lt 100 \] && limit=100', fn), fn
    assert re.search(r'\[ "\$limit" -gt 2000 \] && limit=2000', fn), fn
    assert fn.count("e2e/geo.py check") == 1, \
        "une seule vérification, dans la boucle — pas une par chemin de sortie"
    assert "geo-stop$n.txt" in fn, "l'artefact diag/geo-stopN.txt doit rester publié"

    # the retry rounds are a knob of run.sh only: geo.py keeps its own default
    geo_body = open(GEO, encoding="utf-8").read()
    assert "GEO_RETRY_ROUNDS" not in geo_body, "geo.py ne doit pas connaître les rounds"
    assert re.search(r"^STOP_LIMIT_M = 500\.0", geo_body, re.M), \
        "la clôture backend de 500 m a bougé"

    body = run_text()
    assert re.search(r'GEO_RETRY_ROUNDS:-2', body), "défaut 2 rounds absent"


@case
def test_geofence_stop_regates_the_fix_on_every_retry_round():
    """Static order: inject -> settle -> fresh dumpsys -> check, inside the loop."""
    fn = extract_fn("geofence_stop")
    i_loop = fn.index("while")
    body = fn[i_loop:]
    i_inject = body.index("adb emu geo fix")
    i_settle = body.index("sleep ")
    i_dump = body.index("dumpsys location")
    i_check = body.index("geo.py check")
    assert i_inject < i_settle < i_dump < i_check, body[:1200]
    assert "geo-fix-stop$n-r$k.txt" in body or "geo-fix-stop${n}-r${k}.txt" in body, body


def main():
    failed = 0
    for fn in CASES:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - test harness
            failed += 1
            print(f"FAIL - {fn.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok - {fn.__name__}")
    if failed:
        print(f"FAIL ({len(CASES) - failed}/{len(CASES)})")
        return 1
    print(f"PASS ({len(CASES)}/{len(CASES)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
