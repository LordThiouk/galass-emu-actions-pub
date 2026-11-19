#!/usr/bin/env python3
"""diag-rider-pusher : l'événement pusher est journalisé en MAJUSCULES (DRIVER_ASSIGNED).

Run 44526136027 : le rider a bien reçu « Global pusher event: DRIVER_ASSIGNED » (logcat) mais la
mesure rendait n=0 parce que `grep -F` est sensible à la casse et cherchait `driver_assigned`.
"""
import os
import re
import subprocess
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = open(os.path.join(ROOT, "run.sh"), encoding="utf-8").read()


def measure(log_text):
    fn = re.search(r"^count_in_ci\(\).*?^\}", RUN, re.S | re.M)
    line = re.search(r"^PUSHER_ASSIGNED=.*$", RUN, re.M)
    assert fn and line, "count_in_ci() / PUSHER_ASSIGNED absents de run.sh"
    d = tempfile.mkdtemp()
    log = os.path.join(d, "stream.log")
    open(log, "w", encoding="utf-8").write(log_text)
    script = f'LOG_FILE="{log}"\n{fn.group(0)}\n{line.group(0)}\nprintf "%s" "$PUSHER_ASSIGNED"'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout.strip()


def test_uppercase_event_is_counted():
    assert measure("I flutter : Global pusher event: DRIVER_ASSIGNED\n") == "1"


def test_lowercase_event_is_still_counted():
    assert measure("pusher event: driver_assigned\n") == "1"


def test_absence_is_zero():
    assert measure("Global pusher event: LIVE_LOCATION\n") == "0"


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v(); n += 1
    print(f"{n}/{n} cases passed")
