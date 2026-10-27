#!/usr/bin/env python3
"""Cash-flow ordering for the driver E2E (run 40205373538).

The harness called `driver/rides/received-cash-payment` before the rider had
asked for cash, the backend answered `not_found`, and the verdict blamed the
harness for a call it should never have made. The rider's request is a state the
backend owns: `payment_status == 3` (REQUESTED). This module polls that state
with a bounded budget and reports what it saw; it never writes anything and
never reads the environment at import time.

Honesty rule: an UNREADABLE status is not "the rider did not ask". `None` means
`cash_status_unavailable`; `cash_not_requested` is reserved for a status we
actually read and that is not REQUESTED.
"""
import time

# Backend enum: 0 none, 1 paid, 3 = the rider asked for cash (REQUESTED).
REQUESTED_STATUS = 3
PAID_STATUS = 1

DEFAULT_TIMEOUT_SEC = 30
DEFAULT_INTERVAL_SEC = 2
TIMEOUT_RANGE_SEC = (1, 600)
INTERVAL_RANGE_SEC = (1, 30)


def _as_status(value):
    """Only an exact integer status is a status: "3.5", "", None are not."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            return int(text)
    return None


def _bounded(raw, default, bounds, name):
    """Validated integer from `raw`, or (None, error) — never a silent clamp."""
    if raw is None or str(raw).strip() == "":
        return float(default), None
    text = str(raw).strip()
    try:
        value = int(text)
    except ValueError:
        return None, f"{name}={text!r} n'est pas un entier"
    low, high = bounds
    if value < low or value > high:
        return None, f"{name}={value} hors bornes [{low}..{high}]"
    return float(value), None


def poll_settings(sec_raw=None, interval_raw=None):
    """Validated poll budget: {ok, timeout, interval, error}.

    A value outside its range is a CONFIG ERROR, not something to clamp: the
    caller must say `config_error` rather than silently polling a different
    budget than the one it was asked for.
    """
    timeout, err = _bounded(sec_raw, DEFAULT_TIMEOUT_SEC, TIMEOUT_RANGE_SEC,
                            "CASH_POLL_SEC")
    if err:
        return {"ok": False, "timeout": None, "interval": None, "error": err}
    interval, err = _bounded(interval_raw, DEFAULT_INTERVAL_SEC, INTERVAL_RANGE_SEC,
                             "CASH_POLL_INTERVAL")
    if err:
        return {"ok": False, "timeout": None, "interval": None, "error": err}
    return {"ok": True, "timeout": timeout, "interval": interval, "error": None}


def poll_payment_status(fetch, sleep, timeout=DEFAULT_TIMEOUT_SEC,
                        interval=DEFAULT_INTERVAL_SEC, clock=time.monotonic):
    """Poll `fetch()` until the rider asks for cash or `timeout` seconds pass.

    Always polls at least once (timeout 0 = a single check). Sleeps
    `min(interval, remaining)` so the wait never overshoots the budget by a
    whole interval. Returns the observed state, never a guess:

      {"requested": bool, "payment_status": int|None, "cause": str,
       "waited": float, "polls": int}

    `cause` is `cash_requested`, `cash_not_requested` (a real status was read and
    it was not 3) or `cash_status_unavailable` (no readable status at all).
    """
    timeout = max(0.0, float(timeout))
    interval = max(0.0, float(interval))
    start = clock()
    polls = 0
    status = None
    while True:
        polls += 1
        status = _as_status(fetch())
        if status == REQUESTED_STATUS:
            return {"requested": True, "payment_status": status,
                    "cause": "cash_requested", "waited": round(clock() - start, 3),
                    "polls": polls}
        elapsed = clock() - start
        if elapsed >= timeout:
            break
        if interval <= 0:
            break
        sleep(min(interval, timeout - elapsed))
    return {"requested": False, "payment_status": status,
            "cause": "cash_not_requested" if status is not None
                     else "cash_status_unavailable",
            "waited": round(clock() - start, 3), "polls": polls}


def driver_label(status):
    """Label for the DRIVER side of the cash step, from the observed status.

    cash_requested           status 3: the rider asked, the driver may confirm
    cash_already_paid        status 1: already settled, nothing to confirm
    cash_status_unavailable  no readable status: we could not see the state
    cash_not_requested       a real status, and it is not a request
    """
    value = _as_status(status)
    if value == REQUESTED_STATUS:
        return "cash_requested"
    if value is None:
        return "cash_status_unavailable"
    if value == PAID_STATUS:
        return "cash_already_paid"
    return "cash_not_requested"
