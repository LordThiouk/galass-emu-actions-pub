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

`accept_label` is the ONE place that decides `accept_via` (api | ui | unknown)
from the proofs the harness actually holds.
"""
import argparse
import sys

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


def _truth(value):
    """Accept a bool or a zero-arg callable, so callers never re-query by accident."""
    return bool(value() if callable(value) else value)


def accepted_via(code, kind, assigned=False, tap_proven=False):
    """How the acceptance was obtained, or None.

    "api"     a 2xx answer classified ACCEPTED — precedence, it is the strongest
              proof and a concurrent tap cannot undo it;
    "ui"      the driver is assigned AND the harness proved its own tap (Driver in
              the foreground before and after) — credit belongs to the UI;
    "unknown" assigned without that proof: the ride is ours, but claiming the tap
              would be a guess (a concurrent accept, or a background tap);
    None      not assigned, or an accepted shape on a non-2xx code.

    `assigned`/`tap_proven` are bools or zero-arg callables.
    """
    try:
        status = int(code)
    except (TypeError, ValueError):
        status = 0
    if 200 <= status < 300 and kind == ACCEPTED:
        return "api"
    if _truth(assigned):
        return "ui" if _truth(tap_proven) else "unknown"
    return None


def failure_cause(kind, renewed):
    """Explicit cause handed to `$OUT/accept.json` and the journal ('' if accepted)."""
    if kind == ACCEPTED:
        return ""
    if kind == EXPIRED:
        return EXPIRED
    return kind if kind in KINDS else TERMINAL


def accept_label(ui_tap_proven, ui_screen_proven, api_ok, api_2xx,
                 ui_tap_failed=False, assigned=False):
    """The ONE unambiguous accept label, from the proofs actually held.

    Run 40205373538 published `accept_via` from three different places and they
    disagreed. Precedence is fixed here, once:

      api      an OK answer on a 2xx code is the strongest proof, whatever the UI
               did — the cause says whether the UI tap left no proof
               (`ui_tap_unproven_then_api`: screen not proven, no HTTP status was
               ever observed) or the API simply answered (`api_2xx`);
      ui       the harness proved its own tap AND the next screen appeared;
      unknown  the ride is ours but the tap is unproven
               (`assigned_without_tap_proof`) — `assigned` is the backend's own
               `accept.json .ok`, so a 4xx that assigned nothing cannot borrow
               that cause; anything else is `not_assigned`. Never a success claim.
    """
    if api_ok and api_2xx:
        return {"via": "api",
                "cause": "ui_tap_unproven_then_api" if ui_tap_failed else "api_2xx"}
    if ui_tap_proven and ui_screen_proven:
        return {"via": "ui", "cause": None}
    return {"via": "unknown",
            "cause": "assigned_without_tap_proof" if assigned else "not_assigned"}


def run_accept(respond, assigned, renew, sleep, retries=20, delay=3, renewed=False,
               tap_proven=False):
    """Drive offer acceptance against injected collaborators.

    respond()     -> (http_code, payload)  one accept attempt
    assigned()    -> bool                  already assigned to this driver?
    renew()       -> bool                  cancel the expired ride, create a fresh one
    sleep(s)                               injected (no-op in tests)
    tap_proven                             bool/callable: did the harness prove ITS tap?

    Returns {"ok", "via", "cause", "renewed", "attempts"} where `via` is "api"
    (2xx accepted answer), "ui" (assigned + tap proven) or "unknown" (assigned
    without proof). `unknown` is a SUCCESS — the ride is ours — but it returns
    immediately with an explicit cause: renewing or retrying a ride we already
    hold would be the 24-FAIL cascade again. A non-2xx ACCEPTED shape is a
    failure, never a silent `ok: True, via: None`.

    `renew` is called at most once per ride generation, and an expired offer is
    never retried on the same ride.
    """
    attempts = 0
    renewed = bool(renewed)
    last_kind = None
    last_code = 0
    while attempts < max(0, int(retries)):
        attempts += 1
        code, payload = respond()
        kind = classify(code, payload)
        last_kind = kind
        last_code = code
        via = accepted_via(code, kind, assigned, tap_proven)
        if via == "api":
            return {"ok": True, "via": "api", "cause": None, "code": code,
                    "renewed": renewed, "attempts": attempts}
        if via in ("ui", "unknown"):
            # Assigned already: the offer is ours. Credit the UI only when the tap
            # is provable, otherwise say `unknown` and stop touching the ride.
            cause = f"respond_{code}_assigned" if via == "unknown" else None
            return {"ok": True, "via": via, "cause": cause, "code": code,
                    "renewed": renewed, "attempts": attempts}
        action = decide(kind, renewed)
        if action == "accept":
            # ACCEPTED shape on a non-2xx code, not assigned: do not claim success.
            return {"ok": False, "via": None, "cause": f"respond_{code}",
                    "code": code, "renewed": renewed, "attempts": attempts}
        if action == "renew":
            if renew():
                renewed = True
                continue
            return {"ok": False, "via": None, "cause": EXPIRED, "code": code,
                    "renewed": renewed, "attempts": attempts}
        if action == "fail":
            return {"ok": False, "via": None,
                    "cause": failure_cause(kind, renewed), "code": code,
                    "renewed": renewed, "attempts": attempts}
        sleep(delay)
    return {"ok": False, "via": None,
            "cause": failure_cause(last_kind or TERMINAL, renewed),
            "code": last_code, "renewed": renewed, "attempts": attempts}


# --------------------------------------------------------------------- CLI
def _cmd_label(args):
    out = accept_label(args.ui_tap_proven == "1", args.ui_screen_proven == "1",
                       args.api_ok == "1", args.api_2xx == "1",
                       ui_tap_failed=args.ui_tap_failed == "1",
                       assigned=args.assigned == "1")
    sys.stdout.write(f"via={out['via']} cause={out['cause'] or 'none'}\n")
    return 0


def _parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    label = sub.add_parser("label", help="single accept_via label from real proofs")
    for name in ("--ui-tap-proven", "--ui-screen-proven", "--api-ok", "--api-2xx"):
        label.add_argument(name, default="0")
    label.add_argument("--ui-tap-failed", default="0")
    label.add_argument("--assigned", default="0")
    label.set_defaults(func=_cmd_label)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
