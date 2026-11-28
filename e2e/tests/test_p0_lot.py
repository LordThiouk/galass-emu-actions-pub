#!/usr/bin/env python3
"""P0_LOT is separate from P0_DEFAULT (Papa 10:20 — thresholds untouched)."""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import report  # noqa: E402


def test_p0_default_literal_unchanged():
    """Regression lock: the multistop P0 list must stay byte-stable as a set."""
    expected = """api-preflight apk-base-url emulator-boot apk-install
rider-home-loaded rider-destination-selected rider-stop-picker rider-multistop-summary
rider-driver-assigned rider-running-start rider-stop1-active rider-stop1-done
rider-stop2-done rider-destination-arrived rider-cash-methods rider-cash-waiting
rider-final-itinerary rider-rating rider-home-after
driver-offer-with-stops driver-accepted-details driver-pickup-arrived driver-running-stop1
driver-stop1-done driver-stop2-done driver-cash-request driver-cash-confirmed driver-summary
backend-flow no-prod-calls""".split()
    assert report.P0_DEFAULT == expected, set(report.P0_DEFAULT) ^ set(expected)


def test_p0_lot_defined_and_disjoint():
    assert hasattr(report, "P0_LOT"), "P0_LOT missing"
    assert len(report.P0_LOT) >= 10
    overlap = set(report.P0_LOT) & set(report.P0_DEFAULT) - {
        "api-preflight", "apk-base-url", "emulator-boot", "apk-install", "no-prod-calls"
    }
    assert not overlap, f"lot-only points leaked into P0_DEFAULT overlap: {overlap}"
    for p in report.P0_LOT:
        if p.startswith("lot-"):
            assert p not in report.P0_DEFAULT


def test_p0_lot_matches_lot_flow():
    text = open(os.path.join(ROOT, "lot_flow.sh"), encoding="utf-8").read()
    m = re.search(r'P0_POINTS="([^"]+)"', text, re.S)
    assert m, "P0_POINTS missing in lot_flow.sh"
    declared = set(m.group(1).replace("\\\n", " ").split())
    assert set(report.P0_LOT) == declared, set(report.P0_LOT) ^ declared


def test_run_sh_p0_untouched():
    run = open(os.path.join(ROOT, "run.sh"), encoding="utf-8").read()
    m = re.search(r'P0_POINTS="([^"]+)"', run, re.S)
    assert m
    declared = set(m.group(1).replace("\\\n", " ").split())
    assert set(report.P0_DEFAULT) == declared


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:
                failed += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failed else 0)
