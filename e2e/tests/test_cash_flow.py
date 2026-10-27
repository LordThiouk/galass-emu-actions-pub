#!/usr/bin/env python3
"""TDD spec for the cash-flow ordering (e2e/cash_flow.py + run.sh).

Run 40205373538 called `driver/rides/received-cash-payment` BEFORE the rider had
asked for cash, so the backend answered `not_found` and the verdict blamed the
harness. Pins:

  * `poll_payment_status` waits for `payment_status == 3` (REQUESTED_STATUS),
    polls AT LEAST once (timeout 0 = one check), and never claims a request on
    an absent/invalid status;
  * `api_flow.py` exposes `cash_wait` (bounded poll, writes $OUT/cash.json) and
    `cash_driver` skips the POST when the cash was never requested;
  * run.sh polls BEFORE the "Confirmer le paiement" tap and skips both the tap
    and `api cash_driver` when the cash was not requested.
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
sys.path.insert(0, ROOT)

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def cash():
    import cash_flow
    return cash_flow


def run_text():
    return open(RUN, encoding="utf-8").read()


def api_text():
    return open(API, encoding="utf-8").read()


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


# ------------------------------------------------------------ pure polling
@case
def test_requested_status_is_three():
    f = cash()
    assert f.REQUESTED_STATUS == 3, f.REQUESTED_STATUS


@case
def test_poll_until_the_rider_asks_for_cash():
    f = cash()
    clock = FakeClock()
    seq = iter([0, 0, 3])
    out = f.poll_payment_status(lambda: next(seq), clock.sleep, timeout=10,
                                interval=2, clock=clock)
    assert out["requested"] is True, out
    assert out["payment_status"] == 3, out
    assert out["cause"] == "cash_requested", out
    assert out["polls"] == 3, out
    assert out["waited"] == 4.0, out


@case
def test_poll_timeout_reports_cash_not_requested():
    f = cash()
    clock = FakeClock()
    out = f.poll_payment_status(lambda: 0, clock.sleep, timeout=5, interval=2, clock=clock)
    assert out["requested"] is False, out
    assert out["cause"] == "cash_not_requested", out
    assert out["payment_status"] == 0, out
    # it really waited: at t=0,2,4 then a 1s capped sleep to the deadline.
    assert out["polls"] == 4, out
    assert out["waited"] == 5.0, out


@case
def test_zero_timeout_is_a_single_check():
    f = cash()
    clock = FakeClock()
    out = f.poll_payment_status(lambda: 0, clock.sleep, timeout=0, interval=2, clock=clock)
    assert out["polls"] == 1, out
    assert out["requested"] is False and out["cause"] == "cash_not_requested", out

    clock = FakeClock()
    out = f.poll_payment_status(lambda: 3, clock.sleep, timeout=0, interval=2, clock=clock)
    assert out["polls"] == 1 and out["requested"] is True, out


@case
def test_absent_or_invalid_status_is_never_a_request():
    f = cash()
    # no status at all = we could not READ the state, not "the rider did not ask"
    for value in (None, "", "abc", "3.5"):
        clock = FakeClock()
        out = f.poll_payment_status(lambda v=value: v, clock.sleep, timeout=0,
                                    interval=1, clock=clock)
        assert out["requested"] is False, (value, out)
        assert out["cause"] == "cash_status_unavailable", (value, out)
    # a real, readable, non-3 status IS "not requested"
    for value in (-1, 0, 2, 1):
        clock = FakeClock()
        out = f.poll_payment_status(lambda v=value: v, clock.sleep, timeout=0,
                                    interval=1, clock=clock)
        assert out["requested"] is False, (value, out)
        assert out["cause"] == "cash_not_requested", (value, out)


@case
def test_timeout_with_no_readable_status_is_unavailable():
    f = cash()
    clock = FakeClock()
    out = f.poll_payment_status(lambda: None, clock.sleep, timeout=4, interval=2,
                                clock=clock)
    assert out["requested"] is False, out
    assert out["payment_status"] is None, out
    assert out["cause"] == "cash_status_unavailable", out


@case
def test_driver_label_maps_the_observed_status():
    f = cash()
    assert f.driver_label(3) == "cash_requested", f.driver_label(3)
    assert f.driver_label(1) == "cash_already_paid", f.driver_label(1)
    assert f.driver_label(None) == "cash_status_unavailable", f.driver_label(None)
    for value in (0, 2, -1):
        assert f.driver_label(value) == "cash_not_requested", (value, f.driver_label(value))
    # an unparseable value is as unreadable as a missing one
    for value in ("abc", "", "3.5"):
        assert f.driver_label(value) == "cash_status_unavailable", (value, f.driver_label(value))


@case
def test_poll_settings_are_bounded():
    f = cash()
    ok = f.poll_settings(None, None)
    assert ok["ok"] is True, ok
    assert (ok["timeout"], ok["interval"]) == (30.0, 2.0), ok
    edge = f.poll_settings("1", "30")
    assert edge["ok"] is True and (edge["timeout"], edge["interval"]) == (1.0, 30.0), edge
    edge = f.poll_settings("600", "1")
    assert edge["ok"] is True and (edge["timeout"], edge["interval"]) == (600.0, 1.0), edge
    for sec, interval in (("0", "2"), ("601", "2"), ("abc", "2"),
                          ("30", "0"), ("30", "31"), ("30", "abc")):
        bad = f.poll_settings(sec, interval)
        assert bad["ok"] is False, (sec, interval, bad)
        assert bad["error"], (sec, interval, bad)


@case
def test_cash_flow_is_importable_without_env():
    """Pure module: no BASE/token read at import time."""
    proc = subprocess.run([sys.executable, "-c", "import cash_flow; print('ok')"],
                          capture_output=True, text=True, cwd=ROOT,
                          env={"PATH": os.environ.get("PATH", "")})
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert proc.stdout.strip() == "ok", proc.stdout


# ------------------------------------------------------- api_flow wiring
@case
def test_api_flow_cash_wait_and_defensive_cash_driver():
    body = api_text()
    assert "cash_flow" in body and "poll_payment_status" in body, body
    assert re.search(r'CASH_POLL_SEC', body), "CASH_POLL_SEC absent"
    assert re.search(r'CASH_POLL_INTERVAL', body), "CASH_POLL_INTERVAL absent"
    assert 'f"{OUT}/cash.json"' in body or "'/cash.json'" in body or "cash.json" in body
    assert re.search(r'print\(\s*f?"CASH_WAIT requested=', body), body

    driver = re.search(r"^def cash_driver\(\):(.*?)^def ", body, re.M | re.S)
    assert driver, "cash_driver() introuvable"
    block = driver.group(1)
    assert 'print("CASH_SKIP", label' in block, block
    assert "driver_label(" in block, "le libellé doit venir de l'état observé"
    # the POST is guarded by the requested label, never unconditional
    i_guard = block.index('label != "cash_requested"')
    i_post = block.index("received-cash-payment")
    assert i_guard < i_post, "le POST précède la garde"


# ------------------------------------------------- behavioral api_flow runs
def _api_flow(stub_extra, env_extra):
    tmp = tempfile.mkdtemp(prefix="e2e-cash-api-")
    env = dict(os.environ)
    env.update({"BASE": "https://staging.example.test", "OUT": tmp, "STEP": "ping",
                "RIDER_TOKEN": "r", "DRIVER_TOKEN": "d", "DEV_TOKEN": "x"})
    env.pop("GALASS_STAGING_URL", None)
    env.update(env_extra)
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
    proc = subprocess.run([sys.executable, "-c", stub + stub_extra],
                          capture_output=True, text=True, env=env,
                          cwd=os.path.dirname(ROOT))
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    return tmp, proc.stdout


def run_cash_driver(status):
    script = (
        "posts = []\n"
        "a.call = lambda *x, **k: (posts.append(x[1]), (200, {}))[1]\n"
        "a.payment_status = lambda: %r\n"
        "a.cash_driver()\n"
        "state = json.load(open(a.OUT + '/cash.json'))\n"
        "print(state.get('cause'), len(posts))\n" % (status,)
    )
    tmp, out = _api_flow(script, {})
    state = json.load(open(os.path.join(tmp, "cash.json")))
    posts = 0 if status != 3 else 1
    return {"label": state.get("cause"), "posts": posts}, out


def run_cash_wait(env_extra, status=0):
    script = (
        "a.payment_status = lambda: %r\n"
        "sys.exit(a.cash_wait())\n" % (status,)
    )
    tmp = tempfile.mkdtemp(prefix="e2e-cash-wait-")
    env = dict(os.environ)
    env.update({"BASE": "https://staging.example.test", "OUT": tmp, "STEP": "ping",
                "RIDER_TOKEN": "r", "DRIVER_TOKEN": "d", "DEV_TOKEN": "x"})
    env.pop("GALASS_STAGING_URL", None)
    env.pop("CASH_POLL_SEC", None)
    env.pop("CASH_POLL_INTERVAL", None)
    env.update(env_extra)
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
                          capture_output=True, text=True, env=env,
                          cwd=os.path.dirname(ROOT))
    return proc.returncode, proc.stdout + proc.stderr


# ----------------------------------------------------------- run.sh order
@case
def test_run_sh_polls_before_the_tap_and_skips_the_premature_call():
    body = run_text()
    i_wait = body.index("api cash_wait")
    i_tap = body.index('tap_exact "Confirmer le paiement"')
    i_driver = re.search(r"^\s*api cash_driver ", body, re.M).start()
    assert i_wait < i_tap < i_driver, (i_wait, i_tap, i_driver)

    # The not-requested branch: FAIL mark carrying the observed cause, no tap,
    # no cash_driver, no exit (the run continues with a verdict).
    section = body[i_wait:i_driver]
    branch = re.search(r'if \[ "\$CASH_RC" = "0" \]; then(.*?)^fi$', section, re.M | re.S)
    assert branch, section[:600]
    skip = branch.group(1)
    assert "CASH_CAUSE" in skip, skip
    assert re.search(r'mark driver-cash-confirmed FAIL "\$\{?CASH_CAUSE', skip), skip
    assert "tap_exact" not in skip, skip
    assert "api cash_driver" not in skip, skip
    assert "exit" not in skip, skip

    # the tap + API fallback live inside the requested guard
    guard = re.search(r'if \[ "\$CASH_REQUESTED" = "1" \]; then(.*?)^fi$', body, re.M | re.S)
    assert guard, "garde CASH_REQUESTED introuvable"
    assert 'tap_exact "Confirmer le paiement"' in guard.group(1), guard.group(1)
    assert "api cash_driver" in guard.group(1), guard.group(1)


@case
def test_run_sh_fail_detail_carries_the_cause_from_cash_json():
    body = run_text()
    section = body[body.index("# --------------------------------------------------------------- cash flow"):]
    assert "cash.json" in section, section[:600]
    # no constant may claim the rider's behaviour
    assert 'FAIL "cash_not_requested"' not in section, section[:800]
    assert 'FAIL "cash_status_unavailable"' not in section, section[:800]
    # a config error is its own verdict, and says nothing about the rider
    assert "config_error" in section, section[:800]
    assert re.search(r'mark driver-cash-confirmed FAIL "cash config_error"', section), section[:800]
    # rc 4 must be distinguished from rc 3 (not requested)
    assert re.search(r"CASH_RC", section), section[:800]
    assert re.search(r'= "4" \]', section), section[:800]


@case
def test_api_flow_cash_wait_returns_four_on_a_config_error():
    body = api_text()
    fn = re.search(r"^def cash_wait\(\):(.*?)^def ", body, re.M | re.S)
    assert fn, "cash_wait() introuvable"
    assert "config_error" in fn.group(1), fn.group(1)
    assert re.search(r"return 4", fn.group(1)), fn.group(1)
    # the env is validated at CALL time: a bad value must not crash the import
    assert re.search(r'elif step == "cash_wait":\s*\n\s*sys\.exit\(cash_wait\(\)\)', body), body


@case
def test_cash_driver_labels_the_observed_status_and_never_posts():
    """Run the REAL cash_driver() against stubbed collaborators."""
    cases = [(3, "cash_requested", 1), (1, "cash_already_paid", 0),
             (None, "cash_status_unavailable", 0), (0, "cash_not_requested", 0),
             (2, "cash_not_requested", 0)]
    for status, label, posts in cases:
        out, stdout = run_cash_driver(status)
        assert stdout.strip().endswith(f"{label} {posts}"), (status, stdout)
        assert out["label"] == label, (status, out)
        assert out["posts"] == posts, (status, out)


@case
def test_cash_wait_exits_four_when_the_budget_env_is_invalid():
    for env in ({"CASH_POLL_SEC": "0"}, {"CASH_POLL_SEC": "abc"},
                {"CASH_POLL_INTERVAL": "31"}, {"CASH_POLL_INTERVAL": "0"}):
        rc, out = run_cash_wait(env)
        assert rc == 4, (env, rc, out)
        assert "CASH_WAIT config_error" in out, (env, out)


@case
def test_cash_wait_exits_three_only_on_a_real_status():
    rc, out = run_cash_wait({"CASH_POLL_SEC": "1", "CASH_POLL_INTERVAL": "1"},
                            status=0)
    assert rc == 3, (rc, out)
    assert "cash_not_requested" in out, out
    rc, out = run_cash_wait({"CASH_POLL_SEC": "1", "CASH_POLL_INTERVAL": "1"},
                            status=None)
    assert rc == 3, (rc, out)
    assert "cash_status_unavailable" in out, out


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
    print(f"\n{len(CASES) - failed}/{len(CASES)} cash-flow cases passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
