#!/usr/bin/env python3
"""TDD spec for the stops loop of e2e/run.sh — run with `python3 e2e/tests/test_stops_gate.py`.

ISC-5: a stop whose GPS fix was never verified must produce an explicit FAIL and
NO `complete-stop` at all. `geofence_stop` already refuses (see test_geo.py), but
the loop used to `continue` on that refusal: the second stop still ran, and the
API fallback (`STOP_INDEX=$i api stop_one`) completes the first PENDING stop
whatever index the loop was on — i.e. a stop whose fix was never verified.

Pins:
  * the geofence-failure branch of the stops loop can never `continue` (it must
    `break`, the first geofence failure aborts the whole loop, the run goes on);
  * behaviorally: geofence FAIL at index 0 => zero UI `try_confirm` calls, zero
    `api` invocations, zero `complete-stop`-reaching calls, no second iteration;
  * a geofence FAIL at index 1 keeps the stop that did verify, marks the failing
    one FAIL, and still reaches no API fallback.

No emulator, no network: `geofence_stop`, `adb`, `mark`, `api`, `try_confirm` and
`wait_ui` are stubs; only the real loop text out of run.sh is executed.
"""
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = os.path.join(ROOT, "run.sh")

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def run_text():
    return open(RUN, encoding="utf-8").read()


LOOP_OPEN = re.compile(r"^(for|while|until)\b.*\bdo$")
LOOP_CLOSE = re.compile(r"^done$")


def extract_stops_loop():
    """The whole `ui_stops=0` + `for i in 0 1; do ... done` block, verbatim."""
    lines = run_text().splitlines()
    start = None
    for idx, line in enumerate(lines):
        if line.strip() == "ui_stops=0" and \
                idx + 1 < len(lines) and lines[idx + 1].startswith("for i in 0 1; do"):
            start = idx
            break
    assert start is not None, "bloc `ui_stops=0` + boucle des arrêts introuvable"
    depth = 0
    for idx in range(start + 1, len(lines)):
        stripped = lines[idx].strip()
        if LOOP_OPEN.match(stripped):
            depth += 1
        elif LOOP_CLOSE.match(stripped):
            depth -= 1
            if depth == 0:
                return "\n".join(lines[start:idx + 1])
    raise AssertionError("boucle des arrêts non fermée")


def geofence_branch():
    """The `if ! geofence_stop "$i"; then ... fi` branch of the stops loop."""
    body = extract_stops_loop()
    start = body.find('if ! geofence_stop "$i"; then')
    assert start != -1, "branche `if ! geofence_stop \"$i\"; then` introuvable:\n" + body
    # the branch holds no nested `if`, so the first bare `fi` closes it
    close = body.find("\n  fi", start)
    assert close != -1, "branche geofence non fermée:\n" + body
    return body[start:close]


STUBS = """\
set -o pipefail
RIDER_PKG=sn.galass.galassrider
DRIVER_PKG=sn.galass.galassdriver
MARK_driver_stop1_done='+2/2|Arrêt 2'
MARK_driver_stop2_done='+2/2|Arrivée'
UI_STOP0=0
UI_STOP1=0
UI_END=0
log() { printf '%s\\n' "$*" >> "$OUT/run.log"; }
mark() { printf '%s|%s|%s\\n' "$1" "$2" "$3" >> "$OUT/marks.txt"; }
ride_state() { :; }
bring_fg() { :; }
cap() { :; }
"""


def run_stops_loop(geofence_stub, confirm=0):
    tmp = tempfile.mkdtemp(prefix="e2e-stops-")
    env = dict(os.environ)
    env["OUT"] = tmp
    script = (
        STUBS
        + 'geofence_stop() { printf "%s\\n" "$1" >> "$OUT/geofence-calls.txt"; '
        + geofence_stub + "; }\n"
        + 'try_confirm() { printf "%s\\n" "$*" >> "$OUT/try-confirm-calls.txt"; '
        + f"return {confirm}; }}\n"
        + 'wait_ui() { printf "%s\\n" "$*" >> "$OUT/wait-ui-calls.txt"; '
        + f"return {confirm}; }}\n"
        + 'api() { printf "%s\\n" "$*" >> "$OUT/api-calls.txt"; }\n'
        + 'eval "$1"\n'
        + 'printf "UI_STOPS=%s STOP0=%s STOP1=%s\\n" "$ui_stops" "$UI_STOP0" "$UI_STOP1"\n'
    )
    proc = subprocess.run(["bash", "-c", script, "_", extract_stops_loop()],
                          capture_output=True, text=True, env=env, cwd=tmp)
    return tmp, proc


def lines_of(tmp, name):
    path = os.path.join(tmp, name)
    if not os.path.isfile(path):
        return []
    return [l for l in open(path, encoding="utf-8").read().splitlines() if l.strip()]


# ------------------------------------------------------------------ structure
@case
def test_geofence_failure_branch_breaks_the_stops_loop():
    branch = geofence_branch()
    assert "break" in branch, \
        "la branche geofence doit sortir de la boucle, pas passer à l'arrêt suivant:\n" + branch
    assert not re.search(r"(?m)^\s*continue\b", branch), \
        "`continue` réintroduit: l'arrêt 2 tournerait sans fix vérifié:\n" + branch
    assert not re.search(r"(?m)^\s*(exit|return)\b", branch), \
        "seul `break` est permis: le run doit continuer après la boucle:\n" + branch


@case
def test_geofence_failure_branch_keeps_its_explicit_fail_mark():
    branch = geofence_branch()
    assert 'mark "driver-stop${n}-done" FAIL' in branch, \
        "le FAIL explicite de l'arrêt doit rester dans la branche:\n" + branch


# ---------------------------------------------------------------- behavior
@case
def test_geofence_fail_on_first_stop_completes_nothing():
    tmp, proc = run_stops_loop('return 1')
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    marks = lines_of(tmp, "marks.txt")
    assert marks == ["driver-stop1-done|FAIL|geofence: fix GPS non appliqué (arrêt 1)"], marks
    assert lines_of(tmp, "try-confirm-calls.txt") == [], \
        "un tap UI a été tenté sans fix GPS vérifié"
    assert lines_of(tmp, "wait-ui-calls.txt") == [], \
        "une attente d'écran a été tentée sans fix GPS vérifié"
    assert lines_of(tmp, "api-calls.txt") == [], \
        "repli API appelé alors que le fix n'a jamais été vérifié"
    assert lines_of(tmp, "geofence-calls.txt") == ["0"], \
        "la boucle est retournée sur l'arrêt 2 après l'échec geofence"


@case
def test_geofence_fail_on_second_stop_keeps_the_first_and_calls_no_api():
    tmp, proc = run_stops_loop('[ "$1" = "1" ] && return 1; return 0', confirm=0)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert lines_of(tmp, "geofence-calls.txt") == ["0", "1"], lines_of(tmp, "geofence-calls.txt")
    assert lines_of(tmp, "try-confirm-calls.txt") == ["sn.galass.galassdriver Confirmer l'arrêt"], \
        lines_of(tmp, "try-confirm-calls.txt")
    assert lines_of(tmp, "api-calls.txt") == [], \
        "repli API appelé pour un arrêt dont le fix n'a jamais été vérifié"
    marks = lines_of(tmp, "marks.txt")
    assert marks == ["driver-stop2-done|FAIL|geofence: fix GPS non appliqué (arrêt 2)"], marks
    assert "UI_STOPS=1 STOP0=1 STOP1=0" in proc.stdout, proc.stdout


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
    print(f"\n{len(CASES) - failed}/{len(CASES)} stops-gate cases passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
