#!/usr/bin/env python3
"""Run 45051973500 : l'offre de la course 310 n'est jamais apparue sur le Driver (TIMEOUT 76 s),
puis le backend répond `offer_expired`. Une offre vit 30 s (offer_timeout_sec) ; entre le tap
« Trouver un chauffeur » et le Driver au premier plan le harnais a consommé 21 s (run 45046315983 : 14 s).
Contrat : la bascule vers le Driver part IMMÉDIATEMENT après le tap (am start, sans attente ni dumpsys),
et un TIMEOUT d'offre écrit le logcat du Driver sur la fenêtre de la demande (cause prouvable au prochain échec).
Sans émulateur : `adb` est un faux qui journalise les commandes."""
import os
import re
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
UI = os.path.join(ROOT, "ui.sh")
RUN = open(os.path.join(ROOT, "run.sh"), encoding="utf-8").read()

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


FAKE_ADB = r'''#!/bin/bash
echo "adb $*" >> "$FAKE_CALLS"
case "$*" in
  *pidof*) echo 4242 ;;
  *"logcat"*) cat "$FAKE_LOGCAT" ;;
  *resolve-activity*) printf 'sn.galass.galassdriver/.MainActivity\r\n' ;;
esac
exit 0
'''


def harness(body, logcat=""):
    tmp = tempfile.mkdtemp(prefix="e2e-offer-")
    fake = os.path.join(tmp, "bin")
    os.makedirs(fake)
    with open(os.path.join(fake, "adb"), "w") as fh:
        fh.write(FAKE_ADB)
    os.chmod(os.path.join(fake, "adb"), 0o755)
    open(os.path.join(tmp, "logcat.txt"), "w", encoding="utf-8").write(logcat)
    script = ('export OUT="%s" FAKE_CALLS="%s/calls" FAKE_LOGCAT="%s/logcat.txt" DRIVER_PKG=sn.galass.galassdriver; '
              'export PATH="%s:$PATH"; mkdir -p "$OUT/diag"; : > "$FAKE_CALLS"; smoke_emit() { :; }; . "%s"; %s'
              % (tmp, tmp, tmp, fake, UI, body))
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, cwd=tmp)
    calls = open(os.path.join(tmp, "calls"), encoding="utf-8").read()
    runlog = open(os.path.join(tmp, "run.log"), encoding="utf-8").read() if os.path.exists(os.path.join(tmp, "run.log")) else ""
    return p.stdout, calls, runlog, tmp


def compose_body():
    m = re.search(r"^compose_ride_ui\(\) \{\n(.*?)^\}\n", RUN, re.S | re.M)
    assert m, "compose_ride_ui introuvable"
    return m.group(1).split("\n")


@case
def test_fg_switch_now_fires_am_start_and_never_waits():
    """Bascule d'avant-plan sans attente : un seul `am start`, ni dumpsys ni uiautomator (chacun coûte
    plusieurs secondes sur le runner, sur une fenêtre d'offre de 30 s)."""
    out, calls, _, _ = harness('fg_switch_now "$DRIVER_PKG" "sn.galass.galassdriver/.MainActivity"; echo "rc=$?"')
    assert "rc=0" in out, out
    assert "am start -n sn.galass.galassdriver/.MainActivity -f 0x20000" in calls, calls
    assert "dumpsys" not in calls and "uiautomator" not in calls, calls


@case
def test_fg_switch_now_falls_back_to_monkey_without_component():
    _, calls, _, _ = harness('fg_switch_now "$DRIVER_PKG" ""')
    assert "monkey -p sn.galass.galassdriver" in calls and "am start" not in calls, calls


@case
def test_fg_component_resolves_launcher_activity():
    out, calls, _, _ = harness('fg_component "$DRIVER_PKG"')
    assert out.strip() == "sn.galass.galassdriver/.MainActivity", (out, calls)


@case
def test_compose_switches_to_driver_on_the_line_after_the_request_tap():
    """Rien (ni log, ni deadline_check, ni attente) entre le tap de demande et la bascule Driver."""
    lines = compose_body()
    tap = [i for i, l in enumerate(lines) if 'tap_when "$RIDER_PKG" "Trouver un chauffeur"' in l]
    assert len(tap) == 1, tap
    nxt = lines[tap[0] + 1].strip()
    assert nxt.startswith('fg_switch_now "$DRIVER_PKG"'), nxt
    before = "\n".join(lines[:tap[0]])
    assert re.search(r'DRIVER_COMP=\$\(fg_component "\$DRIVER_PKG"\)', before), "composant Driver non résolu avant le tap"
    assert 'OFFER_REQ_TS=' in before, "horodatage de la demande non posé avant le tap"


@case
def test_offer_timeout_diag_writes_driver_logcat_window():
    """Au TIMEOUT : le logcat Driver depuis la demande est écrit (loadData / APP STATE / dispatch),
    avec ses statistiques, pour que « jamais reçu » et « reçu trop tard » se distinguent."""
    lc = "\n".join([
        "11-22 18:25:50.100  4242  4242 I flutter : APP STATE -> AppLifecycleState.resumed",
        "11-22 18:26:03.200  4242  4242 I flutter : [dispatch] loadData pending=0 online=true page=1",
        "11-22 18:26:03.300  9999  9999 I flutter : bruit d'un autre processus",
    ]) + "\n"
    out, _, runlog, tmp = harness('OFFER_REQ_TS="11-22 18:25:41.000"; offer_timeout_diag', logcat=lc)
    f = os.path.join(tmp, "diag", "offer-timeout.logcat")
    assert os.path.exists(f), (out, runlog)
    body = open(f, encoding="utf-8").read()
    assert "loadData pending=0" in body and "APP STATE" in body and "bruit" not in body, body
    assert "offer_timeout_diag" in runlog and "loadData pending=0" in runlog, runlog


@case
def test_timeout_branch_calls_offer_timeout_diag():
    m = re.search(r'if \[ "\$offer_seen" != "1" \]; then\n(.*?)\nfi\n', RUN, re.S)
    assert m, "branche TIMEOUT introuvable"
    assert "offer_timeout_diag" in m.group(1), m.group(1)


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
