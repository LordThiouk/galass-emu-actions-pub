#!/usr/bin/env python3
"""Staging-only multistop E2E API driver.

Env: BASE, RIDER_TOKEN, DRIVER_TOKEN, DEV_TOKEN, OUT, STEP.
STEP: ping|pick|create|accept|start|stops|end|pay|finish|status|all

The script is idempotent enough to be called step by step from run.sh: state
(ride id, responses) is persisted in $OUT/state.json between calls.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("BASE", "").rstrip("/")
# S19: refuse anything that is not the exact staging origin over https. A substring
# check on "galassapp.com" missed http:// and every lookalike host.
if not BASE.startswith("https://"):
    sys.exit(f"refusing non-https BASE={BASE!r}")
EXPECTED = os.environ.get("GALASS_STAGING_URL", "").rstrip("/")
if EXPECTED and BASE != EXPECTED:
    sys.exit(f"refusing BASE={BASE!r}: it is not GALASS_STAGING_URL")
if "galassapp.com" in BASE:
    sys.exit("staging only: BASE must not be prod")

OUT = os.environ.get("OUT", "e2e-out")
os.makedirs(OUT, exist_ok=True)

RT = os.environ["RIDER_TOKEN"]
DT = os.environ["DRIVER_TOKEN"]
DEV = os.environ["DEV_TOKEN"]

RIDER_ID = int(os.environ.get("RIDER_ID", "106"))
DRIVER_ID = int(os.environ.get("DRIVER_ID", "23"))

log = open(f"{OUT}/api.jsonl", "a")


def call(m, path, tok, body=None, timeout=40):
    req = urllib.request.Request(
        f"{BASE}/api/{path}",
        method=m,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {tok}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "dev-token": DEV,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code, txt = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        code, txt = e.code, e.read().decode()
    except Exception as e:  # network error / timeout
        code, txt = 0, json.dumps({"error": str(e)})
    try:
        j = json.loads(txt)
    except Exception:
        j = {"raw": txt[:500]}
    log.write(json.dumps({"t": time.time(), "m": m, "path": path, "code": code,
                          "resp": j}) + "\n")
    log.flush()
    remark = j.get("remark") if isinstance(j, dict) else None
    print(m, path, code, remark, flush=True)
    return code, j


PICK = (14.7167, -17.4677)
DEST = (14.6928, -17.4467)
STOPS = [
    {"latitude": 14.7100, "longitude": -17.4620, "location_label": "E2E Stop 1 Médina"},
    {"latitude": 14.7020, "longitude": -17.4550, "location_label": "E2E Stop 2 Fann"},
]

state_f = f"{OUT}/state.json"
st = json.load(open(state_f)) if os.path.exists(state_f) else {}


def save():
    json.dump(st, open(state_f, "w"), indent=1)


def driver_loc(lat, lng):
    return call("POST", "driver/location-update", DT,
                {"current_lat": lat, "current_lot": lng})


def go_online():
    driver_loc(*PICK)
    # online-status est un TOGGLE côté backend: rebasculer si on est passé offline
    for _ in range(2):
        c, j = call("POST", "driver/online-status", DT,
                    {"online_status": 1, "lat": PICK[0], "long": PICK[1]})
        if ((j or {}).get("data") or {}).get("online"):
            break


def create_ride():
    go_online()
    c, f = call("POST", "ride/fare-and-distance", RT, {
        "pickup_latitude": PICK[0], "pickup_longitude": PICK[1],
        "destination_latitude": DEST[0], "destination_longitude": DEST[1],
        "stops": STOPS,
    })
    st["fare"] = f.get("data")
    c, r = call("POST", "ride/create", RT, {
        "service_id": 3,
        "pickup_latitude": PICK[0], "pickup_longitude": PICK[1],
        "pickup_location": "E2E Pickup Plateau",
        "destination_latitude": DEST[0], "destination_longitude": DEST[1],
        "destination_location": "E2E Dest Point E",
        "number_of_passenger": 1,
        "payment_type": 2,  # cash
        "note": "E2E staging test - no real ride",
        "stops": STOPS,
    })
    ride = (r.get("data") or {}).get("ride") or {}
    if ride.get("id"):
        st["ride_id"] = ride["id"]
        st["ride_create"] = ride
        save()
        print("RIDE_ID", ride["id"], "amount", ride.get("amount"),
              "driver", ride.get("driver_id"), flush=True)
    return ride.get("id")


def open_rides():
    """Rides still waiting/active for the rider, newest first."""
    c, j = call("GET", "ride/list", RT)
    rides = (((j.get("data") or {}).get("rides") or {}).get("data")) or []
    out = []
    for r in rides:
        try:
            status = int(r.get("status"))
        except (TypeError, ValueError):
            continue
        if status in (0, 2, 3):  # pending / active / running
            out.append(r)
    out.sort(key=lambda r: int(r.get("id") or 0), reverse=True)
    return out


def reset_rides():
    """Cancel pending no-driver rides left by an earlier run (called BEFORE composition)."""
    for r in open_rides():
        if int(r.get("status") or 0) == 0 and not int(r.get("driver_id") or 0):
            c, _ = call("POST", f"ride/cancel/{int(r['id'])}", RT, {"cancel_reason": "E2E reset"})
            print("CANCEL_STALE", r["id"], c, flush=True)


def pick_ride():
    """Adopt the newest pending/active rider ride (created by the UI), else create one.

    A ride just composed in the app has status 0 and no driver yet: it must be adopted,
    never cancelled. Only OLDER pending no-driver rides are treated as stale.
    """
    rides = open_rides()
    if rides:
        keep = rides[0]
        for r in rides[1:]:
            if int(r.get("status") or 0) == 0 and not int(r.get("driver_id") or 0):
                c, _ = call("POST", f"ride/cancel/{int(r['id'])}", RT, {"cancel_reason": "E2E stale ride"})
                print("CANCEL_STALE", r["id"], c, flush=True)
        rid = int(keep["id"])
        st["ride_id"] = rid
        st["ride_picked"] = keep
        # B8: an adopted ride was composed in the app; a created one was not.
        st["ride_source"] = "ui"
        save()
        print("PICKED_RIDE", rid, "status", keep.get("status"), flush=True)
        return rid
    rid = create_ride()
    st["ride_source"] = "api"
    save()
    print("CREATED_RIDE", rid, flush=True)
    return rid


def ride_id():
    return st.get("ride_id")


def details():
    rid = ride_id()
    if not rid:
        return {}
    c, j = call("GET", f"ride/details/{rid}", RT)
    return (j.get("data") or {}).get("ride") or {}


def driver_details():
    rid = ride_id()
    if not rid:
        return {}
    c, j = call("GET", f"driver/rides/details/{rid}", DT)
    return (j.get("data") or {}).get("ride") or {}


def accept_offer(retries=20, delay=3):
    rid = ride_id()
    go_online()
    for i in range(retries):
        c, j = call("POST", "driver/ride/offer/respond", DT,
                    {"ride_id": rid, "action": "accept"})
        if j.get("status") == "success" or j.get("remark") == "offer_accepted":
            st["offer_accept"] = j
            save()
            return True
        remark = j.get("remark")
        if remark == "not_authorized" or remark == "ride_unavailable":
            time.sleep(delay)
            continue
        # already assigned to this driver, or another terminal answer
        d = driver_details()
        if int(d.get("driver_id") or 0) == DRIVER_ID:
            return True
        time.sleep(delay)
    return False


def start_ride():
    d = driver_details()
    if int(d.get("driver_id") or 0) != DRIVER_ID:
        return False
    otp = d.get("otp")
    c, j = call("POST", f"driver/rides/start/{d.get('id')}", DT, {"otp": otp})
    st["start"] = j
    save()
    return j.get("status") == "success"


def complete_stops():
    rid = ride_id()
    if not rid:
        return False
    ok = True
    for i in range(len(STOPS)):
        driver_loc(STOPS[i]["latitude"], STOPS[i]["longitude"])
        hook = os.environ.get("STOP_HOOK")
        if hook:
            # S20: argv, never a shell string — the hook path is configuration, not code.
            subprocess.run([hook, str(i)], check=False)
        c, s = call("POST", f"driver/rides/complete-stop/{rid}", DT)
        st.setdefault("stops_resp", []).append(s)
        save()
        remark = s.get("remark")
        if remark == "stop_completed" or remark == "all_stops_completed":
            continue
        if remark == "stop_too_far":
            time.sleep(2)
            driver_loc(STOPS[i]["latitude"], STOPS[i]["longitude"])
            c, s = call("POST", f"driver/rides/complete-stop/{rid}", DT)
            st.setdefault("stops_resp", []).append(s)
            save()
            if s.get("status") == "success":
                continue
        ok = False
        break
    return ok


def end_ride():
    rid = ride_id()
    driver_loc(*DEST)
    c, e = call("POST", f"driver/rides/end/{rid}", DT,
                {"latitude": DEST[0], "longitude": DEST[1]})
    st["end"] = e
    save()
    return e.get("status") == "success"


def pay_rider():
    rid = ride_id()
    c, p = call("POST", f"ride/payment/{rid}", RT,
                {"payment_type": 2, "tips_amount": 0})
    st["payment"] = p
    save()


def cash_driver():
    rid = ride_id()
    c, r = call("POST", f"driver/rides/received-cash-payment/{rid}", DT)
    st["cash_received"] = r
    save()


def pay():
    pay_rider()
    cash_driver()
    return True


def status():
    d = driver_details()
    r = details()
    c, pn = call("GET", "push-notifications", RT)
    st["driver_final"] = d
    st["rider_final"] = r
    st["rider_push"] = pn.get("data")
    st.setdefault("stops", d.get("stops") or r.get("stops") or [])
    save()
    print(json.dumps({
        "ride_id": ride_id(),
        "status": r.get("status"),
        "driver_id": d.get("driver_id") or r.get("driver_id"),
        "stops": st.get("stops"),
        "payment_status": r.get("payment_status"),
    }), flush=True)


step = os.environ.get("STEP", "all")

if step == "ping":
    call("GET", "general-setting", RT)
elif step == "reset":
    reset_rides()
elif step == "pick":
    pick_ride()
elif step == "cancel":
    if ride_id():
        c, j = call("POST", f"ride/cancel/{ride_id()}", RT, {"cancel_reason": "E2E cleanup"})
        print("CANCEL", ride_id(), c, flush=True)
elif step == "push_count":
    c, pn = call("GET", "push-notifications", RT)
    data = pn.get("data") or {}
    total = data.get("total") if isinstance(data, dict) else None
    print("PUSH_TOTAL", total, flush=True)
elif step == "create":
    create_ride()
elif step == "accept":
    accept_offer()
elif step == "start":
    start_ride()
elif step == "stops":
    complete_stops()
elif step == "loc_stop":
    i = int(os.environ.get("STOP_INDEX", "0"))
    driver_loc(STOPS[i]["latitude"], STOPS[i]["longitude"])
elif step == "loc_dest":
    driver_loc(*DEST)
elif step == "ride_state":
    # état compact (statut, arrêts faits, paiement) pour décider du repli API
    d = driver_details()
    done = sum(1 for x in (d.get("stops") or []) if x.get("completed_at"))
    print(f"status={d.get('status')} stops_done={done} payment_status={d.get('payment_status')}", flush=True)
elif step == "stop_one":
    # Repli API borné à UN arrêt (celui que l'UI a raté), jamais tous d'un coup.
    rid = ride_id()
    # S5: the fallback always completes the first PENDING stop, whatever index the
    # UI loop was on; indexing into `pending` skipped a stop after one UI success.
    pending = [x for x in (driver_details().get("stops") or []) if not x.get("completed_at")]
    if not rid:
        sys.exit("stop_one: no ride")
    if not pending:
        print("STOP_ONE none", "no_pending_stop", flush=True)
        sys.exit(1)
    driver_loc(pending[0]["latitude"], pending[0]["longitude"])
    c, resp = call("POST", f"driver/rides/complete-stop/{rid}", DT)
    st.setdefault("stops_resp", []).append(resp)
    save()
    print("STOP_ONE", pending[0].get("id"), resp.get("remark"), flush=True)
elif step == "stops_pending":
    d = driver_details()
    rid = ride_id()
    for i, x in enumerate(d.get("stops") or []):
        if x.get("completed_at"):
            continue
        driver_loc(x["latitude"], x["longitude"])
        c, r = call("POST", f"driver/rides/complete-stop/{rid}", DT)
        st.setdefault("stops_resp", []).append(r)
        save()
    st["stops"] = (driver_details().get("stops") or [])
    save()
elif step == "end":
    end_ride()
elif step == "pay":
    pay()
elif step == "pay_rider":
    pay_rider()
elif step == "cash_driver":
    cash_driver()
elif step == "finish":
    if not ride_id():
        pick_ride()
    if not start_ride():
        accept_offer()
        start_ride()
    complete_stops()
    end_ride()
    pay()
    status()
elif step == "status":
    status()
elif step == "all":
    go_online()
    create_ride()
    accept_offer()
    start_ride()
    complete_stops()
    end_ride()
    pay()
    status()
else:
    sys.exit(f"unknown STEP={step}")
