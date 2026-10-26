#!/usr/bin/env python3
"""Mask secrets before an E2E log line reaches the public smoke log.

The raw run.log stays inside $OUT and ships only inside the encrypted evidence
archive. Anything copied to stderr or e2e/smoke.log passes through sanitize()
first: bearer/basic credentials, tokens, passwords, keys and phone numbers are
masked, everything else is preserved so the log stays useful.

CLI:
    printf '%s\\n' "$line" | python3 e2e/sanitize.py [--append FILE] [--stderr]
"""
import os
import re
import sys

REDACTED = "[REDACTED]"

_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/\-]{6,}=*")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")
# Any `*_token`, `*_secret`, `*_key`, password or authorization field — fcm_token,
# device_token, remember_token, x-api-key, client_secret, … included.
_KV = re.compile(
    r"""(?ix)
    (?<![\w-])
    (["']?
      (?:[a-z0-9]+[_-])*
      (?:token|password|passwd|pwd|secret|key|authorization|otp)
    ["']?\s*[:=]\s*)
    (["']?)
    ([^\s"',;}\[\]]+)
    """
)
# S21: the ride uid must never reach the public log — as a key/value pair
# (`uid=…`, `"uid": "…"`) AND in prose ("portant l'uid 8f3c…", "uid 123").
# The lookbehind keeps counters like `records_uid=7` readable.
_UID_KV = re.compile(
    r"""(?ix)
    (?<![\w-])
    (["']?(?:ride[_-]?)?uid["']?\s*(?:[=:]\s*|\s+))
    (["']?)
    ([A-Za-z0-9_-]{3,})
    """
)
_KEY_NAME = re.compile(r"""["']?([A-Za-z0-9_-]+)["']?\s*[:=]\s*$""")
_PHONE_KV = re.compile(
    r"""(?ix)
    (?<![\w-])
    (["']?(?:phone(?:_number)?|user_phone_number|tel|mobile)["']?\s*[:=]\s*)
    (["']?)
    (\+?\d[\d\s.\-]{4,18}\d)
    """
)
_E164 = re.compile(r"(?<![\w+])\+[1-9]\d{6,14}(?!\w)")
_PHONE_SPACED = re.compile(r"(?<![\w+])\+\d{1,3}(?:[ .-]\d{1,4}){2,5}(?!\d)")
_SN_LOCAL = re.compile(r"(?<!\w)(?:221)?7[0678]\d{7}(?!\w)")


def _mask_phone_text(raw):
    digits = [i for i, ch in enumerate(raw) if ch.isdigit()]
    keep = set(digits[-3:])
    if raw.lstrip().startswith("+") and len(digits) > 6:
        keep.update(digits[:3])  # country code stays readable
    return "".join(ch if (not ch.isdigit() or i in keep) else "*"
                   for i, ch in enumerate(raw))


def _mask_phone(match):
    return _mask_phone_text(match.group(0))


def _is_counter(key_field, value):
    """`records_token=0` / `records_token=12` are counters, not credentials."""
    m = _KEY_NAME.search(key_field)
    return bool(m) and m.group(1).lower().endswith("token") and value.isdigit()


def sanitize(text):
    """Return `text` with ride ids, credentials and phone numbers masked."""
    out = mask_path(text)  # same route table as the api_flow `path` field
    out = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", out)
    out = _JWT.sub(REDACTED, out)
    out = _KV.sub(
        lambda m: m.group(0) if _is_counter(m.group(1), m.group(3))
        else f"{m.group(1)}{m.group(2)}{REDACTED}", out)
    out = _UID_KV.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)
    out = _PHONE_KV.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{_mask_phone_text(m.group(3))}", out)
    out = _PHONE_SPACED.sub(_mask_phone, out)
    out = _E164.sub(_mask_phone, out)
    out = _SN_LOCAL.sub(_mask_phone, out)
    return out


# ---------------------------------------------------------------- API records
# Only these top-level fields and response fields survive into the public smoke log.
# Everything else is masked BY DEFAULT, so a new backend field can never leak.
API_TOP_KEYS = frozenset({"t", "m", "path", "code"})
API_RESP_KEYS = frozenset({
    "status", "remark", "message", "total", "count", "online", "amount",
    "payment_status", "driver_id", "ride_id", "service_id", "payment_type",
    "stops_count", "completed", "distance", "duration",
})
API_CONTAINERS = frozenset({"resp", "data", "ride", "rides", "stops"})
# Substrings that always mean "secret", even when the value is numeric (otp=4821).
_SECRET_KEY_HINTS = (
    "otp", "token", "secret", "uid", "phone", "fcm", "password", "authorization", "key",
)
# Numeric ids in routes: `/api/ride/details/123` -> `/api/ride/details/[REDACTED]`.
_PATH_ID = re.compile(
    r"""(?x)
    (?P<head>
      (?:/api/)?
      (?:
        ride/(?:details|cancel|payment)
        |driver/rides/(?:details|start|end|complete-stop|received-cash-payment)
      )/
    )
    \d+
    (?=[/?#]|$)
    """
)


def mask_path(path):
    """Mask numeric ride/driver ids embedded in an API route."""
    if not isinstance(path, str):
        return path
    return _PATH_ID.sub(lambda m: m.group("head") + REDACTED, path)


def _is_secret_key(key):
    low = str(key).lower()
    return any(hint in low for hint in _SECRET_KEY_HINTS)


def _redact_any(value):
    if isinstance(value, dict):
        return _redact_dict(value)
    if isinstance(value, list):
        return [_redact_any(item) for item in value]
    return REDACTED


def _redact_keep(value):
    if isinstance(value, (dict, list)):
        return _redact_any(value)
    return value


def _redact_dict(record):
    out = {}
    for key, value in record.items():
        if _is_secret_key(key):
            out[key] = REDACTED
        elif key == "path":
            out[key] = mask_path(value)
        elif key in API_TOP_KEYS or key in API_RESP_KEYS or key in API_CONTAINERS:
            out[key] = _redact_keep(value)
        else:
            out[key] = REDACTED
    return out


def redact_record(record):
    """Return the publishable copy of an api_flow record (raw stays in api.jsonl)."""
    if not isinstance(record, dict):
        return REDACTED
    return _redact_dict(record)


def default_smoke_log():
    return os.environ.get("SMOKE_LOG") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "smoke.log")


def append_line(line, path=None):
    path = path or default_smoke_log()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line.rstrip("\n") + "\n")


def smoke_emit(text, path=None):
    """Append the sanitized line to the smoke log and echo it to stderr."""
    clean = sanitize(str(text).rstrip("\n"))
    append_line(clean, path)
    print(clean, file=sys.stderr, flush=True)
    return clean


def main(argv):
    append = None
    to_stderr = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--append":
            i += 1
            if i >= len(argv):
                sys.exit(f"usage: {argv[0]} [--append FILE] [--stderr]")
            append = argv[i]
        elif arg == "--stderr":
            to_stderr = True
        else:
            sys.exit(f"usage: {argv[0]} [--append FILE] [--stderr]")
        i += 1
    to_stdout = not (append or to_stderr)
    for line in sys.stdin:
        clean = sanitize(line.rstrip("\n"))
        if append:
            append_line(clean, append)
        if to_stderr:
            print(clean, file=sys.stderr, flush=True)
        if to_stdout:
            print(clean, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
