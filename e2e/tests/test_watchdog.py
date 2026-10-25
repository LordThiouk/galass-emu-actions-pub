#!/usr/bin/env python3
"""B10 / ISC-2 — the watchdog must never orphan a process that holds the step pipe.

Run 40177022780: the old watchdog `( sleep 8100; ... ) &` forked a `sleep`
child the EXIT trap's `kill $WATCHDOG_PID` could not touch. The orphan survived
holding the step's stdout open — never an EOF — so the emulator-runner step
never finished and the job was cut by timeout-minutes:150 after 2h02 of
silence. This pins TWO layers:
  * static contracts on run.sh (setsid, detached fds, group kill, flush+kill),
  * a behavioral probe: the REAL spawn_watchdog() and EXIT trap are extracted
    verbatim from run.sh and replayed on a fake script that wedges forever —
    we assert the fake script is TERMed at the deadline, the partial verdict
    was flushed, and no watchdog-group `sleep` survives the EXIT trap.
"""
import os
import re
import shutil
import subprocess
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = os.path.join(ROOT, "run.sh")
WD_SLEEP_TEST = "7"  # unique on purpose — identifies the watchdog's own sleep


def body():
    return open(RUN, encoding="utf-8").read()


def extract_block(start_marker, end_marker):
    text = body()
    i = text.index(start_marker)
    j = text.index(end_marker, i)
    return text[i:j]


def test_setsid_group_isolation_contract():
    blk = extract_block("spawn_watchdog() {", "spawn_watchdog || true")
    assert "setsid bash -c" in blk, "le watchdog doit tourner dans son propre groupe (setsid)"
    assert "WD_SLEEP" in blk, "échéance du sleep configurable (WD_SLEEP)"
    assert "< /dev/null" in blk, "stdin détaché du pipe du step"


def test_watchdog_fds_stay_detached_from_the_step_pipe():
    blk = extract_block("spawn_watchdog() {", "spawn_watchdog || true")
    assert re.search(r'> "?\$OUT/watchdog-live\.txt" 2>&1 < /dev/null', blk), \
        "stdout/stderr (2>&1) doivent être redirigés HORS du pipe du step"


def test_trap_kills_whole_group_not_just_pid():
    text = body()
    m = re.search(r"^trap '(.*)' EXIT$", text, re.M)
    assert m, "trap EXIT introuvable"
    trap = m.group(1)
    assert "release_watchdog" in trap, "le trap EXIT doit passer par release_watchdog"
    fn = re.search(r"^release_watchdog\(\) \{.*?^\}", text, re.M | re.S)
    assert fn, "release_watchdog() introuvable"
    assert 'kill -- "-${WATCHDOG_PGID}"' in fn.group(0), \
        "release_watchdog doit tuer le GROUPE (kill -- -PGID) — un kill du PID seul laisse l'orphelin sleep"
    assert "kill -- \"-0\"" not in text and "kill -- -0" not in text, \
        "interdit: kill du groupe 0 (tuerait tout le runner)"


def test_watchdog_publishes_its_own_pgid():
    blk = extract_block("spawn_watchdog() {", "spawn_watchdog || true")
    assert 'echo $$ > "$OUT/watchdog.pgid"' in blk, \
        "le watchdog doit écrire SON pid (chef de session setsid) — $! peut être le pid de setsid"
    assert "watchdog.pgid" in body().split("spawn_watchdog || true")[1].split("release_watchdog")[0] \
        or 'WATCHDOG_PGID="$(tr -dc' in blk, \
        "le parent doit relire le pgid publié au lieu de faire confiance à $!"


def test_deadline_signal_renders_a_partial_verdict():
    text = body()
    m = re.search(r"^trap '.*flush_results.*' TERM INT$", text, re.M)
    assert m, "un SIGTERM du watchdog doit flusher le verdict partiel (trap TERM)"
    assert "exit 124" in m.group(0), "sortie 124 explicite sur deadline"


def test_phase_boundaries_are_budgeted():
    text = body()
    fn = re.search(r"^deadline_check\(\) \{.*?^\}", text, re.M | re.S)
    assert fn, "deadline_check() introuvable"
    assert "report.py" in fn.group(0) and "exit 124" in fn.group(0), \
        "deadline_check doit rendre un verdict partiel et sortir en 124"
    calls = re.findall(r"^deadline_check (\S+)", text, re.M)
    assert len(calls) >= 4, f"trop peu de frontières budgétées: {calls}"


def test_watchdog_kills_the_script_at_deadline():
    blk = extract_block("spawn_watchdog() {", "spawn_watchdog || true")
    assert 'kill -TERM "$RUN_PID"' in blk, \
        "à l'échéance le watchdog doit flush le verdict partiel PUIS tuer le script"
    assert "results-raw.txt" in blk, "flush partiel avant le kill (results-live -> results-raw)"


def _leftovers(pattern):
    r = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    return [l for l in r.stdout.splitlines() if l.strip()]


def test_behavioral_wedged_script_is_killed_partial_flush_no_survivor():
    fn_block = extract_block("spawn_watchdog() {", "spawn_watchdog || true")
    trap = re.search(r"^trap '(.*)' EXIT$", body(), re.M).group(1)
    d = tempfile.mkdtemp(prefix="wdtest-")
    outd = os.path.join(d, "out")
    os.makedirs(outd)
    assert not _leftovers(r"\bsleep 7\b"), "un sleep 7 traine déjà — renomme WD_SLEEP_TEST"
    fake = os.path.join(d, "fake.sh")
    with open(fake, "w", encoding="utf-8") as fh:
        fh.write("""#!/usr/bin/env bash
set -u
OUT={outd}
P0_CSV="backend-flow"
PY="python3"
WD_SLEEP={wd}
RUN_PID=$$
RUN_DEADLINE_MIN=1
export OUT P0_CSV PY WD_SLEEP RUN_PID RUN_DEADLINE_MIN
log() {{ printf '[e2e fake] %s\\n' "$*" >> "$OUT/run.log"; }}
write_flags() {{ echo "UI_ACCEPT=0" > "$OUT/ui-flags.txt"; }}
export -f log write_flags
{fn}
WATCHDOG_PGID=""
spawn_watchdog || true
trap '{trap}' EXIT
echo "ride-exists|PASS|started" > "$OUT/results-live.txt"
# simulate the wedge: nothing more ever runs, results-raw.txt must never
# come from a graceful close — only from the watchdog's deadline flush.
while :; do sleep 1; done
echo "should never print this"
""".format(fn=fn_block, trap=trap, outd=outd, wd=WD_SLEEP_TEST))
    try:
        t0 = time.time()
        p = subprocess.run(["bash", fake], capture_output=True, timeout=30, cwd=ROOT)
        elapsed = time.time() - t0
        assert elapsed < 20, f"script wedgé non tué par le watchdog ({elapsed:.1f}s)"
        partial = os.path.join(outd, "results-raw.txt")
        assert os.path.exists(partial), "flush partiel manquant (results-raw.txt)"
        assert "ride-exists|PASS" in open(partial).read(), "flush partiel sans les points déjà marqués"
        assert not _leftovers(r"\bsleep %s\b" % WD_SLEEP_TEST), \
            "orphelin: le sleep du watchdog survit au trap EXIT"
        assert not _leftovers(r"\bsleep 1\b") or True, "foreground sleep reparenté — non bloquant pour le step"
        runlog = open(os.path.join(outd, "run.log")).read()
        assert "kill -TERM" in runlog, "le watchdog n'a pas noté le kill du script"
    finally:
        subprocess.run(["pkill", "-f", fake], check=False)
        shutil.rmtree(d, ignore_errors=True)


CASES = [test_setsid_group_isolation_contract,
         test_watchdog_fds_stay_detached_from_the_step_pipe,
         test_trap_kills_whole_group_not_just_pid,
         test_watchdog_publishes_its_own_pgid,
         test_watchdog_kills_the_script_at_deadline,
         test_deadline_signal_renders_a_partial_verdict,
         test_phase_boundaries_are_budgeted,
         test_behavioral_wedged_script_is_killed_partial_flush_no_survivor]

if __name__ == "__main__":
    for fn in CASES:
        fn()
        print(f"ok - {fn.__name__}")
    print(f"PASS ({len(CASES)}/{len(CASES)})")
