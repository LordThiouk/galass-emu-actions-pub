#!/usr/bin/env bash
# Appelé par api_flow.py avant chaque complete-stop: capture l'arrêt côté chauffeur puis rider.
set -uo pipefail
i="${1:-0}"; n=$((i + 1))
OUT="${OUT:-e2e-out}"
. "$(dirname "$0")/ui.sh"
mkdir -p "$OUT/logcat"
snap "stop-$n-driver" "$DRIVER_PKG" "Arr[eê]t $n|E2E Stop $n" 25 || true
snap "stop-$n-rider" "$RIDER_PKG" "Arr[eê]t $n|E2E Stop $n" 25 || true
adb logcat -d -v threadtime 2>/dev/null | grep -a -iE "galassapp\.com|${STAGING_HOST//./\\.}" \
  | tail -50 > "$OUT/logcat/stop-$n.log" || true
