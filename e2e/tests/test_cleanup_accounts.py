#!/usr/bin/env python3
"""Pre-run cleanup: closes open rides of rider 106 / driver 23 only, never deletes."""
import json, os, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(rider_rides, driver_rides, env_extra=None, fail_paths=()):
    tmp = tempfile.mkdtemp(prefix="e2e-cleanup-")
    smoke = os.path.join(tmp, "smoke.log"); open(smoke, "w").close()
    env = dict(os.environ)
    env.update({"BASE": "https://staging.example.test", "OUT": tmp, "STEP": "ping",
                "RIDER_TOKEN": "r", "DRIVER_TOKEN": "d", "DEV_TOKEN": "x", "SMOKE_LOG": smoke})
    env.pop("GALASS_STAGING_URL", None)
    env.update(env_extra or {})
    script = (
        "import sys, json, urllib.request\n"
        "class _R:\n    status = 200\n    def read(self): return b'{\"data\":{}}'\n"
        "    def __enter__(self): return self\n    def __exit__(self, *a): return False\n"
        "urllib.request.urlopen = lambda *a, **k: _R()\n"
        f"sys.path.insert(0, {ROOT!r})\n"
        "import api_flow as a\n"
        f"R = json.loads({json.dumps(rider_rides)!r}); D = json.loads({json.dumps(driver_rides)!r})\n"
        f"FAIL = {list(fail_paths)!r}\n"
        "CALLS = []\n"
        "def close(rid):\n"
        "    for L in (R, D):\n"
        "        for r in L:\n"
        "            if int(r['id']) == rid: r['status'] = 9\n"
        "def fake(m, path, tok, body=None, timeout=40):\n"
        "    CALLS.append([m, path])\n"
        "    if path == 'ride/list': return 200, {'data': {'rides': {'data': R}}}\n"
        "    if path in ('driver/rides/active', 'driver/rides/accepted'): return 200, {'data': {'rides': D}}\n"
        "    if any(path.startswith(f) for f in FAIL): return 400, {'status': 'error'}\n"
        "    if m == 'POST' and ('cancel' in path or '/end/' in path):\n"
        "        close(int(path.rsplit('/', 1)[1])); return 200, {'status': 'success'}\n"
        "    return 200, {'status': 'success'}\n"
        "a.call = fake\n"
        "ok = a.cleanup_accounts()\n"
        "print('CALLS', json.dumps(CALLS)); print('OK', ok)\n")
    p = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env,
                       cwd=os.path.dirname(ROOT))
    assert p.returncode == 0, p.stderr
    calls = json.loads([l for l in p.stdout.splitlines() if l.startswith("CALLS ")][0][6:])
    return p.stdout, calls


def test_running_ride_249_is_ended_and_counters_reach_zero():
    out, calls = run([{"id": 249, "status": 3, "payment_status": 3, "driver_id": 23}], [])
    assert "CLEANUP_CLOSED 249 status 3 via driver/rides/end" in out, out
    assert "CLEANUP_OPEN rider=0 driver=0" in out and "OK True" in out, out


def test_pending_rider_ride_is_cancelled_by_rider():
    out, calls = run([{"id": 300, "status": 0, "payment_status": 0, "driver_id": 0}], [])
    assert ["POST", "ride/cancel/300"] in calls, calls
    assert "OK True" in out


def test_driver_only_ride_of_driver_23_is_closed_other_drivers_ignored():
    out, calls = run([], [{"id": 301, "status": 2, "driver_id": 23}, {"id": 302, "status": 2, "driver_id": 99}])
    assert "CLEANUP_CLOSED 301" in out, out
    assert not any("302" in p for _, p in calls), calls


def test_never_deletes_and_never_reads_driver_request_list():
    _, calls = run([{"id": 249, "status": 3}], [{"id": 301, "status": 0, "driver_id": 23}])
    assert all(m in ("GET", "POST") for m, _ in calls), calls
    assert not any(p == "driver/rides/list" for _, p in calls), calls


def test_other_accounts_are_refused():
    out, calls = run([{"id": 249, "status": 3}], [], {"RIDER_ID": "107"})
    assert "CLEANUP_REFUSED" in out and calls == [], (out, calls)


def test_unclosable_ride_reports_left_and_false():
    out, _ = run([{"id": 249, "status": 3}], [], fail_paths=("driver/rides/end",))
    assert "CLEANUP_LEFT 249" in out and "OK False" in out, out


def _svc(driver_info, dashboard):
    tmp = tempfile.mkdtemp(); smoke = os.path.join(tmp, "s.log"); open(smoke, "w").close()
    env = dict(os.environ, BASE="https://staging.example.test", OUT=tmp, STEP="ping",
               RIDER_TOKEN="r", DRIVER_TOKEN="d", DEV_TOKEN="x", SMOKE_LOG=smoke)
    env.pop("GALASS_STAGING_URL", None)
    script = (
        "import sys, json, urllib.request\n"
        "class _R:\n    status = 200\n    def read(self): return b'{\"data\":{}}'\n"
        "    def __enter__(self): return self\n    def __exit__(self, *a): return False\n"
        "urllib.request.urlopen = lambda *a, **k: _R()\n"
        f"sys.path.insert(0, {ROOT!r})\nimport api_flow as a\nCALLS=[]\n"
        f"DI = json.loads({json.dumps(driver_info)!r}); DB = json.loads({json.dumps(dashboard)!r})\n"
        "def fake(m, path, tok, body=None, timeout=40):\n"
        "    CALLS.append([m, path])\n"
        "    return 200, (DI if path == 'driver/driver-info' else DB)\n"
        "a.call = fake\nprint('IDX', a.service_index()); print('CALLS', json.dumps(CALLS))\n")
    p = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=os.path.dirname(ROOT))
    assert p.returncode == 0, p.stderr
    return p.stdout


def test_service_index_maps_driver_service_to_rider_card_order_read_only():
    out = _svc({"data": {"driver": {"service_id": 3, "service": {"name": "Voiture"}}}},
               {"data": {"services": [{"id": 1, "name": "Economie"}, {"id": 2, "name": "Luxe"}, {"id": 4, "name": "Livraison"}, {"id": 3, "name": "Voiture"}]}})
    assert "SERVICE_INDEX 3 Voiture" in out and "IDX 3" in out, out
    assert '"POST"' not in out, out


def test_service_index_none_when_unknown():
    out = _svc({"data": {}}, {"data": {"services": [{"id": 1}]}})
    assert "SERVICE_INDEX none" in out, out
