#!/usr/bin/env bash
# Called by api_flow.py before each complete-stop: freeze the driver screen.
set -uo pipefail
i="${1:-0}"
OUT="${OUT:-e2e-out}"
mkdir -p "$OUT/screens" "$OUT/logcat"
name="stop-$((i + 1))-before"
adb exec-out screencap -p > "$OUT/screens/$name.png" 2>/dev/null || true
adb shell uiautomator dump /sdcard/ui-stop.xml >/dev/null 2>&1 || true
adb pull /sdcard/ui-stop.xml "$OUT/$name.xml" >/dev/null 2>&1 || true
adb logcat -d -v threadtime 2>/dev/null | grep -a -iE "galassapp\.com|${STAGING_HOST//./\\.}" \
  | tail -50 > "$OUT/logcat/stop-$((i + 1)).log" || true
