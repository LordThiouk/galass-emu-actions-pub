#!/usr/bin/env python3
"""Staging-only guard for BASE.

https:// is required, except plain http:// to the exact staging origin
192.162.69.113:8080 (no TLS on staging). galassapp.com (prod) is always refused.
CLI: base_guard.py <BASE>  -> exit 0 if allowed, 1 otherwise (value never echoed).
"""
import sys
from urllib.parse import urlsplit

STAGING_HTTP_NETLOC = "192.162.69.113:8080"


def check_base(base):
    """Return None if BASE is allowed, else a reason string (without the value)."""
    if not base or any(c.isspace() for c in base) or "\\" in base:
        return "empty or malformed BASE"
    if "galassapp.com" in base.lower():
        return "BASE must not be prod (galassapp.com)"
    try:
        u = urlsplit(base)
        port = u.port
    except ValueError:
        return "unparseable BASE"
    if u.query or u.fragment or "@" in u.netloc or not u.hostname:
        return "BASE must be a plain origin (no userinfo/query/fragment)"
    if u.scheme == "https":
        return None
    if u.scheme == "http":
        if u.netloc == STAGING_HTTP_NETLOC and port == 8080:
            return None
        return "http:// is only allowed for the staging origin"
    return "BASE must be https:// (or http:// staging origin)"


if __name__ == "__main__":
    reason = check_base(sys.argv[1] if len(sys.argv) > 1 else "")
    if reason:
        print(f"BASE refused: {reason}")
        sys.exit(1)
    print("BASE ok (staging)")
