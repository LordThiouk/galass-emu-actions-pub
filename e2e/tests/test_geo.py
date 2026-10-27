#!/usr/bin/env python3
"""TDD spec for the GPS geofence (e2e/geo.py + run.sh stops).

`complete-stop` used to run with whatever fix the emulator had, so a stop could
be completed from the pickup point. Pins:

  * haversine distance on real coordinates;
  * parsing Android `Location[...]` lines out of `dumpsys location`;
  * the CLI: an applied fix within 100 m of the injected point AND within 500 m
    of the stop is the only PASS, everything else is rc=3 with `geofence`;
  * run.sh reads the stop coordinates from $OUT/state.json (never hardcoded),
    injects the fix, dumps `dumpsys location`, and gates BOTH the UI tap and the
    `api stop_one` fallback behind `geofence_stop`.
"""
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = os.path.join(ROOT, "run.sh")
GEO = os.path.join(ROOT, "geo.py")
sys.path.insert(0, ROOT)

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def geo():
    import geo as module
    return module


def run_text():
    return open(RUN, encoding="utf-8").read()


def extract_fn(name):
    """Whole `name() { ... }` definition, brace-counted (braces are balanced here)."""
    body = run_text()
    start = re.search(r"(?m)^%s\(\) \{" % re.escape(name), body)
    assert start, f"{name}() introuvable"
    depth = 0
    for i in range(start.end() - 1, len(body)):
        if body[i] == "{":
            depth += 1
        elif body[i] == "}":
            depth -= 1
            if depth == 0:
                return body[start.start():i + 1]
    raise AssertionError(f"{name}() non fermée")


def py(argv):
    return subprocess.run([sys.executable, GEO, *argv], capture_output=True, text=True)


def write_fix(tmp, body):
    path = os.path.join(tmp, "fix.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


DUMPSYS = """\
Location Manager State:
  gps provider:
    last location=Location[gps 14.710000,-17.462000 hAcc=5 et=+1d2h3m4s alt=12.0]
    last location=Location[network 14.700000,-17.450000 hAcc=900]
  fused provider:
    last location=Location[fused 14.710100,-17.462100 hAcc=8]
"""


# ------------------------------------------------------------- haversine
@case
def test_haversine_on_known_distances():
    f = geo()
    # same point
    assert f.haversine_m(14.71, -17.462, 14.71, -17.462) == 0.0
    # ~1 degree of latitude is ~111 km
    d = f.haversine_m(0.0, 0.0, 1.0, 0.0)
    assert 110_000 < d < 112_500, d
    # the two E2E stops are ~1 km apart, never kilometres away
    d = f.haversine_m(14.71, -17.462, 14.702, -17.455)
    assert 800 < d < 1400, d
    # symmetric
    assert abs(f.haversine_m(14.71, -17.462, 14.702, -17.455)
               - f.haversine_m(14.702, -17.455, 14.71, -17.462)) < 1e-9


@case
def test_parse_locations_reads_android_fix_lines_with_their_provider():
    f = geo()
    fixes = f.parse_locations(DUMPSYS)
    # the provider travels WITH the coordinates: a coarse network fix is not a
    # GPS fix, and conflating them was the whole hole in the old check.
    assert ("gps", 14.71, -17.462) in fixes, fixes
    assert ("network", 14.7, -17.45) in fixes, fixes
    assert ("fused", 14.7101, -17.4621) in fixes, fixes
    assert len(fixes) == 3, fixes
    assert f.parse_locations("") == []
    assert f.parse_locations("no location here") == []


@case
def test_parse_locations_ignores_non_numeric_coordinates():
    f = geo()
    assert f.parse_locations("  last location=Location[gps abc,def hAcc=5]\n") == []
    assert f.parse_locations("  last location=Location[gps , hAcc=5]\n") == []


@case
def test_nearest_fix_distance_picks_the_closest_fix():
    f = geo()
    d = f.nearest_fix_distance(DUMPSYS, 14.71, -17.462)
    assert d is not None and d < 30, d
    assert f.nearest_fix_distance("", 14.71, -17.462) is None
    # provider-scoped: a network fix 1 km away must not answer for the gps provider
    network_only = "  last location=Location[network 14.700000,-17.450000 hAcc=900]\n"
    assert f.nearest_fix_distance(network_only, 14.71, -17.462) is None
    assert f.nearest_fix_distance(network_only, 14.71, -17.462, provider="network") < 2000


@case
def test_geofence_ok_thresholds():
    f = geo()
    assert f.geofence_ok(0.0) is True
    assert f.geofence_ok(499.0) is True
    assert f.geofence_ok(500.0) is True
    assert f.geofence_ok(501.0) is False
    assert f.geofence_ok(None) is False
    assert f.geofence_ok(120.0, limit=100.0) is False


# ------------------------------------------------------------------- CLI
@case
def test_cli_passes_on_an_applied_fix_near_the_stop():
    tmp = tempfile.mkdtemp(prefix="e2e-geo-pass-")
    fix = write_fix(tmp, DUMPSYS)
    proc = py(["check", "--fix", fix,
               "--stop-lat", "14.7105", "--stop-lng", "-17.4625"])
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    out = proc.stdout
    assert "provider=gps" in out and "stop_distance_m=" in out, out
    stop = float(re.search(r"stop_distance_m=([0-9.]+)", out).group(1))
    assert 0 < stop < 200, stop


@case
def test_cli_fails_when_the_fix_was_never_applied():
    """The emulator kept its previous fix: the injected point is 1 km away."""
    tmp = tempfile.mkdtemp(prefix="e2e-geo-noapply-")
    fix = write_fix(tmp, "  last location=Location[gps 14.700000,-17.450000 hAcc=5]\n")
    proc = py(["check", "--fix", fix,
               "--stop-lat", "14.71", "--stop-lng", "-17.462"])
    assert proc.returncode == 3, (proc.returncode, proc.stdout, proc.stderr)
    assert "geofence" in (proc.stdout + proc.stderr).lower(), (proc.stdout, proc.stderr)
    stop = float(re.search(r"stop_distance_m=([0-9.]+)", proc.stdout).group(1))
    assert stop > 500, stop


@case
def test_cli_fails_when_the_fix_is_far_from_the_stop():
    tmp = tempfile.mkdtemp(prefix="e2e-geo-far-")
    # applied exactly where asked, but the stop is 1.1 km away
    fix = write_fix(tmp, "  last location=Location[gps 14.700000,-17.450000 hAcc=5]\n")
    proc = py(["check", "--fix", fix,
               "--stop-lat", "14.71", "--stop-lng", "-17.462"])
    assert proc.returncode == 3, (proc.returncode, proc.stdout, proc.stderr)
    stop = float(re.search(r"stop_distance_m=([0-9.]+)", proc.stdout).group(1))
    assert stop > 500, stop


@case
def test_cli_fails_without_any_fix():
    tmp = tempfile.mkdtemp(prefix="e2e-geo-nofix-")
    fix = write_fix(tmp, "no location here\n")
    proc = py(["check", "--fix", fix,
               "--stop-lat", "14.71", "--stop-lng", "-17.462"])
    assert proc.returncode == 3, (proc.returncode, proc.stdout, proc.stderr)
    assert "geofence" in (proc.stdout + proc.stderr).lower(), (proc.stdout, proc.stderr)
    assert "stop_distance_m=none" in proc.stdout, proc.stdout


@case
def test_cli_fails_on_a_network_only_dump():
    """A network fix is not a GPS fix: it must never satisfy the geofence."""
    tmp = tempfile.mkdtemp(prefix="e2e-geo-net-")
    fix = write_fix(tmp, "  last location=Location[network 14.710000,-17.462000 hAcc=900]\n")
    proc = py(["check", "--fix", fix,
               "--stop-lat", "14.71", "--stop-lng", "-17.462"])
    assert proc.returncode == 3, (proc.returncode, proc.stdout, proc.stderr)
    assert "stop_distance_m=none" in proc.stdout, proc.stdout
    # ... and the same dump PASSES only when the caller asks for that provider.
    proc = py(["check", "--fix", fix, "--provider", "network",
               "--stop-lat", "14.71", "--stop-lng", "-17.462"])
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)


@case
def test_cli_boundary_is_the_limit_itself():
    """499 m PASS, 501 m FAIL, at the requested limit — no off-by-one."""
    stop = (14.71, -17.462)

    def at_metres(meters):
        delta = meters / 6_371_000.0 * (180.0 / 3.141592653589793)
        return (stop[0] + delta, stop[1])

    for meters, rc in ((499.0, 0), (501.0, 3)):
        lat, lng = at_metres(meters)
        tmp = tempfile.mkdtemp(prefix=f"e2e-geo-{int(meters)}-")
        fix = write_fix(tmp, f"  last location=Location[gps {lat:.9f},{lng:.9f} hAcc=5]\n")
        proc = py(["check", "--fix", fix, "--stop-lat", str(stop[0]),
                   "--stop-lng", str(stop[1])])
        assert proc.returncode == rc, (meters, proc.returncode, proc.stdout, proc.stderr)


# --------------------------------------------------------- run.sh wiring
FAKE_ADB = r"""
adb() {
  if [ "$1" = emu ] && [ "$2" = geo ] && [ "$3" = fix ]; then
    printf '%s %s\n' "$5" "$4" > "$OUT/applied.txt"
    return 0
  fi
  if [ "$1" = shell ] && [ "$2" = dumpsys ]; then
    if [ -f "$OUT/dump.txt" ]; then
      cat "$OUT/dump.txt"
      return 0
    fi
    if [ "${APPLY:-1}" = 1 ]; then
      read -r lat lng < "$OUT/applied.txt"
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


def run_geofence(index="0", apply="1", ride_id="R1", stops_ride="R1",
                 extra_env=None, dump=None):
    import json

    tmp = tempfile.mkdtemp(prefix="e2e-geofence-")
    state = {"stops": [
        {"latitude": 14.7100, "longitude": -17.4620},
        {"latitude": 14.7020, "longitude": -17.4550},
    ]}
    if stops_ride is not None:
        state["stops_ride"] = stops_ride
    with open(os.path.join(tmp, "state.json"), "w") as fh:
        json.dump(state, fh)
    env = dict(os.environ)
    env.pop("GEO_PROVIDER", None)
    env.update({"OUT": tmp, "PY": sys.executable, "GEO_SETTLE_SEC": "0", "APPLY": apply,
                "RIDE_ID": ride_id})
    env.update(extra_env or {})
    if dump is not None:
        with open(os.path.join(tmp, "dump.txt"), "w", encoding="utf-8") as fh:
            fh.write(dump)
    script = (
        "set -uo pipefail\n"
        'log() { printf "%s\\n" "$*" >> "$OUT/run.log"; }\n'
        + FAKE_ADB +
        'eval "$1"\n'
        'geofence_stop ' + index + '\n'
        'printf "RC=%s\\n" "$?"\n'
    )
    # run.sh is invoked from the repo root in CI: e2e/geo.py is relative to it.
    repo = os.path.dirname(ROOT)
    proc = subprocess.run(["bash", "-c", script, "_", extract_fn("geofence_stop")],
                          capture_output=True, text=True, env=env, cwd=repo)
    return tmp, proc


@case
def test_geofence_stop_posts_loc_stop_only_after_a_verified_fix():
    tmp, proc = run_geofence("1")
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout.strip().endswith("RC=0"), (proc.stdout, proc.stderr)
    assert os.path.isfile(os.path.join(tmp, "diag", "geo-stop2.txt")), "pas de diag"
    calls = open(os.path.join(tmp, "api-calls.txt")).read()
    assert calls.strip() == "loc_stop", calls
    # the injected fix is the SECOND stop (index 1), from state.json
    applied = open(os.path.join(tmp, "applied.txt")).read().split()
    assert applied == ["14.702", "-17.455"], applied


@case
def test_geofence_stop_blocks_when_the_fix_was_not_applied():
    tmp, proc = run_geofence("0", apply="0")
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout.strip().endswith("RC=1"), (proc.stdout, proc.stderr)
    assert not os.path.exists(os.path.join(tmp, "api-calls.txt")), \
        "loc_stop a été appelé sans fix vérifié"
    log = open(os.path.join(tmp, "run.log")).read()
    assert "geofence" in log, log


@case
def test_geofence_stop_pins_the_gps_provider():
    """Only an applied GPS fix counts: the provider is not a knob."""
    tmp, proc = run_geofence("0")
    assert proc.stdout.strip().endswith("RC=0"), (proc.stdout, proc.stderr)
    log = open(os.path.join(tmp, "run.log")).read()
    assert "provider=gps" in log, log
    block = extract_fn("geofence_stop")
    assert "--provider gps" in block, block
    assert "GEO_PROVIDER" not in run_text(), \
        "le knob provider permet de re-sélectionner un fix network périmé"


@case
def test_geofence_stop_ignores_a_network_provider_knob():
    """A coarse `network` fix inside the fence must never be selected."""
    dump = "  last location=Location[network 14.710000,-17.462000 hAcc=900]\n"
    tmp, proc = run_geofence("0", extra_env={"GEO_PROVIDER": "network"}, dump=dump)
    assert proc.stdout.strip().endswith("RC=1"), (proc.stdout, proc.stderr)
    assert not os.path.exists(os.path.join(tmp, "api-calls.txt")), \
        "loc_stop appelé sur un fix network"


@case
def test_geofence_stop_clamps_the_stop_limit():
    """GEO_STOP_LIMIT_M stays, clamped to the backend-meaningful 100..2000 m."""
    fifty = "  last location=Location[gps 14.710450,-17.462000 hAcc=5]\n"
    tmp, proc = run_geofence("0", extra_env={"GEO_STOP_LIMIT_M": "10"}, dump=fifty)
    assert proc.stdout.strip().endswith("RC=0"), (proc.stdout, proc.stderr)

    far = "  last location=Location[gps 14.732500,-17.462000 hAcc=5]\n"
    tmp, proc = run_geofence("0", extra_env={"GEO_STOP_LIMIT_M": "99999"}, dump=far)
    assert proc.stdout.strip().endswith("RC=1"), (proc.stdout, proc.stderr)
    assert not os.path.exists(os.path.join(tmp, "api-calls.txt"))

    # non-numeric => default 500
    tmp, proc = run_geofence("0", extra_env={"GEO_STOP_LIMIT_M": "abc"}, dump=fifty)
    assert proc.stdout.strip().endswith("RC=0"), (proc.stdout, proc.stderr)


@case
def test_geofence_stop_refuses_stops_from_another_ride():
    """A renewed ride makes the persisted stops stale: never inject them."""
    tmp, proc = run_geofence("0", ride_id="R2", stops_ride="R1")
    assert proc.stdout.strip().endswith("RC=1"), (proc.stdout, proc.stderr)
    assert not os.path.exists(os.path.join(tmp, "api-calls.txt")), \
        "loc_stop a été appelé avec les arrêts d'une autre course"
    log = open(os.path.join(tmp, "run.log")).read()
    assert "stops_ride" in log, log


@case
def test_run_sh_geofence_stop_checks_the_ride_tag():
    block = extract_fn("geofence_stop")
    assert "stops_ride" in block, block
    assert "--provider" in block, "le provider GPS doit être explicite"
    assert "--stop-lat" in block and "--stop-lng" in block, block
    assert "--expected-lat" not in block, "argument tautologique encore présent"
    # a loc_stop refusal must not let the complete-stop through
    assert re.search(r"if ! STOP_INDEX=\"\$i\" api loc_stop", block), block


@case
def test_geofence_stop_refuses_an_unknown_stop_index():
    tmp, proc = run_geofence("9")
    assert proc.stdout.strip().endswith("RC=1"), (proc.stdout, proc.stderr)
    assert "state.json" in open(os.path.join(tmp, "run.log")).read()
    assert not os.path.exists(os.path.join(tmp, "api-calls.txt"))


@case
def test_run_sh_has_no_hardcoded_stop_coordinates():
    body = run_text()
    assert "STOP_LAT=(" not in body, "coordonnées d'arrêt codées en dur"
    assert "STOP_LNG=(" not in body, "coordonnées d'arrêt codées en dur"


@case
def test_geofence_stop_gates_every_complete_stop():
    body = run_text()
    fn = re.search(r"^geofence_stop\(\) \{(.*?)^\}", body, re.M | re.S)
    assert fn, "geofence_stop() introuvable"
    block = fn.group(1)
    assert "state.json" in block, "les coordonnées doivent venir de $OUT/state.json"
    assert re.search(r"adb emu geo fix", block), "aucun fix injecté"
    assert "dumpsys location" in block, "aucun dumpsys location"
    assert "e2e/geo.py check" in block, "le garde-fou geo.py n'est pas appelé"
    assert re.search(r"STOP_INDEX=\"?\$?\{?i", block) or "api loc_stop" in block, block

    # both complete-stop paths sit behind the geofence call
    loop = body[body.index("for i in 0 1; do"):body.index("mark ui-stops")]
    i_geo = loop.index("geofence_stop")
    i_tap = loop.index('try_confirm "$DRIVER_PKG" "Confirmer l\'arrêt"')
    i_api = loop.index("api stop_one")
    assert i_geo < i_tap < i_api, (i_geo, i_tap, i_api)
    # the old unconditional fix + loc_stop pair is gone from the loop
    assert "adb emu geo fix" not in loop, loop
    assert "api loc_stop" not in loop, loop

    # a failed geofence is a FAIL with the geofence detail, and no complete-stop
    assert re.search(r'mark "driver-stop\$\{n\}-done" FAIL "geofence:', body), body


# ------------------------------------------------------------------- main
def main():
    failed = 0
    for fn in CASES:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - test harness
            failed += 1
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {fn.__name__}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} geo cases passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
