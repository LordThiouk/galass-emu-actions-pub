#!/usr/bin/env python3
"""GPS geofence for the driver E2E stops.

`complete-stop` used to run against whatever fix the emulator happened to hold,
so a stop could be completed from the pickup point and the backend's
`stop_too_far` guard became the only thing standing between the harness and a
false PASS. This module checks the emulator's OWN report (`adb shell dumpsys
location`): the fix for ONE provider (gps by default) must be within `--limit`
of the stop we are about to complete.

Provider scoping is the point: a stale `network` fix from the previous point used
to be indistinguishable from an applied GPS fix, so a stop could be "verified"
from several hundred metres away.

CLI:
  check --fix F --stop-lat A --stop-lng B [--limit 500] [--provider gps]
        -> "provider=gps fixes=.. stop_distance_m=..", rc=0 PASS / rc=3 geofence
"""
import argparse
import math
import re
import sys

EARTH_RADIUS_M = 6_371_000.0
STOP_LIMIT_M = 500.0      # backend: stop_too_far
DEFAULT_PROVIDER = "gps"

# `dumpsys location` prints one `last location=Location[<provider> lat,lng ...]`
# per provider (gps, network, fused). The provider travels WITH the numbers: a
# coarse network fix is not an applied GPS fix.
_LOCATION_RE = re.compile(
    r"Location\[\s*([A-Za-z0-9_.-]+)\s+(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)")


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres."""
    phi1, phi2 = math.radians(float(lat1)), math.radians(float(lat2))
    dphi = phi2 - phi1
    dlambda = math.radians(float(lon2) - float(lon1))
    a = (math.sin(dphi / 2.0) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2)
    return 2.0 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def parse_locations(text):
    """Every `(provider, lat, lng)` triple in a `dumpsys location` dump, in file order."""
    return [(provider, float(lat), float(lng))
            for provider, lat, lng in _LOCATION_RE.findall(text or "")]


def nearest_fix_distance(text, lat, lng, provider=DEFAULT_PROVIDER):
    """Distance to the closest fix of `provider`, or None when it reported none."""
    fixes = [(flat, flng) for prov, flat, flng in parse_locations(text)
             if prov == provider]
    if not fixes:
        return None
    return min(haversine_m(lat, lng, flat, flng) for flat, flng in fixes)


def count_fixes(text, provider=DEFAULT_PROVIDER):
    """How many fixes of `provider` the dump carries (0 = nothing to trust)."""
    return sum(1 for prov, _, _ in parse_locations(text) if prov == provider)


def geofence_ok(distance_m, limit=STOP_LIMIT_M):
    """A missing fix is never inside the fence."""
    if distance_m is None:
        return False
    return float(distance_m) <= float(limit)


def _fmt(value):
    return "none" if value is None else f"{value:.1f}"


def _cmd_check(args):
    try:
        with open(args.fix, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        sys.stderr.write(f"geofence: fix illisible ({exc})\n")
        return 3

    stop = nearest_fix_distance(text, args.stop_lat, args.stop_lng,
                               provider=args.provider)
    sys.stdout.write(f"provider={args.provider} "
                     f"fixes={count_fixes(text, args.provider)} "
                     f"stop_distance_m={_fmt(stop)}\n")
    if not geofence_ok(stop, args.limit):
        sys.stderr.write(
            f"geofence: chauffeur hors du périmètre de l'arrêt "
            f"(provider={args.provider} stop_distance_m={_fmt(stop)} "
            f"> {float(args.limit):.0f} m)\n")
        return 3
    return 0


def _parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="verify the applied fix against a stop")
    check.add_argument("--fix", required=True)
    check.add_argument("--stop-lat", type=float, required=True)
    check.add_argument("--stop-lng", type=float, required=True)
    check.add_argument("--limit", type=float, default=STOP_LIMIT_M)
    check.add_argument("--provider", default=DEFAULT_PROVIDER)
    check.set_defaults(func=_cmd_check)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
