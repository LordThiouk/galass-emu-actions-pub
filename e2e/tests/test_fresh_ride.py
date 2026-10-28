#!/usr/bin/env python3
"""TDD spec for the FRESH-ride rule (run 40669473919 reused ride 230).

Run 40669473919 adopted `rides[0]` while it was status=3 payment_status=3 (paid,
finished), so the whole flow replayed a dead ride. Pins:
  * ISC-6: `pick_ride()` adopts ONLY status==0 AND payment_status==0 AND no driver;
    every other newest ride is rejected (logged, never adopted) and a fresh ride is
    created; rides in 2/3 or with a payment are NEVER cancelled (active/terminal),
    only a stale pending-no-driver candidate is;
  * ISC-7: `fresh_ride_id=<id> status=<s> payment_status=<ps> source=<src>` lands in
    the smoke log on BOTH paths (adopted => ui, created => api);
  * Anti-ride: the exact ride-230 shape can never become `st['ride_id']`.

No network: `call` is stubbed, and `urllib.request.urlopen` is replaced before
import so the STEP=ping call at import opens no socket.
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.path.join(ROOT, "api_flow.py")

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def run_api_flow(script, rides, state=None, create=True):
    """Import the REAL api_flow.py against a temp OUT; ride/list is stubbed."""
    tmp = tempfile.mkdtemp(prefix="e2e-fresh-ride-")
    smoke = os.path.join(tmp, "smoke.log")
    open(smoke, "w", encoding="utf-8").close()
    if state is not None:
        with open(os.path.join(tmp, "state.json"), "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    env = dict(os.environ)
    env.update({"BASE": "https://staging.example.test", "OUT": tmp, "STEP": "ping",
                "RIDER_TOKEN": "r", "DRIVER_TOKEN": "d", "DEV_TOKEN": "x",
                "SMOKE_LOG": smoke})
    env.pop("GALASS_STAGING_URL", None)
    create_stub = (
        "def fake_create():\n"
        "    CREATED.append(True)\n"
        "    a.st['ride_id'] = 232\n"
        "    a.st['ride_create'] = {'id': 232, 'status': 0, 'payment_status': 0}\n"
        "    return 232\n" if create else
        "def fake_create():\n"
        "    CREATED.append(True)\n"
        "    return 0\n")
    stub = (
        "import sys, json, urllib.request\n"
        "class _R:\n"
        "    status = 200\n"
        "    def read(self): return b'{\"data\":{}}'\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *a): return False\n"
        "urllib.request.urlopen = lambda *a, **k: _R()\n"
        "sys.path.insert(0, %r)\n"
        "import api_flow as a\n"
        "RIDES = json.loads(%r)\n"
        "CALLS = []\n"
        "def fake_call(m, path, tok, body=None, timeout=40):\n"
        "    CALLS.append([m, path])\n"
        "    if path == 'ride/list':\n"
        "        return 200, {'data': {'rides': {'data': RIDES}}}\n"
        "    return 200, {'data': {}}\n"
        "a.call = fake_call\n"
        "CREATED = []\n"
        % (ROOT, json.dumps(rides))
        + create_stub
        + "a.create_ride = fake_create\n"
    )
    proc = subprocess.run([sys.executable, "-c", stub + script], capture_output=True,
                          text=True, env=env, cwd=os.path.dirname(ROOT))
    state_path = os.path.join(tmp, "state.json")
    after = json.load(open(state_path, encoding="utf-8")) if os.path.exists(state_path) else {}
    return tmp, proc, after, open(smoke, encoding="utf-8").read()


PICK_SCRIPT = (
    "rid = a.pick_ride()\n"
    "st = json.load(open(a.state_f))\n"
    "print('CALLS', json.dumps(CALLS))\n"
    "print('PICKED', json.dumps({\n"
    "    'returned': rid, 'ride_id': st.get('ride_id'),\n"
    "    'fresh_ride_id': st.get('fresh_ride_id'),\n"
    "    'fresh_status': st.get('fresh_ride_status'),\n"
    "    'fresh_payment': st.get('fresh_payment_status'),\n"
    "    'fresh_source': st.get('fresh_ride_source'),\n"
    "    'ride_source': st.get('ride_source'),\n"
    "    'created': bool(CREATED)}))\n"
)


def picked(stdout):
    line = [l for l in stdout.splitlines() if l.startswith("PICKED ")][0]
    return json.loads(line[len("PICKED "):])


def calls(stdout):
    line = [l for l in stdout.splitlines() if l.startswith("CALLS ")][0]
    return json.loads(line[len("CALLS "):])


def assert_ok(proc):
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)


# ------------------------------------------------------------------ ISC-6 (a)
@case
def test_a_terminal_paid_ride_is_rejected_and_a_fresh_one_created():
    rides = [{"id": 230, "status": 3, "payment_status": 3, "driver_id": 0}]
    _tmp, proc, state, smoke = run_api_flow(PICK_SCRIPT, rides)
    assert_ok(proc)
    got = picked(proc.stdout)
    assert "REJECTED_RIDE 230 status=3 payment_status=3" in proc.stdout, proc.stdout
    assert "(non fraîche — jamais adoptée)" in proc.stdout, proc.stdout
    assert got["created"] is True, got
    assert got["fresh_ride_id"] == "232", got
    assert got["fresh_status"] == 0 and got["fresh_payment"] == 0, got
    assert got["fresh_source"] == "api", got
    assert got["ride_id"] != 230, "la ride 230 a été adoptée"
    cancelled = [c for c in calls(proc.stdout) if c[1].startswith("ride/cancel/")]
    assert cancelled == [], f"une ride terminale a été annulée: {cancelled}"
    assert "fresh_ride_id=232 status=0 payment_status=0 source=api" in smoke, smoke
    assert state.get("fresh_ride_id") == "232", state


# ------------------------------------------------------------------ ISC-6 (b)
@case
def test_a_pending_unpaid_driverless_ride_is_adopted():
    rides = [{"id": 231, "status": 0, "payment_status": 0, "driver_id": 0}]
    _tmp, proc, state, smoke = run_api_flow(PICK_SCRIPT, rides)
    assert_ok(proc)
    got = picked(proc.stdout)
    assert got["created"] is False, got
    assert got["ride_id"] == 231 and got["fresh_ride_id"] == "231", got
    assert got["fresh_status"] == 0 and got["fresh_payment"] == 0, got
    assert got["fresh_source"] == "ui" and got["ride_source"] == "ui", got
    assert "fresh_ride_id=231 status=0 payment_status=0 source=ui" in smoke, smoke
    assert state.get("fresh_ride_id") == "231", state


# ------------------------------------------------------------------ ISC-6 (c)
@case
def test_only_the_stale_pending_candidate_is_cancelled():
    rides = [{"id": 241, "status": 0, "payment_status": 0, "driver_id": 0},
             {"id": 239, "status": 0, "payment_status": 0, "driver_id": 0}]
    _tmp, proc, _state, _smoke = run_api_flow(PICK_SCRIPT, rides)
    assert_ok(proc)
    got = picked(proc.stdout)
    assert got["ride_id"] == 241 and got["created"] is False, got
    cancelled = [c[1] for c in calls(proc.stdout) if c[1].startswith("ride/cancel/")]
    assert cancelled == ["ride/cancel/239"], cancelled
    assert "CANCEL_STALE 239" in proc.stdout, proc.stdout
    assert "CANCEL_STALE 241" not in proc.stdout, proc.stdout


@case
def test_active_and_paid_rides_are_never_cancelled():
    rides = [{"id": 250, "status": 2, "payment_status": 0, "driver_id": 7},
             {"id": 249, "status": 0, "payment_status": 3, "driver_id": 0}]
    _tmp, proc, _state, _smoke = run_api_flow(PICK_SCRIPT, rides)
    assert_ok(proc)
    got = picked(proc.stdout)
    assert got["created"] is True and got["ride_id"] != 250, got
    cancelled = [c[1] for c in calls(proc.stdout) if c[1].startswith("ride/cancel/")]
    assert cancelled == [], f"une ride active/payée a été annulée: {cancelled}"
    assert "REJECTED_RIDE 250 status=2 payment_status=0" in proc.stdout, proc.stdout


# -------------------------------------------------------------- Anti-ride (d)
@case
def test_the_ride_230_shape_can_never_become_the_current_ride():
    rides = [{"id": 230, "status": 3, "payment_status": 3, "driver_id": 0}]
    _tmp, proc, state, _smoke = run_api_flow(PICK_SCRIPT, rides, state={"ride_id": 230})
    assert_ok(proc)
    got = picked(proc.stdout)
    assert got["ride_id"] != 230, "la ride 230 réutilisée est restée ride_id"
    assert state.get("ride_id") != 230, state
    assert got["fresh_ride_id"] == "232", got


# ------------------------------------------------- order / guard (ISC-6 e-f)
@case
def test_the_newest_adoptable_ride_wins_over_a_non_adoptable_newest():
    """A terminal newest ride must not push the loop onto an OLDER adoptable one."""
    rides = [{"id": 250, "status": 3, "payment_status": 3, "driver_id": 0},
             {"id": 241, "status": 0, "payment_status": 0, "driver_id": 0}]
    _tmp, proc, _state, _smoke = run_api_flow(PICK_SCRIPT, rides)
    assert_ok(proc)
    got = picked(proc.stdout)
    assert got["created"] is False, got
    assert got["ride_id"] == 241, got
    cancelled = [c[1] for c in calls(proc.stdout) if c[1].startswith("ride/cancel/")]
    assert cancelled == [], f"une ride active/terminale a été annulée: {cancelled}"
    assert "TOMBSTONE_RIDE 250" in proc.stdout, proc.stdout


@case
def test_the_list_order_never_decides_which_ride_is_adopted():
    """Not id-descending: the max-id adoptable ride wins, the lower one is cancelled."""
    rides = [{"id": 239, "status": 0, "payment_status": 0, "driver_id": 0},
             {"id": 241, "status": 0, "payment_status": 0, "driver_id": 0}]
    _tmp, proc, _state, _smoke = run_api_flow(PICK_SCRIPT, rides)
    assert_ok(proc)
    got = picked(proc.stdout)
    assert got["ride_id"] == 241, got
    cancelled = [c[1] for c in calls(proc.stdout) if c[1].startswith("ride/cancel/")]
    assert cancelled == ["ride/cancel/239"], cancelled


@case
def test_a_rejected_ride_and_a_failed_create_purge_the_state():
    """No ride survived: state is purged and the run still proves it (fresh_ride_id=none)."""
    rides = [{"id": 230, "status": 3, "payment_status": 3, "driver_id": 0}]
    state = {"ride_id": 230, "ride_picked": {"id": 230},
             "ride_create": {"id": 230, "status": 3}}
    _tmp, proc, after, smoke = run_api_flow(PICK_SCRIPT, rides, state=state, create=False)
    assert_ok(proc)
    got = picked(proc.stdout)
    assert not got["returned"], got
    assert "ride_id" not in after, after
    assert "ride_picked" not in after, after
    assert "ride_create" not in after, after
    assert got["fresh_ride_id"] == "none", got
    assert got["fresh_source"] == "none", got
    assert "fresh_ride_id=none status=- payment_status=- source=none" in smoke, smoke


# ----------------------------------------------------------- reset_rides (ISC-6)
@case
def test_reset_rides_reuses_the_adoptable_predicate():
    """Payment/driver/status guards are the SAME predicate as pick_ride (audit R7).

    An unparsable driver_id is NOT "driverless": `_ride_int` used to map it to 0,
    so reset_rides cancelled a ride a real driver may own. The safe side is "has
    a driver" — 305 is neither adopted nor cancelled (Max M3).
    """
    rides = [
        {"id": 301, "status": 0, "payment_status": 0, "driver_id": 0},   # stale
        {"id": 302, "status": 0, "payment_status": 3, "driver_id": 0},   # paid
        {"id": 303, "status": 0, "payment_status": 0, "driver_id": 9},   # has driver
        {"id": 304, "status": 2, "payment_status": 0, "driver_id": 0},   # active
        # present but unparsable => not proven driverless: never cancelled
        {"id": 305, "status": 0, "payment_status": 0, "driver_id": "n/a"},
    ]
    _tmp, proc, _state, _smoke = run_api_flow(
        "a.reset_rides()\nprint('CALLS', json.dumps(CALLS))\n", rides)
    assert_ok(proc)
    # open_rides() is id-descending, but only 301 is proven stale
    cancelled = [c[1] for c in calls(proc.stdout) if c[1].startswith("ride/cancel/")]
    assert sorted(cancelled) == ["ride/cancel/301"], cancelled


@case
def test_an_unparsable_driver_id_is_neither_adopted_nor_cancelled():
    """Max M3: `driver_id: "n/a"` / an object is not 0. A ride that may already
    have a driver must never be adopted (which would hijack it) nor cancelled
    (which would kill it). Absent/null still means driverless."""
    rides = [{"id": 311, "status": 0, "payment_status": 0, "driver_id": "n/a"},
             {"id": 312, "status": 0, "payment_status": 0, "driver_id": {"id": 7}}]
    _tmp, proc, state, _smoke = run_api_flow(PICK_SCRIPT, rides)
    assert_ok(proc)
    got = picked(proc.stdout)
    assert got["ride_id"] != 311 and got["ride_id"] != 312, got
    assert got["fresh_source"] == "api", got
    cancelled = [c[1] for c in calls(proc.stdout) if c[1].startswith("ride/cancel/")]
    assert cancelled == [], f"une ride au conducteur non prouvé a été annulée: {cancelled}"
    assert "TOMBSTONE_RIDE 311" in proc.stdout, proc.stdout
    # the tombstone reason must not claim "active ou terminale" for a PENDING ride
    assert "conducteur non prouvé" in proc.stdout, proc.stdout
    assert state.get("fresh_ride_id") == "232", state


@case
def test_an_unparsable_payment_status_is_not_adopted():
    """Max M3: an unparsable payment_status is not proven unpaid — the safe side
    is NOT adoptable, and it is never cancelled either."""
    rides = [{"id": 321, "status": 0, "payment_status": "n/a", "driver_id": 0},
             {"id": 322, "status": 0, "payment_status": {"cents": 500},
              "driver_id": 0}]
    _tmp, proc, _state, _smoke = run_api_flow(PICK_SCRIPT, rides)
    assert_ok(proc)
    got = picked(proc.stdout)
    assert got["ride_id"] not in (321, 322), got
    assert got["fresh_source"] == "api", got
    cancelled = [c[1] for c in calls(proc.stdout) if c[1].startswith("ride/cancel/")]
    assert cancelled == [], f"une ride non prouvée impayée a été annulée: {cancelled}"
    assert "payment_status=-" in proc.stdout, proc.stdout


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
    print(f"\n{len(CASES) - failed}/{len(CASES)} fresh-ride cases passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
