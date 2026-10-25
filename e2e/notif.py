#!/usr/bin/env python3
"""Attribute notification evidence to the ride under test (bug B7).

usage: adb shell dumpsys notification --noredact | python3 e2e/notif.py <pkg> <uid>

Splits the dump into NotificationRecord blocks and prints
    {"records_total": N, "records_pkg": N, "records_token": N, "token_valid": bool}
The raw dump is never written anywhere and never echoed: only counts leave this
process, so notification payloads (which can carry tokens) cannot leak into the
public artifact.

Attribution is strict on purpose. A ride id is a number ("12") that also appears
in "12 min away"; matching it by substring turns any notification into proof.
The ride uid must be a real identifier (>= 8 chars, not numeric-only) and must
appear on a word boundary, never inside another token.
"""
import json
import re
import sys

RECORD_RE = re.compile(r"^\s*NotificationRecord[({ ]", re.M)
UID_RE = re.compile(r"^[A-Za-z0-9_-]{8,}$")


def split_records(dump):
    starts = [m.start() for m in RECORD_RE.finditer(dump)]
    if not starts:
        return []
    starts.append(len(dump))
    return [dump[a:b] for a, b in zip(starts, starts[1:])]


def valid_uid(uid):
    uid = (uid or "").strip()
    if not UID_RE.match(uid):
        return False
    return not uid.isdigit()


def uid_in(record, uid):
    """Word-boundary match: 8f3c1d9a4b2e never matches x8f3c1d9a4b2e."""
    return re.search(r"(?<![A-Za-z0-9_-])" + re.escape(uid) + r"(?![A-Za-z0-9_-])",
                     record) is not None


def probe(dump, pkg, uid=""):
    records = split_records(dump)
    uid = (uid or "").strip()
    ok = valid_uid(uid)
    return {
        "records_total": len(records),
        "records_pkg": sum(1 for r in records if pkg and pkg in r),
        "records_token": sum(1 for r in records if ok and uid_in(r, uid)),
        "token_valid": ok,
    }


def main(argv):
    if len(argv) < 3 or len(argv) > 4:
        sys.stderr.write(f"usage: {argv[0]} <pkg> <uid>\n")
        return 2
    pkg, uid = argv[1], argv[2]
    dump = sys.stdin.read()
    print(json.dumps(probe(dump, pkg, uid)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
