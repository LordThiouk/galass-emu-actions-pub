#!/usr/bin/env bash
# Appelé par api_flow.py avant chaque complete-stop (STEP=stops / stop_one, STOP_HOOK).
# Signature snap alignée sur ui.sh : name pkg timeout "spec". Inactif tant que run.sh
# n'exporte pas STOP_HOOK (run.sh pilote désormais les arrêts un par un), mais valide.
set -uo pipefail
i="${1:-0}"; n=$((i + 1))
OUT="${OUT:-e2e-out}"
. "$(dirname "$0")/ui.sh"
mkdir -p "$OUT/logcat"

if [ "$i" -eq 0 ]; then
  driver_spec="+Arrêt 2|+Terminer la course|-Accepter\\s+la\\s+course"
  rider_spec="+Arrêt 2"
else
  driver_spec="+Terminer la course|+Destination|-Accepter\\s+la\\s+course"
  rider_spec="+Destination"
fi

snap "stop-$n-driver" "$DRIVER_PKG" 25 "$driver_spec" || true
snap "stop-$n-rider" "$RIDER_PKG" 25 "$rider_spec" || true
adb logcat -d -v threadtime 2>/dev/null | grep -a -iE "galassapp\.com|${STAGING_HOST:-__none__}" \
  | tail -50 > "$OUT/logcat/stop-$n.log" || true
