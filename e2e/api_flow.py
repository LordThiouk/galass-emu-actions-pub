#!/usr/bin/env python3
"""Staging-only multistop E2E API driver.

Env: BASE, RIDER_TOKEN, DRIVER_TOKEN, DEV_TOKEN, OUT, STEP.
STEP: ping|pick|create|accept|start|stops|end|pay|pay_rider|cash_wait|cash_driver
      |finish|status|all

STEP=stops|all|stop_one refuses to complete anything unless E2E_GEOFENCE_OK=1:
`run.sh` `geofence_stop` is the ONLY path that injects a fix and proves the
emulator applied it, and the backend must never see a `complete-stop` it did not
vouch for. `run.sh` scopes that variable to the single `api stop_one` call.
`loc_stop` always posts a stop read from `$OUT/state.json` (the same stop
`geofence_stop` verified), never a hardcoded copy, and stays ungated on purpose:
its sole caller runs immediately after `geofence_stop` succeeded.

The script is idempotent enough to be called step by step from run.sh: state
(ride id, responses) is persisted in $OUT/state.json between calls.
"""
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("BASE", "").rstrip("/")
# S19: refuse anything that is not the exact staging origin over https. A substring
# check on "galassapp.com" missed http:// and every lookalike host.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from base_guard import check_base  # noqa: E402
from sanitize import mask_path, redact_record, smoke_emit  # noqa: E402
from offer_flow import run_accept  # noqa: E402
from cash_flow import poll_payment_status, poll_settings, driver_label  # noqa: E402
_reason = check_base(BASE)
if _reason:
    sys.exit(f"refusing BASE: {_reason}")
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

_raw_fh = open(f"{OUT}/api.jsonl", "a")


def log(record):
    """Raw record in api.jsonl (encrypted evidence), redacted copy to smoke log."""
    line = json.dumps(record)
    _raw_fh.write(line + "\n")
    _raw_fh.flush()
    smoke_emit(json.dumps(redact_record(record)))


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
    log({"t": time.time(), "m": m, "path": path, "code": code, "resp": j})
    remark = j.get("remark") if isinstance(j, dict) else None
    print(m, mask_path(path), code, remark, flush=True)
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


def _ride_int(ride, key, default=0):
    """Backend integers are sometimes strings; a missing/unparsable one is `default`.

    `default=0` is the value the backend itself means by "not set" — see
    `open_rides`: a ride the backend did not price yet is still pending payment
    (payment_status 0), so it stays adoptable. Callers that must NOT read an
    absent field as 0 pass their own default.
    """
    try:
        return int(ride.get(key))
    except (TypeError, ValueError):
        return default


def _present_int(ride, key):
    """`(known, value)` for a numeric ride field, absence vs garbage split.

    Absent or null keeps the backend's own 0 default — there is nothing to judge.
    A PRESENT value that does not parse is UNKNOWN, never 0: coercing it made an
    exotic `payment_status` look unpaid and an exotic `driver_id` look driverless,
    so such a ride was adopted AND cancelled (the old loud ValueError was safer).
    """
    if key not in ride or ride.get(key) is None:
        return True, 0
    try:
        return True, int(ride.get(key))
    except (TypeError, ValueError):
        return False, None


def _ride_label(ride, key):
    """Published form of a numeric ride field: the number, or `-` when unknown.

    Never prints the 0 default for a value that was present and unparsable —
    that 0 is exactly the claim `adoptable()` refuses to make.
    """
    known, value = _present_int(ride, key)
    return value if known else "-"


def open_rides():
    """Rides still waiting/active for the rider, newest first.

    `status` and `payment_status` are normalised to int here, so every caller
    judges the SAME values the backend sent (run 40669473919 read status=3
    payment_status=3 and still adopted it). A missing payment_status reads as 0:
    a ride the backend did not price yet is still pending payment. An UNPARSABLE
    payment_status keeps its raw value: `adoptable()` must be able to see that it
    was never proven unpaid.
    """
    c, j = call("GET", "ride/list", RT)
    rides = (((j.get("data") or {}).get("rides") or {}).get("data")) or []
    out = []
    for r in rides:
        try:
            status = int(r.get("status"))
        except (TypeError, ValueError):
            continue
        if status in (0, 2, 3):  # pending / active / running
            ride = dict(r)
            ride["status"] = status
            known, payment_status = _present_int(ride, "payment_status")
            if known:
                ride["payment_status"] = payment_status
            out.append(ride)
    out.sort(key=lambda r: int(r.get("id") or 0), reverse=True)
    return out


def adoptable(ride):
    """The ONLY adoptable shape: pending, unpaid, no driver (ISC-6).

    Anything else belongs to another run: status 2/3 is an active/terminal ride
    and a non-zero payment_status is money already moved. Adopting either replays
    a dead ride (run 40669473919 reused ride 230, status=3 payment_status=3).

    A present-but-unparsable field lands on the SAFE side, never on 0: an
    unparsable payment_status is not proven unpaid (so it is not adopted), and an
    unparsable driver_id is not proven driverless — it may name a real driver, so
    the ride is neither adopted nor cancelled. Absent fields keep their 0 default.
    """
    if _ride_int(ride, "status") != 0:
        return False
    pay_known, pay = _present_int(ride, "payment_status")
    drv_known, drv = _present_int(ride, "driver_id")
    return pay_known and pay == 0 and drv_known and drv == 0


def record_fresh_ride(rid, ride, source):
    """Publish the ride THIS run works on, on both the adopted and created paths."""
    status = _ride_int(ride, "status")
    payment_status = _ride_int(ride, "payment_status")
    st["fresh_ride_id"] = str(rid)
    st["fresh_ride_status"] = status
    st["fresh_payment_status"] = payment_status
    st["fresh_ride_source"] = source
    save()
    smoke_emit(f"fresh_ride_id={rid} status={status} "
               f"payment_status={payment_status} source={source}")
    return status, payment_status


def record_no_fresh_ride():
    """No ride survived this run (adopt rejected AND create failed): prove it.

    Same sanitized smoke line as the two real paths, with `none`/`-` values, so
    every run carries the proof line and no later step can replay a stale ride.
    """
    st["fresh_ride_id"] = "none"
    st["fresh_ride_status"] = "-"
    st["fresh_payment_status"] = "-"
    st["fresh_ride_source"] = "none"
    save()
    smoke_emit("fresh_ride_id=none status=- payment_status=- source=none")


def reset_rides():
    """Cancel stale rides left by an earlier run (called BEFORE composition).

    "Stale" is exactly `adoptable()`: pending (status 0), unpaid (payment_status
    0) and driverless. The old inline `int(driver_id)` cast crashed on a
    non-numeric driver_id and ignored payment_status entirely.
    """
    for r in open_rides():
        if adoptable(r):
            c, _ = call("POST", f"ride/cancel/{int(r['id'])}", RT, {"cancel_reason": "E2E reset"})
            print("CANCEL_STALE", r["id"], c, flush=True)


CLEAN_RIDER_ID, CLEAN_DRIVER_ID = 106, 23
OPEN_STATUSES = (0, 2, 3)


def _rides_in(j):
    """Ride dicts from any list-shaped payload (data.rides.data, data.rides, data...)."""
    out, todo = [], [j]
    while todo:
        x = todo.pop()
        if isinstance(x, dict):
            if "id" in x and "status" in x:
                out.append(x)
            else:
                todo.extend(x.values())
        elif isinstance(x, list):
            todo.extend(x)
    return out


def _driver_open_rides():
    seen = {}
    # Pas driver/rides/list : ce sont les DEMANDES ouvertes d'autres passagers (hors périmètre).
    for path in ("driver/rides/active", "driver/rides/accepted"):
        c, j = call("GET", path, DT)
        if c != 200:
            continue
        for r in _rides_in(j):
            try:
                rid, status = int(r.get("id")), int(r.get("status"))
            except (TypeError, ValueError):
                continue
            drv = r.get("driver_id")
            if drv not in (None, "") and str(drv) != str(CLEAN_DRIVER_ID):
                continue  # pas une course du chauffeur 23
            if status in OPEN_STATUSES:
                seen[rid] = dict(r, id=rid, status=status)
    return sorted(seen.values(), key=lambda r: r["id"])


def _close_one(r, owner):
    """Close ONE open ride by cancel (pending/accepted) or end (running). Never delete."""
    rid, status = int(r["id"]), int(r["status"])
    tries = []
    if status in (0, 2):
        if owner == "rider":
            tries.append(("POST", f"ride/cancel/{rid}", RT, {"cancel_reason": "E2E cleanup pre-run"}))
        tries.append(("POST", f"driver/rides/cancel/{rid}", DT, {"cancel_reason": "E2E cleanup pre-run"}))
    if status == 2:
        tries.append(("POST", f"driver/rides/start/{rid}", DT, None))
    if status in (2, 3):
        tries.append(("POST", f"driver/rides/end/{rid}", DT, {"latitude": DEST[0], "longitude": DEST[1]}))
    for m, path, tok, body in tries:
        c, j = call(m, path, tok, body)
        if c == 200 and (not isinstance(j, dict) or j.get("status") != "error"):
            if "start" in path:
                continue
            return path.split("/" + str(rid))[0]
    return None


def cleanup_accounts():
    """Pre-run: close every non-terminal ride of rider 106 and driver 23 (staging only).

    Strict scope: refuses any other account; uses cancel/end only, never delete.
    Prints CLEANUP_CLOSED <id> <via>, CLEANUP_LEFT <id>, CLEANUP_OPEN rider=<n> driver=<n>.
    Returns True only when both open counters read 0 afterwards.
    """
    if RIDER_ID != CLEAN_RIDER_ID or DRIVER_ID != CLEAN_DRIVER_ID:
        print(f"CLEANUP_REFUSED rider={RIDER_ID} driver={DRIVER_ID}", flush=True)
        return False
    for _ in range(2):
        todo = [(r, "rider") for r in open_rides()]
        known = {int(r["id"]) for r, _ in todo}
        todo += [(r, "driver") for r in _driver_open_rides() if r["id"] not in known]
        if not todo:
            break
        for r, owner in todo:
            via = _close_one(r, owner)
            if via:
                print("CLEANUP_CLOSED", r["id"], "status", r["status"], "via", via, flush=True)
            else:
                print("CLEANUP_LEFT", r["id"], "status", r["status"], flush=True)
    n_r, n_d = len(open_rides()), len(_driver_open_rides())
    print(f"CLEANUP_OPEN rider={n_r} driver={n_d}", flush=True)
    return n_r == 0 and n_d == 0


def pick_ride():
    """Adopt the newest FRESH rider ride (created by the UI), else create one.

    A ride just composed in the app has status 0, payment_status 0 and no driver:
    that is the ONLY adoptable shape. The NEWEST adoptable ride wins — not
    `rides[0]`, because the list is not guaranteed id-descending and a
    non-adoptable newest ride used to make the loop cancel an adoptable OLDER
    one (then create, abandoning a perfectly good ride). Only a stale adoptable
    ride with an id LOWER than the adopted one is cancelled; status 2/3
    (active/terminal) and any paid ride are never cancelled (ISC-6).
    """
    rides = open_rides()
    if rides:
        adopt = None
        for r in rides:
            if adoptable(r) and (adopt is None or int(r["id"]) > int(adopt["id"])):
                adopt = r
        adopted_id = int(adopt["id"]) if adopt is not None else None
        for r in rides:
            if adopt is not None and r is adopt:
                continue
            ride_id_int = int(r["id"])
            if adoptable(r) and (adopted_id is None or ride_id_int < adopted_id):
                # Stale pending-unpaid-driverless ride behind the adopted one.
                c, _ = call("POST", f"ride/cancel/{ride_id_int}", RT,
                            {"cancel_reason": "E2E stale ride"})
                print("CANCEL_STALE", ride_id_int, c, flush=True)
            else:
                # active / terminal / paid / driver not proven absent (or newer
                # than the adopted ride): never cancelled, just left alone.
                print("TOMBSTONE_RIDE", ride_id_int, "status", _ride_int(r, "status"),
                      "payment_status", _ride_label(r, "payment_status"),
                      "(active, terminale, payée ou conducteur non prouvé — "
                      "jamais annulée)", flush=True)
        if adopt is not None:
            rid = adopted_id
            st["ride_id"] = rid
            st["ride_picked"] = adopt
            # B8: an adopted ride was composed in the app; a created one was not.
            st["ride_source"] = "ui"
            save()
            record_fresh_ride(rid, adopt, "ui")
            print("PICKED_RIDE", rid, "status", adopt.get("status"), flush=True)
            return rid
        # Nothing adoptable: name the newest listed ride, never adopt it.
        rejected = rides[0]
        print("REJECTED_RIDE", rejected["id"],
              f"status={_ride_int(rejected, 'status')}",
              f"payment_status={_ride_label(rejected, 'payment_status')}",
              "(non fraîche — jamais adoptée)", flush=True)
    rid = create_ride()
    st["ride_source"] = "api"
    save()
    print("CREATED_RIDE", rid, flush=True)
    if rid:
        record_fresh_ride(rid, st.get("ride_create") or {}, "api")
    else:
        # A rejected newest ride + a failed create must not leave a terminal
        # ride in state: later steps would replay it.
        st.pop("ride_id", None)
        st.pop("ride_picked", None)
        st.pop("ride_create", None)
        save()
        record_no_fresh_ride()
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
    """Ride detail seen by the DRIVER, and the stops it carries.

    Persisted under `st["stops"]` because run.sh's `geofence_stop` reads the stop
    coordinates from `$OUT/state.json`: a hardcoded copy could diverge from the
    ride the backend actually created.
    """
    rid = ride_id()
    if not rid:
        return {}
    c, j = call("GET", f"driver/rides/details/{rid}", DT)
    ride = (j.get("data") or {}).get("ride") or {}
    if ride.get("stops"):
        # Tagged with its ride: a stale list from a previous ride would make
        # geofence_stop inject the wrong coordinates (D4).
        st["stops"] = ride["stops"]
        st["stops_ride"] = rid
        save()
    return ride


def stop_coords(index):
    """(lat, lng) for stop `index` from the ride-detail snapshot, or None.

    This is the SAME stop `run.sh:geofence_stop` verified: the geofence reads the
    coordinates from `$OUT/state.json`, so the backend must be sent those and not
    a hardcoded copy that can diverge from the ride it actually created.
    """
    if isinstance(index, bool) or not isinstance(index, int):
        return None
    stops = st.get("stops") or []
    if index < 0 or index >= len(stops):
        return None
    stop = stops[index] or {}
    try:
        lat = float(stop["latitude"])
        lng = float(stop["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lng)):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
        return None
    return lat, lng


def loc_stop(index):
    """Send the driver to a stop the geofence has coordinates for. Fail closed.

    Deliberately NOT behind `E2E_GEOFENCE_OK`: its sole caller is
    `run.sh:geofence_stop`, which reaches this line only after it injected and
    verified the fix. It posts a location, never a `complete-stop`.
    """
    coords = stop_coords(index)
    if coords is None:
        print(f"LOC_STOP refuse: arrêt {index} absent/invalide dans {state_f} "
              f"(aucun fix envoyé)", file=sys.stderr, flush=True)
        return False
    driver_loc(*coords)
    return True


def renew_offer():
    """Remplacer une offre expirée par une course NEUVE (une seule fois).

    Le backend exclut le driver de la course expirée : répondre à nouveau sur le
    même ride ne peut que re-expirer. `offer_flow.run_accept` garantit l'unicité du
    renouvellement ; ici on ne fait que permuter l'état et le journaliser.
    """
    rid = ride_id()
    if rid:
        call("POST", f"ride/cancel/{rid}", RT, {"cancel_reason": "E2E offer expired"})
        print("OFFER_RENEW cancel", rid, flush=True)
    new_id = create_ride()
    if not new_id:
        return False
    # Course recréée par l'API : la provenance UI de la composition est perdue.
    st["ride_source"] = "api"
    st["ride_picked"] = st.get("ride_create") or {}
    st["offer_renewed"] = 1
    save()
    print("OFFER_RENEW new", new_id, flush=True)
    return True


def ui_tap_proven():
    """Le harnais a-t-il prouvé SON tap ? run.sh exporte E2E_UI_TAP_PROVEN=1
    seulement si le Driver était devant avant ET après le tap réussi."""
    value = str(os.environ.get("E2E_UI_TAP_PROVEN") or "").strip().lower()
    return value in ("1", "true", "yes", "on")


def write_accept(out):
    """$OUT/accept.json : la seule source lue par run.sh pour accept_via/cause."""
    via = out.get("via") or "none"
    cause = out.get("cause")
    if via == "unknown" and not cause:
        # Jamais de cause vide sur un crédit non prouvé.
        cause = "assigned_without_tap_proof"
    payload = {
        "ok": bool(out.get("ok")),
        # dernier code HTTP réellement observé sur offer/respond (0 = aucun appel)
        "code": int(out.get("code") or 0),
        # "ui" quand le tap a gagné sans que l'API reçoive offer_accepted.
        "via": via,
        "cause": cause,
        "renewed": bool(out.get("renewed")),
        "attempts": int(out.get("attempts") or 0),
    }
    with open(f"{OUT}/accept.json", "w") as fh:
        json.dump(payload, fh, indent=1)


def accept_offer(retries=8, delay=2):
    """Accepter l'offre côté API, avec renouvellement UNIQUE en cas d'expiration.

    Borné à la fenêtre d'offre (~22 s utiles) : 8 tentatives x 2 s restent dedans.
    """
    go_online()

    def respond():
        return call("POST", "driver/ride/offer/respond", DT,
                    {"ride_id": ride_id(), "action": "accept"})

    def assigned():
        d = driver_details()
        return int(d.get("driver_id") or 0) == DRIVER_ID

    def renew():
        return renew_offer()

    out = run_accept(respond, assigned, renew, time.sleep,
                     retries=retries, delay=delay, renewed=bool(st.get("offer_renewed")),
                     tap_proven=ui_tap_proven())
    write_accept(out)
    if out["ok"]:
        st["offer_accept"] = {"remark": "offer_accepted", "via": out.get("via"),
                              "renewed": out["renewed"]}
        save()
    print("ACCEPT", "ok" if out["ok"] else "failed", "cause", out.get("cause"),
          "renewed", out["renewed"], "attempts", out["attempts"], flush=True)
    return out["ok"]


def start_ride():
    d = driver_details()
    if int(d.get("driver_id") or 0) != DRIVER_ID:
        return False
    otp = d.get("otp")
    c, j = call("POST", f"driver/rides/start/{d.get('id')}", DT, {"otp": otp})
    st["start"] = j
    save()
    return j.get("status") == "success"


GEOFENCE_ENV = "E2E_GEOFENCE_OK"


def complete_stops():
    """Complete EVERY stop — refused without the run.sh geofence.

    `run.sh:geofence_stop` is the only path that injects a fix, proves the
    emulator applied it, and only then lets `complete-stop` through. Completing
    stops from here would bypass that proof entirely, so it fails closed unless
    the caller positively asserts the geofence ran (run.sh never sets it).
    """
    if os.environ.get(GEOFENCE_ENV) != "1":
        print(f"stops/all refuse: {GEOFENCE_ENV}!=1 - seule la boucle géofencée "
              f"de run.sh (geofence_stop) peut compléter un arrêt", file=sys.stderr,
              flush=True)
        return False
    rid = ride_id()
    if not rid:
        return False
    total = len(st.get("stops") or [])
    if total == 0:
        print("stops/all refuse: aucun arrêt dans state.json", file=sys.stderr, flush=True)
        return False
    ok = True
    for i in range(total):
        coords = stop_coords(i)
        if coords is None:
            print(f"stops/all refuse: arrêt {i} invalide dans state.json",
                  file=sys.stderr, flush=True)
            return False
        driver_loc(*coords)
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
            driver_loc(*coords)
            c, s = call("POST", f"driver/rides/complete-stop/{rid}", DT)
            st.setdefault("stops_resp", []).append(s)
            save()
            if s.get("status") == "success":
                continue
        ok = False
        break
    return ok


def stop_one():
    """Repli API borné à UN arrêt (celui que l'UI a raté), jamais tous d'un coup.

    Same gate as `complete_stops`: a `complete-stop` is only legitimate after
    `run.sh:geofence_stop` injected and PROVED the fix. `run.sh` scopes
    `E2E_GEOFENCE_OK=1` to this one call, right after that proof; nothing else
    sets it.
    """
    if os.environ.get(GEOFENCE_ENV) != "1":
        print(f"stop_one refuse: {GEOFENCE_ENV}!=1 - seule la boucle géofencée "
              f"de run.sh (geofence_stop) peut compléter un arrêt", file=sys.stderr,
              flush=True)
        return False
    rid = ride_id()
    # S5: the fallback always completes the first PENDING stop, whatever index the
    # UI loop was on; indexing into `pending` skipped a stop after one UI success.
    pending = [x for x in (driver_details().get("stops") or []) if not x.get("completed_at")]
    if not rid:
        print("stop_one: no ride", file=sys.stderr, flush=True)
        return False
    if not pending:
        print("STOP_ONE none", "no_pending_stop", flush=True)
        return False
    driver_loc(pending[0]["latitude"], pending[0]["longitude"])
    c, resp = call("POST", f"driver/rides/complete-stop/{rid}", DT)
    st.setdefault("stops_resp", []).append(resp)
    save()
    print("STOP_ONE", pending[0].get("id"), resp.get("remark"), flush=True)
    return True


def complete_pending_stops():
    """Complete every stop the backend still lists as PENDING — same gate as above.

    Third and last `complete-stop` site. `run.sh` never calls this step, so the
    gate costs the run nothing; it stays available only to a hand-written flow
    that positively asserts the geofence ran (`E2E_GEOFENCE_OK=1`). Without that
    proof a `complete-stop` here would bypass `run.sh:geofence_stop` entirely.
    """
    if os.environ.get(GEOFENCE_ENV) != "1":
        print(f"stops_pending refuse: {GEOFENCE_ENV}!=1 - seule la boucle géofencée "
              f"de run.sh (geofence_stop) peut compléter un arrêt", file=sys.stderr,
              flush=True)
        return False
    d = driver_details()
    rid = ride_id()
    if not rid:
        print("stops_pending: no ride", file=sys.stderr, flush=True)
        return False
    for x in d.get("stops") or []:
        if x.get("completed_at"):
            continue
        driver_loc(x["latitude"], x["longitude"])
        c, r = call("POST", f"driver/rides/complete-stop/{rid}", DT)
        st.setdefault("stops_resp", []).append(r)
        save()
    st["stops"] = (driver_details().get("stops") or [])
    save()
    return True


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


def payment_status():
    """`payment_status` as the BACKEND reports it for this ride."""
    return details().get("payment_status")


def write_cash(state):
    with open(f"{OUT}/cash.json", "w") as fh:
        json.dump(state, fh, indent=1)


def cash_wait():
    """Poll until the rider asks for cash (payment_status==3) or the budget ends.

    Run 40205373538: the harness called `received-cash-payment` before the rider
    had asked, the backend answered `not_found`, and the verdict blamed the
    harness. The request is a backend state; it must be observed, never assumed.

    rc: 0 requested, 3 not requested (a REAL non-3 status), 4 config error — an
    out-of-range CASH_POLL_* is reported, never silently clamped.
    """
    config = poll_settings(os.environ.get("CASH_POLL_SEC"),
                           os.environ.get("CASH_POLL_INTERVAL"))
    if not config["ok"]:
        write_cash({"requested": False, "payment_status": None,
                    "cause": "config_error", "waited": 0.0, "polls": 0,
                    "error": config["error"]})
        print(f"CASH_WAIT config_error {config['error']}", flush=True)
        return 4
    state = poll_payment_status(payment_status, time.sleep,
                                timeout=config["timeout"],
                                interval=config["interval"])
    write_cash(state)
    print(f"CASH_WAIT requested={1 if state['requested'] else 0} "
          f"status={state['payment_status']} cause={state['cause']} "
          f"polls={state['polls']} waited={state['waited']}s", flush=True)
    return 0 if state["requested"] else 3


CASH_REFUSAL_TOKENS = ("not_found", "not found")
# A 200 body only closes the point when it NAMES the success: silence (`{}`, a
# non-JSON body, an unknown remark) is not consent (audit R5).
# The endpoint's verdict is an ENUM, matched EXACTLY (lower().strip()) — never as a
# substring: `unsuccessful`, `cash_received_false` and `success notification sent`
# all contain a success token and none of them is a confirmation (audit round 3).
CASH_SUCCESS_VALUES = ("success", "cash_confirmed", "cash_received", "confirmed",
                       "received", "paid")


def _cash_texts(response):
    """Every string a cash response could carry its verdict in, top level first."""
    if isinstance(response, str):
        return [response]
    if not isinstance(response, dict):
        return []
    out = []
    for key in ("remark", "status", "message", "error", "result", "payment_status"):
        value = response.get(key)
        if isinstance(value, dict):
            value = value.get("message") or value.get("remark") or value.get("status")
        if isinstance(value, str):
            out.append(value)
    data = response.get("data")
    if isinstance(data, (dict, str)):
        out += _cash_texts(data)
    return out


def cash_refusal(response):
    """The backend's own refusal class on received-cash-payment, or "".

    Run 40602473815: the endpoint answered HTTP 200 with `remark: "not_found"` on
    a separate backend run — the cash was refused. The status code cannot say
    "cash received" on its own; the body's refusal outranks it.
    """
    for value in _cash_texts(response):
        lowered = value.strip().lower()
        for token in CASH_REFUSAL_TOKENS:
            if token in lowered:
                return "backend_not_found"
    return ""


def cash_confirmation(response):
    """True only when a scanned field IS one of the enum's success values.

    Exact match closes both directions at once: a success hidden inside prose
    (`unsuccessful`, `cash_received_false`) is not a success, and a positive
    sentence (`success notification sent`) is not a refusal either — it is simply
    not a verdict, so it stays `unknown_body`. `{}` proves nothing.
    """
    for value in _cash_texts(response):
        if value.strip().lower() in CASH_SUCCESS_VALUES:
            return True
    return False


def cash_driver():
    """Confirm the driver received the cash — ONLY if the rider asked for it.

    The label reports what was OBSERVED: a payment_status we could not read is
    `cash_status_unavailable`, never "the rider did not ask". And the POST's
    verdict is the BODY: a 200 carrying a refusal is `backend_not_found`, not a
    confirmation (the exact false positive of run 40602473815).
    """
    rid = ride_id()
    state = poll_payment_status(payment_status, time.sleep, timeout=0, interval=0)
    label = driver_label(state["payment_status"])
    # cash.json is the one place run.sh reads the cause from.
    state["cause"] = label
    write_cash(state)
    if label != "cash_requested":
        # Rien à confirmer : appeler l'endpoint ferait un POST que le backend
        # refusera (not_found) et que le rapport imputerait au harnais.
        print("CASH_SKIP", label, flush=True)
        st["cash_received"] = {"skipped": label,
                               "payment_status": state["payment_status"]}
        save()
        return False
    c, r = call("POST", f"driver/rides/received-cash-payment/{rid}", DT)
    st["cash_received"] = r
    save()
    refusal = cash_refusal(r)
    if refusal:
        cause = refusal
    elif c != 200:
        cause = f"http_{c}"
    elif not cash_confirmation(r):
        # 200 + anything that is not an exact success value ({}, non-JSON, prose,
        # `unsuccessful`, `cash_received_false`) is NOT a confirmation.
        cause = "unknown_body"
    else:
        cause = ""
    if cause:
        # run.sh reads the cause here: a refusal must be written even on HTTP 200.
        # A real confirmation leaves the poll label alone (`cash_requested`).
        state["cause"] = cause
        write_cash(state)
        print(f"CASH_FAIL {cause}", flush=True)
        return False
    print("CASH_OK", flush=True)
    return True


def pay():
    pay_rider()
    # Jamais de POST si le rider n'a pas demandé le cash (voir cash_driver()).
    cash_driver()
    return True


def status():
    d = driver_details()
    r = details()
    c, pn = call("GET", "push-notifications", RT)
    st["driver_final"] = d
    st["rider_final"] = r
    st["rider_push"] = pn.get("data")
    fresh = d.get("stops") or r.get("stops") or []
    rid = ride_id()
    # A new ride invalidates the persisted stops: never keep the previous ride's
    # coordinates around for geofence_stop to trust.
    if fresh and st.get("stops_ride") != rid:
        st["stops"] = fresh
        st["stops_ride"] = rid
    st.setdefault("stops", [])
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
elif step == "cleanup":
    sys.exit(0 if cleanup_accounts() else 4)
elif step == "pick":
    pick_ride()
elif step == "rider_rides":
    # Diagnostic lecture seule : courses ouvertes du rider (id, statut, paiement, chauffeur).
    _rr = open_rides()
    print("RIDER_RIDE total", len(_rr), flush=True)
    for r in _rr[:5]:
        print("RIDER_RIDE", r.get("id"), "status", r.get("status"),
              "payment_status", r.get("payment_status"),
              "driver", "oui" if str(r.get("driver_id") or "0") not in ("0", "") else "non",
              "created_at", r.get("created_at"), flush=True)
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
    sys.exit(0 if accept_offer() else 1)
elif step == "start":
    start_ride()
elif step == "stops":
    sys.exit(0 if complete_stops() else 1)
elif step == "loc_stop":
    raw = os.environ.get("STOP_INDEX", "0")
    try:
        i = int(raw)
    except ValueError:
        sys.exit(f"loc_stop: STOP_INDEX={raw!r} invalide")
    sys.exit(0 if loc_stop(i) else 1)
elif step == "loc_dest":
    driver_loc(*DEST)
elif step == "ride_state":
    # état compact (statut, arrêts faits, paiement) pour décider du repli API
    d = driver_details()
    done = sum(1 for x in (d.get("stops") or []) if x.get("completed_at"))
    print(f"status={d.get('status')} stops_done={done} payment_status={d.get('payment_status')}", flush=True)
elif step == "stop_one":
    sys.exit(0 if stop_one() else 1)
elif step == "stops_pending":
    sys.exit(0 if complete_pending_stops() else 1)
elif step == "end":
    end_ride()
elif step == "pay":
    pay()
elif step == "pay_rider":
    pay_rider()
elif step == "cash_wait":
    sys.exit(cash_wait())
elif step == "cash_driver":
    sys.exit(0 if cash_driver() else 1)
elif step == "finish":
    if not ride_id():
        pick_ride()
    if not start_ride():
        accept_offer()
        start_ride()
    if not complete_stops():
        sys.exit(1)
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
    if not complete_stops():
        sys.exit(1)
    end_ride()
    pay()
    status()
else:
    sys.exit(f"unknown STEP={step}")
