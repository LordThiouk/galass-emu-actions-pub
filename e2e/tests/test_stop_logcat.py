#!/usr/bin/env python3
"""Run 45058950773 : les 3 taps « Confirmer l'arrêt » ne font pas avancer stops_done, et les fichiers
diag/dbg-stop1-aN.logcat ne contiennent que ~5 s de polling `dashboard` : `tail -60` écrase les lignes
`complete-stop request/response/ignored` du Driver (de750b7), seules capables de dire si la requête part
et ce que le serveur répond. Contrat : ces lignes sont gardées en premier, le bruit de polling est écarté,
et stop_diag les recopie dans run.log. Sans émulateur : `adb` est un faux."""
import os
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
UI = os.path.join(ROOT, "ui.sh")

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


FAKE_ADB = r'''#!/bin/bash
echo "adb $*" >> "$FAKE_CALLS"
case "$*" in
  *pidof*) echo 4242 ;;
  *"logcat"*) cat "$FAKE_LOGCAT" ;;
esac
exit 0
'''

H = "11-22 19:40:%02d.%03d  4242  4242 I flutter : "
OTHER = "11-22 19:40:%02d.%03d  9999  9999 I flutter : "


def logcat_with_polling():
    rows = [
        H % (38, 1) + "│ 💡 complete-stop request: ride=311 stop=1/2 pending=2 stop_pos=14.71,-17.462 ping_pos=14.5,-17.4 ping_age_s=40 dist_m=27000",
        H % (40, 2) + "│ 💡 complete-stop response: http=403 remark=stop_too_far outcome=rejected message=Trop loin",
    ]
    for i in range(30):
        s = 41 + (i // 10)
        rows += [
            H % (s, i) + "│ #0   printX (package:galass_driver/core/helper/string_format_helper.dart:224:7)",
            H % (s, i) + "│ 💡 url--------------http://example.invalid/api/driver/dashboard?page=1",
            H % (s, i) + "│ 💡 [dispatch] dashboard raw pending_rides=0 active_offer=false",
            H % (s, i) + "│ 💡 [dispatch] loadData pending=0 online=true page=1 driverId=23",
            H % (s, i) + "│ 💡 Service already running",
        ]
    rows.append(OTHER % (41, 5) + "│ 💡 complete-stop bruit d'un autre processus")
    return "\n".join(rows) + "\n"


def harness(body, logcat):
    tmp = tempfile.mkdtemp(prefix="e2e-stoplc-")
    fake = os.path.join(tmp, "bin")
    os.makedirs(fake)
    with open(os.path.join(fake, "adb"), "w") as fh:
        fh.write(FAKE_ADB)
    os.chmod(os.path.join(fake, "adb"), 0o755)
    open(os.path.join(tmp, "logcat.txt"), "w", encoding="utf-8").write(logcat)
    script = ('export OUT="%s" FAKE_CALLS="%s/calls" FAKE_LOGCAT="%s/logcat.txt" DRIVER_PKG=sn.galass.galassdriver; '
              'export PATH="%s:$PATH"; mkdir -p "$OUT/diag"; : > "$FAKE_CALLS"; smoke_emit() { :; }; . "%s"; '
              'ui_xml() { return 1; }; %s' % (tmp, tmp, tmp, fake, UI, body))
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, cwd=tmp)
    runlog = os.path.join(tmp, "run.log")
    return p.stdout, (open(runlog, encoding="utf-8").read() if os.path.exists(runlog) else ""), tmp


@case
def test_complete_stop_lines_survive_dashboard_polling():
    out, _, _ = harness('app_logcat_since "11-22 19:40:30.000" decisive', logcat_with_polling())
    assert "complete-stop request: ride=311" in out, out[:600]
    assert "complete-stop response: http=403 remark=stop_too_far" in out, out[:600]


@case
def test_polling_noise_and_other_processes_are_dropped():
    out, _, _ = harness('app_logcat_since "11-22 19:40:30.000" decisive', logcat_with_polling())
    assert "Service already running" not in out and "loadData" not in out, out[:400]
    assert "dashboard raw" not in out and "bruit" not in out, out[:400]


@case
def test_stop_diag_copies_complete_stop_lines_into_run_log():
    _, runlog, tmp = harness('STOP_TAP_TS="11-22 19:40:30.000"; stop_diag stop1-a1', logcat_with_polling())
    assert "complete-stop request: ride=311" in runlog, runlog[-800:]
    assert "complete-stop response: http=403 remark=stop_too_far" in runlog, runlog[-800:]
    f = open(os.path.join(tmp, "diag", "dbg-stop1-a1.logcat"), encoding="utf-8").read()
    assert "complete-stop response: http=403" in f, f[:400]


def accept_logcat():
    rows = [
        H % (47, 30) + "│ 💡 APP STATE -> AppLifecycleState.resumed",
        H % (47, 52) + "│ 💡 [dispatch] presentOffer rideId=312 callKit=false modal=true ok=true",
        H % (47, 58) + "│ 💡 [dispatch] respond accept rideId=312",
        H % (47, 59) + "│ 💡 [dispatch] respond HTTP=403 status=error remark=not_authorized message=[Offre expirée]",
    ]
    for i in range(30):
        rows += [H % (48, i) + "│ 💡 [dispatch] loadData pending=0 online=true page=1 driverId=23",
                 H % (48, i) + "│ 💡 Service already running"]
    return "\n".join(rows) + "\n"


@case
def test_accept_fail_diag_keeps_dispatch_respond_lines():
    """Fenêtre d'acceptation (run 45065480752 : offre vue, tap « Accepter » sans effet, offer_expired) :
    la réponse du Driver à l'offre (respond HTTP/remark) doit être écrite, polling écarté."""
    _, runlog, tmp = harness('OFFER_REQ_TS="11-22 19:40:20.000"; accept_fail_diag', accept_logcat())
    f = os.path.join(tmp, "diag", "accept-fail.logcat")
    assert os.path.exists(f), runlog
    body = open(f, encoding="utf-8").read()
    assert "respond HTTP=403" in body and "presentOffer rideId=312" in body and "APP STATE" in body, body
    assert "Service already running" not in body and "loadData" not in body, body
    assert "accept_fail_diag" in runlog and "respond HTTP=403" in runlog, runlog


@case
def test_run_sh_calls_accept_fail_diag_when_ui_accept_fails():
    run = open(os.path.join(ROOT, "run.sh"), encoding="utf-8").read()
    i = run.index('mark driver-accepted-details FAIL "api fallback: accept"')
    assert "accept_fail_diag" in run[i - 400:i], run[i - 400:i]


if __name__ == "__main__":
    failed = 0
    for fn in CASES:
        try:
            fn()
            print("ok  ", fn.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", fn.__name__, str(e)[:300])
    print("%d/%d cas passés" % (len(CASES) - failed, len(CASES)))
    raise SystemExit(1 if failed else 0)
