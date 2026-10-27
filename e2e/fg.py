#!/usr/bin/env python3
"""Read the REAL foreground package from a `dumpsys` dump on stdin.

Why this exists: the old `fg_pkg` grepped the first `mCurrentFocus` line of
`dumpsys window`. On a multi-display dump (Android 13/14 emulators print the
launcher display first), that line belongs to a SECONDARY display, so a Driver
app in the foreground on `mDisplayId=0` was reported as absent — every wait then
timed out on a lie.

Rules, testable without an emulator:

  * only the DEFAULT display counts: `mDisplayId=0`. A dump that names displays but
    no id 0 has no default display and yields "" (the caller tries the next source);
    a dump with no `Display` header is one anonymous block and stays parsable. A
    launcher on display 1 or 2 can never win;
  * window -> activities -> top is a fallback chain, first non-empty answer wins;
  * no answer is `""` (the caller prints UNKNOWN), never a guess.

Two more questions the harness asks about a foreground package (run 40375590679:
the Driver dashboard calls requestIgnoreBatteryOptimization(), Settings opens its
dialog and steals the foreground for a minute):

  * `--overlay PKG` — is PKG one of the known SYSTEM overlays? Exit 0 when it is
    (com.android.settings, com.android.permissioncontroller,
    com.google.android.permissioncontroller), 1 for anything else — launcher3 is a
    normal app, not a dialog to close. Answers from the argument alone: no stdin.
  * `--component` — the pkg/activity currently focused on the default display, so
    the journal can name WHICH Settings screen stole the foreground.

CLI:
    adb shell dumpsys window displays   | python3 e2e/fg.py --source window
    adb shell dumpsys activity activities | python3 e2e/fg.py --source activities
    adb shell dumpsys activity top      | python3 e2e/fg.py --source top
    adb shell dumpsys window displays   | python3 e2e/fg.py --component
    python3 e2e/fg.py --overlay com.android.settings      # exit 0 / 1
"""
import argparse
import re
import sys

# `Display: mDisplayId=0` (window) and `Display #0 (activities...)` (activities/top).
_DISPLAY = re.compile(r"^\s*Display(?::\s*mDisplayId=(\d+)| #(\d+))")
_COMPONENT = re.compile(r"([A-Za-z0-9_.]+)/([A-Za-z0-9_.$]+)")
_FOCUS = re.compile(r"\b(?:mCurrentFocus|mFocusedApp)\b")
_RESUMED = re.compile(r"\b(?:topResumedActivity|mResumedActivity|ResumedActivity)\b")
_TOP = re.compile(r"^\s*(?:ACTIVITY|TASK)\s+(\S+)")


def display_blocks(text):
    """Split a dump into (display_id, block) pairs, display 0 first when present.

    A dump with no `Display` header is one anonymous block: the caller may still
    find a component in it, and a lie is worse than an empty answer.
    """
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if _DISPLAY.match(line)]
    if not starts:
        return [(None, text)]
    blocks = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        match = _DISPLAY.match(lines[start])
        ident = int(match.group(1) if match.group(1) is not None else match.group(2))
        blocks.append((ident, "\n".join(lines[start:end])))
    blocks.sort(key=lambda pair: 0 if pair[0] == 0 else 1)  # stable: 0, then source order
    return blocks


def _pkg(line):
    match = _COMPONENT.search(line or "")
    return match.group(1) if match else ""


def _default_block(text):
    """Content of the DEFAULT display, or "" when the dump names no display 0.

    A dump with no `Display` header is one anonymous block and stays parsable.
    A dump that names displays 2 and 1 but never 0 has no default display: the
    answer is "", so the caller falls through to the next source. Falling back to
    the first block would hand the win to a secondary-display launcher — the exact
    bug this module exists to kill.
    """
    if not text.strip():
        return ""
    for ident, block in display_blocks(text):
        if ident is None or ident == 0:
            return block
    return ""


def parse_window(text):
    """`mCurrentFocus`/`mFocusedApp` of the default display, either line."""
    for line in _default_block(text).splitlines():
        if _FOCUS.search(line):
            pkg = _pkg(line)
            if pkg:
                return pkg
    return ""


def parse_activities(text):
    """Top resumed activity of the default display."""
    for line in _default_block(text).splitlines():
        if _RESUMED.search(line):
            pkg = _pkg(line)
            if pkg:
                return pkg
    return ""


def parse_top(text):
    """`dumpsys activity top`: the first ACTIVITY/TASK component wins."""
    for line in _default_block(text).splitlines():
        match = _TOP.match(line)
        if match:
            pkg = _pkg(match.group(1))
            if pkg:
                return pkg
    return ""


PARSERS = {"window": parse_window, "activities": parse_activities, "top": parse_top}
ORDER = ("window", "activities", "top")

# Activities that own a SYSTEM dialog (battery-optimisation exemption, runtime
# permission prompt). They are not the app under test: focusing one means an
# overlay stole the foreground, and the harness has to close it.
SYSTEM_OVERLAYS = frozenset((
    "com.android.settings",
    "com.android.permissioncontroller",
    "com.google.android.permissioncontroller",
))


def is_system_overlay(pkg):
    """True only for a known system overlay; launcher3 and apps are normal."""
    return (pkg or "").strip() in SYSTEM_OVERLAYS


def focused_component(text):
    """`pkg/activity` of the focused window on the default display, or "".

    Focus lines win over resumed lines: the dialog that stole the foreground is
    named by `mCurrentFocus`, and a transient resumed activity would name the app
    behind it. Unreadable is "", never a guess.
    """
    block = _default_block(text)
    for pattern in (_FOCUS, _RESUMED):
        for line in block.splitlines():
            if not pattern.search(line):
                continue
            match = _COMPONENT.search(line)
            if match:
                return "%s/%s" % (match.group(1), match.group(2))
    return ""


def foreground(text, source="auto"):
    if source in PARSERS:
        return PARSERS[source](text)
    for name in ORDER:
        pkg = PARSERS[name](text)
        if pkg:
            return pkg
    return ""


def main(argv=None):
    parser = argparse.ArgumentParser(description="Foreground package from a dumpsys dump")
    parser.add_argument("--source", choices=("auto",) + ORDER, default="auto",
                        help="which dump is on stdin (auto probes all three parsers)")
    parser.add_argument("--overlay", metavar="PKG", default=None,
                        help="exit 0 if PKG is a known system overlay, 1 otherwise")
    parser.add_argument("--component", action="store_true",
                        help="print the focused pkg/activity of the default display")
    args = parser.parse_args(argv)
    # `--overlay` answers from its argument: reading stdin would block the caller.
    if args.overlay is not None:
        return 0 if is_system_overlay(args.overlay) else 1
    text = sys.stdin.read()
    if args.component:
        comp = focused_component(text)
        if not comp:
            return 1
        print(comp)
        return 0
    print(foreground(text, args.source))
    return 0


if __name__ == "__main__":
    sys.exit(main())
