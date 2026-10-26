#!/usr/bin/env python3
"""Pure offer/accept rules for the Galass staging E2E (no network, no files).

Run 40205373538 lost the whole Driver chain because a failed accept was never
distinguished from an expired offer: the harness kept retrying a ride the backend
will never offer to that driver again, then cascaded 24 FAIL.

Two invariants live here, testable without any I/O:

  * an offer response classifies into ONE stable kind (accepted / expired /
    not_authorized / ride_unavailable / terminal);
  * an expired offer is renewed AT MOST ONCE (a renewal is a new ride, since the
    backend excludes the driver from the expired one) and then fails with an
    explicit cause instead of looping.

`api_flow.py` only injects its HTTP calls into `run_accept`, so the renewal rule
is exercised by `e2e/tests/test_offer_flow.py` with fakes.
"""

ACCEPTED = "offer_accepted"
EXPIRED = "offer_expired"
NOT_AUTHORIZED = "not_authorized"
RIDE_UNAVAILABLE = "ride_unavailable"
TERMINAL = "terminal"

KINDS = (ACCEPTED, EXPIRED, NOT_AUTHORIZED, RIDE_UNAVAILABLE, TERMINAL)


def classify(code, payload):
    """Map one `driver/ride/offer/respond` result to a stable kind.

    Unknown shapes are TERMINAL on purpose: the API fallback is already a
    fallback, and retrying an unrecognised answer can only burn the offer window.
    """
    body = payload if isinstance(payload, dict) else {}
    remark = str(body.get("remark") or "").strip().lower()
    status = str(body.get("status") or "").strip().lower()
    if remark in ("offer_accepted", "accepted"):
        return ACCEPTED
    if status == "success" and not remark:
        return ACCEPTED
    if remark in ("offer_expired", "expired"):
        return EXPIRED
    if remark in ("not_authorized", "unauthorized"):
        return NOT_AUTHORIZED
    if remark in ("ride_unavailable", "unavailable"):
        return RIDE_UNAVAILABLE
    return TERMINAL


def decide(kind, renewed):
    """Next move for one classified answer: accept | renew | retry | fail.

    `renewed` is the renewal already spent on this ride generation. After it,
    an expiration is final — never a second renewal, never a loop.
    """
    if kind == ACCEPTED:
        return "accept"
    if kind == EXPIRED:
        return "fail" if renewed else "renew"
    if kind in (NOT_AUTHORIZED, RIDE_UNAVAILABLE):
        return "retry"
    return "fail"


def accepted_via(kind, assigned):
    """How the acceptance was obtained: "api" if the backend answered accepted,
    "ui" if the driver is assigned without us getting that answer (the tap won),
    None otherwise. `assigned` is a callable bool."""
    if kind == ACCEPTED:
        return "api"
    if assigned:
        return "ui"
    return None


def failure_cause(kind, renewed):
    """Explicit cause handed to `$OUT/accept.json` and the journal ('' if accepted)."""
    if kind == ACCEPTED:
        return ""
    if kind == EXPIRED:
        return EXPIRED
    return kind if kind in KINDS else TERMINAL


def run_accept(respond, assigned, renew, sleep, retries=20, delay=3, renewed=False):
    """Drive offer acceptance against injected collaborators.

    respond()  -> (http_code, payload)      one accept attempt
    assigned() -> bool                      already assigned to this driver?
    renew()    -> bool                      cancel the expired ride, create a fresh one
    sleep(s)                                injected (no-op in tests)

    Returns {"ok", "via", "cause", "renewed", "attempts"} where `via` is "api"
    (the backend accepted our respond), "ui" (the driver ended up assigned without
    that answer: the tap won) or None. `renew` is called at most once per ride
    generation, and an expired offer is never retried on the same ride.
    """
    attempts = 0
    renewed = bool(renewed)
    last_kind = None
    while attempts < max(0, int(retries)):
        attempts += 1
        code, payload = respond()
        kind = classify(code, payload)
        last_kind = kind
        if kind == ACCEPTED:
            return {"ok": True, "via": "api", "cause": None,
                    "renewed": renewed, "attempts": attempts}
        if assigned():
            # A concurrent accept (or the UI tap) may have won the offer: success,
            # but the credit belongs to the UI, never to the API fallback.
            return {"ok": True, "via": "ui", "cause": None,
                    "renewed": renewed, "attempts": attempts}
        action = decide(kind, renewed)
        if action == "accept":
            return {"ok": True, "via": accepted_via(kind, assigned),
                    "cause": None, "renewed": renewed, "attempts": attempts}
        if action == "renew":
            if renew():
                renewed = True
                continue
            return {"ok": False, "via": None, "cause": EXPIRED,
                    "renewed": renewed, "attempts": attempts}
        if action == "fail":
            return {"ok": False, "via": None,
                    "cause": failure_cause(kind, renewed),
                    "renewed": renewed, "attempts": attempts}
        sleep(delay)
    return {"ok": False, "via": None,
            "cause": failure_cause(last_kind or TERMINAL, renewed),
            "renewed": renewed, "attempts": attempts}
