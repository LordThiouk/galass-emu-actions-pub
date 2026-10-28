#!/usr/bin/env python3
"""TDD spec — a backend refusal of the cash is NEVER a PASS (run 40602473815).

The run reported `received-cash-payment 200` with `remark: "not_found"` on a
separate backend run: HTTP 200, cash refused. The harness read the status code
and called it a success, so a backend-side refusal could have been written up as
a product PASS.

Pins:
  * `cash_driver()` classifies the BODY, not just the status: an explicit
    `not_found` is `backend_not_found`, an HTTP failure is `http_<code>`, and
    both are rc!=0;
  * the cause lands in `$OUT/cash.json` (the one place run.sh reads it from) and
    on stdout as `CASH_FAIL <cause>` — never "PASS"/"OK";
  * `STEP=cash_driver` exits non-zero so run.sh can branch on it, and run.sh
    materialises the point as FAIL with the cause;
  * report.py aggregates worst-wins, so no surface can outvote that FAIL.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = os.path.join(ROOT, "run.sh")
API = os.path.join(ROOT, "api_flow.py")
REPO = os.path.dirname(ROOT)
sys.path.insert(0, ROOT)

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def text(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _api_flow(script):
    """Run the REAL api_flow with urlopen stubbed and a throwaway $OUT."""
    tmp = tempfile.mkdtemp(prefix="e2e-cash-honesty-")
    env = dict(os.environ)
    env.update({"BASE": "https://staging.example.test", "OUT": tmp, "STEP": "ping",
                "RIDER_TOKEN": "r", "DRIVER_TOKEN": "d", "DEV_TOKEN": "x"})
    env.pop("GALASS_STAGING_URL", None)
    with open(os.path.join(tmp, "state.json"), "w", encoding="utf-8") as fh:
        json.dump({"ride_id": 4242}, fh)
    stub = (
        "import sys, json, urllib.request\n"
        "class _R:\n"
        "    status = 200\n"
        "    def read(self): return b'{\"data\":{}}'\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *a): return False\n"
        "urllib.request.urlopen = lambda *a, **k: _R()\n"
        "sys.path.insert(0, %r)\n"
        "import api_flow as a\n" % ROOT
    )
    proc = subprocess.run([sys.executable, "-c", stub + script],
                          capture_output=True, text=True, env=env, cwd=REPO)
    return tmp, proc


def result_line(stdout):
    for line in stdout.splitlines():
        if line.startswith("RESULT "):
            return line.split()[1:]
    raise AssertionError(f"RESULT absent: {stdout!r}")


@case
def test_backend_not_found_is_never_a_success():
    """200 + `{"remark": "not_found"}` is a refusal, not a confirmation."""
    tmp, proc = _api_flow(
        "posts = []\n"
        "a.call = lambda *x, **k: (posts.append(x[1]), (200, {'remark': 'not_found'}))[1]\n"
        "a.payment_status = lambda: 3\n"
        "ok = a.cash_driver()\n"
        "state = json.load(open(a.OUT + '/cash.json'))\n"
        "print('RESULT', bool(ok), state.get('cause'))\n")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    ok, cause = result_line(proc.stdout)
    assert ok == "False", "200 not_found classé succès: " + proc.stdout
    assert cause == "backend_not_found", (cause, proc.stdout)
    assert "CASH_FAIL backend_not_found" in proc.stdout, proc.stdout
    for forbidden in ("CASH_OK", "CASH_PASS", " PASS"):
        assert forbidden not in proc.stdout, (forbidden, proc.stdout)
    # the cause is persisted where run.sh reads it
    assert json.load(open(os.path.join(tmp, "cash.json")))["cause"] == "backend_not_found"


@case
def test_a_real_confirmation_still_succeeds():
    """The fix must not turn a genuine confirmation into a failure."""
    for body in ({"status": "success", "remark": "success"},
                 {"remark": "cash_confirmed"},
                 {"data": {"payment_status": "cash_received"}}):
        tmp, proc = _api_flow(
            "a.call = lambda *x, **k: (200, %r)\n"
            "a.payment_status = lambda: 3\n"
            "ok = a.cash_driver()\n"
            "print('RESULT', bool(ok))\n" % (body,))
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        assert result_line(proc.stdout) == ["True"], (body, proc.stdout)
        assert "CASH_FAIL" not in proc.stdout, (body, proc.stdout)


@case
def test_an_unreadable_or_unknown_200_body_is_never_a_success():
    """200 with `{}`, a non-JSON body or an unknown remark proves nothing.

    The old rule read "no refusal token => success", so a body the harness could
    not parse was written up as a confirmation. Silence is not consent: only a
    body that NAMES the success closes the point.
    """
    for body in ({}, "", "not json at all", {"remark": "???"}, {"data": {}},
                 {"remark": ""}):
        tmp, proc = _api_flow(
            "a.call = lambda *x, **k: (200, %r)\n"
            "a.payment_status = lambda: 3\n"
            "ok = a.cash_driver()\n"
            "state = json.load(open(a.OUT + '/cash.json'))\n"
            "print('RESULT', bool(ok), state.get('cause'))\n" % (body,))
        assert proc.returncode == 0, (body, proc.stdout, proc.stderr)
        ok, cause = result_line(proc.stdout)
        assert ok == "False", (body, proc.stdout)
        assert cause == "unknown_body", (body, cause, proc.stdout)
        assert "CASH_FAIL unknown_body" in proc.stdout, (body, proc.stdout)
        for forbidden in ("CASH_OK", "CASH_PASS", " PASS"):
            assert forbidden not in proc.stdout, (body, forbidden, proc.stdout)
        assert json.load(open(os.path.join(tmp, "cash.json")))["cause"] == "unknown_body"


@case
def test_a_negated_success_is_never_a_success():
    """`cash_not_received` names the success AND negates it — never a PASS.

    A substring match on "received" reads the backend's own refusal as a
    confirmation: the falsifier of ISC-8 stays reachable until negation is
    honoured. `not_found` keeps its own cause; `cash_received` is unchanged.
    """
    # Round 3 moved the classifier to an exact enum allowlist: a negated success is
    # no longer a named refusal, it is simply not a verdict — `unknown_body`. The
    # invariant this case guards is unchanged: never success, cause always explicit.
    for body, cause_want in (({"remark": "cash_not_received"}, "unknown_body"),
                             ({"remark": "payment not received"}, "unknown_body"),
                             ({"message": "Cash not confirmed by the backend"},
                              "unknown_body"),
                             ({"remark": "aucun paiement reçu"}, "unknown_body")):
        tmp, proc = _api_flow(
            "a.call = lambda *x, **k: (200, %r)\n"
            "a.payment_status = lambda: 3\n"
            "ok = a.cash_driver()\n"
            "state = json.load(open(a.OUT + '/cash.json'))\n"
            "print('RESULT', bool(ok), state.get('cause'))\n" % (body,))
        assert proc.returncode == 0, (body, proc.stdout, proc.stderr)
        ok, cause = result_line(proc.stdout)
        assert ok == "False", (body, proc.stdout)
        assert cause == cause_want, (body, cause, proc.stdout)
        for forbidden in ("CASH_OK", "CASH_PASS", " PASS"):
            assert forbidden not in proc.stdout, (body, forbidden, proc.stdout)
        assert json.load(open(os.path.join(tmp, "cash.json")))["cause"] == cause


@case
def test_only_an_exact_success_value_is_a_success():
    """The endpoint's verdict is an ENUM, not prose: substring matching is a mirror.

    Max's counter-examples all contain a success token and all mean the opposite
    (or nothing at all): `unsuccessful`, `successful_but_refused`,
    `cash_received_false`, `received: false`. And `success notification sent` is
    prose, not a verdict — the negation blocklist read it as a refusal, the mirror
    image of the same bug. Only an exact value closes the point.
    """
    counter = ({"remark": "unsuccessful"},
               {"remark": "successful_but_refused"},
               {"remark": "failure - success rollback"},
               {"remark": "cash_received but rejected"},
               {"remark": "cash_received_false"},
               {"remark": "received: false"},
               {"remark": "pas confirmed"},
               {"message": "success notification sent"})
    for body in counter:
        tmp, proc = _api_flow(
            "a.call = lambda *x, **k: (200, %r)\n"
            "a.payment_status = lambda: 3\n"
            "ok = a.cash_driver()\n"
            "state = json.load(open(a.OUT + '/cash.json'))\n"
            "print('RESULT', bool(ok), state.get('cause'))\n" % (body,))
        assert proc.returncode == 0, (body, proc.stdout, proc.stderr)
        ok, cause = result_line(proc.stdout)
        assert ok == "False", (body, proc.stdout)
        assert cause == "unknown_body", (body, cause, proc.stdout)
        for forbidden in ("CASH_OK", "CASH_PASS", " PASS"):
            assert forbidden not in proc.stdout, (body, forbidden, proc.stdout)
        assert json.load(open(os.path.join(tmp, "cash.json")))["cause"] == "unknown_body"

    # ... and the REAL backend verdicts still close the point
    for body in ({"remark": "success"},
                 {"status": "success", "remark": "success"},
                 {"remark": "cash_confirmed"},
                 {"remark": "cash_received"},
                 {"remark": "confirmed"},
                 {"data": {"payment_status": "cash_received"}}):
        tmp, proc = _api_flow(
            "a.call = lambda *x, **k: (200, %r)\n"
            "a.payment_status = lambda: 3\n"
            "ok = a.cash_driver()\n"
            "print('RESULT', bool(ok))\n" % (body,))
        assert proc.returncode == 0, (body, proc.stdout, proc.stderr)
        assert result_line(proc.stdout) == ["True"], (body, proc.stdout)
        assert "CASH_FAIL" not in proc.stdout, (body, proc.stdout)


@case
def test_an_http_failure_is_classified_with_its_status():
    tmp, proc = _api_flow(
        "a.call = lambda *x, **k: (500, {'remark': 'error'})\n"
        "a.payment_status = lambda: 3\n"
        "ok = a.cash_driver()\n"
        "state = json.load(open(a.OUT + '/cash.json'))\n"
        "print('RESULT', bool(ok), state.get('cause'))\n")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    ok, cause = result_line(proc.stdout)
    assert ok == "False", proc.stdout
    assert cause.startswith("http_"), (cause, proc.stdout)
    assert "CASH_FAIL" in proc.stdout, proc.stdout


@case
def test_step_cash_driver_exits_non_zero_on_a_refusal():
    body = text(API)
    assert re.search(r'elif step == "cash_driver":\s*\n\s*sys\.exit\(0 if cash_driver\(\) else 1\)',
                     body), body[-2000:]
    assert re.search(r"^def cash_refusal\(", body, re.M), "classification du corps absente"
    fn = re.search(r"^def cash_driver\(\):(.*?)^def ", body, re.M | re.S)
    assert fn, "cash_driver() introuvable"
    block = fn.group(1)
    assert "cash_refusal" in block, block
    assert re.search(r'print\(f?"CASH_FAIL', block), block
    # the POST guard from the previous ISA is untouched
    assert block.index('label != "cash_requested"') < block.index("received-cash-payment")


@case
def test_run_sh_materialises_the_cause_and_can_never_mark_it_pass():
    body = text(RUN)
    assert "mark driver-cash-confirmed PASS" not in body, \
        "un refus backend pourrait être écrit comme PASS"
    section = body[body.index("api cash_wait"):body.index("cap driver-cash-confirmed")]
    assert "api cash_driver" in section, section[-1500:]
    assert "CASH_DRIVER_RC" in section, "le rc de cash_driver n'est pas capturé"
    after = section[section.index("api cash_driver"):]
    # the cause is re-read from cash.json AFTER the POST and turned into a FAIL
    assert "cash.json" in after, after[:1200]
    i_fail = after.index("mark driver-cash-confirmed FAIL")
    assert i_fail > after.index("cash.json") or after.count("mark driver-cash-confirmed FAIL") >= 2, after
    assert "CASH_CAUSE" in after, after
    assert "backend" in after, "aucune explication « backend séparé » dans le harnais"


@case
def test_report_worst_wins_keeps_a_backend_refusal_failed():
    import report

    assert "driver-cash-confirmed" in report.P0_DEFAULT, "le point n'est plus P0"
    rows = [
        {"point": "driver-cash-confirmed", "result": "PASS", "detail": "ui"},
        {"point": "driver-cash-confirmed", "result": "FAIL", "detail": "backend_not_found"},
    ]
    out = report.aggregate(rows)
    assert [r["result"] for r in out] == ["FAIL"], out
    assert out[0]["detail"] == "backend_not_found", out


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
