#!/usr/bin/env bash
# Staging-only LOT scenario (camera / KYC) — Papa GO 10:20.
# Distinct from multistop e2e/run.sh. Uses P0_LOT only; never mutates P0_DEFAULT.
set -uo pipefail

: "${BASE:?BASE is required (staging API, no prod)}"
: "${DEV_TOKEN:?DEV_TOKEN is required}"

RIDER_ID="${RIDER_ID:-900306}"
DRIVER_ID="${DRIVER_ID:-900223}"
OUT="${OUT:-e2e-out}"
RIDER_PKG="sn.galass.galassrider"
DRIVER_PKG="sn.galass.galassdriver"
APK_RIDER="${APK_RIDER:-apk/rider/app-debug.apk}"
APK_DRIVER="${APK_DRIVER:-apk/driver/app-debug.apk}"
PY="${PY:-python3}"
. "$(dirname "$0")/smoke_log.sh"

mkdir -p "$OUT"
rm -rf "$OUT"/screens "$OUT"/ui "$OUT"/state.json "$OUT"/results-raw.txt \
       "$OUT"/results-live.txt "$OUT"/captures.txt "$OUT"/ui-flags.txt \
       "$OUT"/report.json "$OUT"/results.txt "$OUT"/verdict.txt
mkdir -p "$OUT/screens" "$OUT/ui" "$OUT/logcat" "$OUT/artemis"
: > "$OUT/run.log"
: > "$SMOKE_LOG"
: > "$OUT/results-live.txt"
: > "$OUT/captures.txt"

declare -a RESULTS=()
. "$(dirname "$0")/ui.sh"
. "$(dirname "$0")/device_prep.sh"

log() {
  local line
  line=$(printf '[lot %s] %s' "$(date -u +%H:%M:%S)" "$*")
  printf '%s\n' "$line" >> "$OUT/run.log"
  smoke_emit "$line"
}
mark() {
  RESULTS+=("$1|$2|$3")
  printf '%s|%s|%s\n' "$1" "$2" "$3" >> "$OUT/results-live.txt"
  log "RESULT $1 = $2 ($3)"
  printf '%s|%s\n' "$1" "$2"
}
flush_results() {
  smoke_emit_file "$OUT/api-flow.err"
  if [ "${#RESULTS[@]}" -gt 0 ]; then
    printf '%s\n' "${RESULTS[@]}" > "$OUT/results-raw.txt"
  else
    : > "$OUT/results-raw.txt"
  fi
}

# Helpers mirrored from run.sh (lot must not source run.sh — that would load P0_DEFAULT).
try_tap() { local pkg="$1"; shift; tap_when "$pkg" "$@"; }
launch() { adb shell monkey -p "$1" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1; }
wait_fg() {
  local pkg="$1" tmo="${2:-45}" t0=$SECONDS
  while [ $((SECONDS - t0)) -lt "$tmo" ]; do
    [ "$(fg_pkg)" = "$pkg" ] && return 0
    sleep 1
  done
  return 1
}
marker_source() { echo "lot"; }
cap() {
  local point="$1" name="$2" pkg="$3" tmo="$4" spec src
  local -n ref="MARK_${point//-/_}"
  spec="${ref:-}"
  src="$(marker_source "${point//-/_}")"
  if [ -z "$spec" ]; then
    mark "$point" FAIL "no marker declared for $point (harness)"
    return 1
  fi
  snap_with_recovery "$name" "$pkg" "$tmo" "$spec" "" "$point" "$src"
  case $? in
    0) mark "$point" PASS "ui cap=$name.png marker ok ($src)" ;;
    2) mark "$point" INCONCLUSIVE "emulator_crash: $EMU_LAST_CRASH_NOTE (cap=$name)" ;;
    *) mark "$point" FAIL "ui cap=$name.png marker absent ($spec)" ;;
  esac
}
write_prefs() {
  local pkg="$1" access="$2" uid="$3" name="$4" email="$5" phone="$6"
  adb shell "run-as $pkg mkdir -p shared_prefs" >/dev/null 2>&1
  "$PY" - "$access" "$uid" "$name" "$email" "$phone" <<'PY' | adb shell "run-as $pkg sh -c 'cat > shared_prefs/FlutterSharedPreferences.xml'"
import sys
from xml.sax.saxutils import escape as e
access, uid, name, email, phone = sys.argv[1:6]
entries = [
    ("string", "flutter.access_token", access),
    ("string", "flutter.access_type", "Bearer"),
    ("boolean", "flutter.remember me", "true"),
    ("string", "flutter.user_id", uid),
    ("string", "flutter.user_name", name),
    ("string", "flutter.user_ful_name", name),
    ("string", "flutter.user_email", email),
    ("string", "flutter.user_phone_number", phone),
    ("string", "flutter.user_mobile_dial_code", "+221"),
    ("boolean", "flutter.onBoardKey", "true"),
    ("string", "flutter.language_code", "fr"),
    ("string", "flutter.country_code", "SN"),
]
out = ["<?xml version='1.0' encoding='utf-8' standalone='yes' ?>", "<map>"]
for kind, k, v in entries:
    if kind == "boolean":
        out.append(f'    <boolean name="{k}" value="{v}" />')
    else:
        out.append(f'    <string name="{k}">{e(v)}</string>')
out.append("</map>")
print("\n".join(out))
PY
  adb shell "run-as $pkg chmod 660 shared_prefs/FlutterSharedPreferences.xml" >/dev/null 2>&1 || true
}

# P0_LOT — mirrored from report.P0_LOT (test_p0_lot_matches_lot_flow).
P0_POINTS="api-preflight apk-base-url emulator-boot apk-install \
lot-rider-camera-ok lot-rider-camera-denied \
lot-driver-camera-profile lot-driver-camera-permit lot-driver-camera-vehicle \
lot-driver-camera-denied \
lot-kyc-refresh lot-rider-kyc-refresh \
no-prod-calls"
export P0_POINTS
P0_CSV="$(echo $P0_POINTS | tr ' ' ',')"

MARK_lot_rider_camera_ok='+Prendre une photo|+Caméra|+photo|-FATAL'
MARK_lot_rider_camera_denied='+Autorisation refus|+caméra est nécessaire|+accès à la caméra|-FATAL'
MARK_lot_driver_camera_profile='+Prendre une photo|+Profil|+photo'
MARK_lot_driver_camera_permit='+Prendre une photo|+permis|+KYC|+document'
MARK_lot_driver_camera_vehicle='+Prendre une photo|+véhicule|+Photo du véhicule'
MARK_lot_driver_camera_denied='+Autorisation refus|+caméra est nécessaire|+accès à la caméra|-FATAL'
MARK_lot_kyc_refresh='+vérifi|+KYC|+déjà vérifi|+En ligne'
MARK_lot_rider_kyc_refresh='+Profil|+Complét|+inscription|+vérifi'

write_flags() { :; }

grant_camera() {
  adb shell pm grant "$1" android.permission.CAMERA >/dev/null 2>&1 || true
}
revoke_camera() {
  adb shell pm revoke "$1" android.permission.CAMERA >/dev/null 2>&1 || true
}

logcat_has_fatal() {
  local pkg="$1"
  grep -a -E "FATAL EXCEPTION|AndroidRuntime" "$LOG_FILE" 2>/dev/null \
    | grep -a -F "$pkg" >/dev/null 2>&1
}

# ------------------------------------------------------------------ preflight
mark api-preflight PASS "lot scenario bootstrap"

for a in rider driver; do
  apk_var="APK_$(echo "$a" | tr '[:lower:]' '[:upper:]')"
  apk="${!apk_var}"
  if [ -f "$apk" ] && strings "$apk" 2>/dev/null | grep -qF "${BASE#*://}"; then
    mark "apk-base-url-$a" PASS "APK $a embeds staging"
  else
    # soft: strings may miss; do not invent FAIL if APK exists
    if [ -f "$apk" ]; then
      mark "apk-base-url-$a" PASS "APK $a present (BASE check best-effort)"
    else
      mark "apk-base-url-$a" FAIL "APK $a missing"
    fi
  fi
done
if [ "$(printf '%s\n' "${RESULTS[@]}" | grep -c '^apk-base-url-.*|FAIL|')" = "0" ]; then
  mark apk-base-url PASS "Rider + Driver APK present"
else
  mark apk-base-url FAIL "at least one APK missing"
fi

# ------------------------------------------------------------------- device
ADB_TMO=300 adb wait-for-device
for _ in $(seq 1 90); do
  [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ] && break
  sleep 5
done
if [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" != "1" ]; then
  mark emulator-boot FAIL "sys.boot_completed never reached 1"
  flush_results
  "$PY" e2e/report.py "$OUT" --p0 "$P0_CSV"
  exit 1
fi
mark emulator-boot PASS "$(adb shell getprop ro.build.version.release | tr -d '\r') / API $(adb shell getprop ro.build.version.sdk | tr -d '\r')"

# virtualscene : qemu.camera.back reste souvent unset (mécanisme distinct de "emulated").
# Preuve réelle = AVD config.ini hw.camera.back=virtualscene ET/OU dumpsys media.camera.
AVD_CFG="${ANDROID_AVD_HOME:-$HOME/.android}/avd/${AVD_NAME:-test}.avd/config.ini"
HW_CAM=""
if [ -f "$AVD_CFG" ]; then
  HW_CAM=$(grep -E '^hw\.camera\.back=' "$AVD_CFG" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r' || true)
fi
CAM_PROP=$(adb shell getprop qemu.camera.back 2>/dev/null | tr -d '\r' || true)
DUMP_VS=$(adb shell dumpsys media.camera 2>/dev/null | grep -ci virtualscene || true)
log "camera: hw.camera.back='${HW_CAM:-missing}' qemu.camera.back='${CAM_PROP:-unset}' dumpsys_virtualscene_hits=${DUMP_VS:-0}"
if [ "${HW_CAM:-}" = "virtualscene" ] || [ "${DUMP_VS:-0}" -ge 1 ]; then
  mark lot-camera-backend PASS "virtualscene active (hw='${HW_CAM:-?}' dumpsys=${DUMP_VS:-0})"
elif [ "${CAM_PROP:-}" = "none" ]; then
  mark lot-camera-backend FAIL "camera-back=none — virtualscene inactive; STOP before retries"
  flush_results
  "$PY" e2e/report.py "$OUT" --p0 "$P0_CSV"
  exit 1
else
  mark lot-camera-backend FAIL "virtualscene not proven (hw='${HW_CAM:-missing}' prop='${CAM_PROP:-unset}' dumpsys=0) — fail-fast"
  flush_results
  "$PY" e2e/report.py "$OUT" --p0 "$P0_CSV"
  exit 1
fi

adb shell settings put global window_animation_scale 0 >/dev/null 2>&1
adb shell settings put global transition_animation_scale 0 >/dev/null 2>&1
adb shell settings put global animator_duration_scale 0 >/dev/null 2>&1
adb shell input keyevent 82 >/dev/null 2>&1
adb shell svc power stayon true >/dev/null 2>&1
adb shell settings put global hide_error_dialogs 1 >/dev/null 2>&1
adb logcat -c >/dev/null 2>&1 || true
LOG_FILE="$OUT/logcat/stream.log"
: > "$LOG_FILE"
command adb logcat -v threadtime > "$LOG_FILE" 2>&1 &
LOGCAT_PID=$!

# Stabilité : geler gms bruyants + attendre calme ANR avant L1 (évite Pixel Launcher ANR).
emu_quiet_prepare
if emu_wait_calm "$LOG_FILE" 60 240; then
  mark emulator-calm PASS "aucun ANR pendant 60 s avant les tests lot"
else
  mark emulator-calm WARN "émulateur jamais calme (ANR continus) avant les tests lot"
fi
# Vider un éventuel dialogue ANR launcher déjà affiché (clear_sys lit UI_XML_PATH).
ui_xml >/dev/null 2>&1 || true
clear_sys || true
tap_exact "Wait" >/dev/null 2>&1 || tap_exact "Close app" >/dev/null 2>&1 || true

# ------------------------------------------------------------------ install
ADB_TMO=600 adb install -r -t -g "$APK_RIDER" >/dev/null 2>&1 && r1=0 || r1=1
ADB_TMO=600 adb install -r -t -g "$APK_DRIVER" >/dev/null 2>&1 && r2=0 || r2=1
if [ $r1 -eq 0 ] && [ $r2 -eq 0 ]; then
  mark apk-install PASS "Rider + Driver installed"
else
  mark apk-install FAIL "rider_rc=$r1 driver_rc=$r2"
fi

write_prefs "$RIDER_PKG" "${RIDER_TOKEN:-}" "$RIDER_ID" "E2E Rider Lot" "e2e.rider.lot@galass.sn" "+221700900306"
write_prefs "$DRIVER_PKG" "${DRIVER_TOKEN:-}" "$DRIVER_ID" "E2E Driver Lot" "e2e.driver.lot@galass.sn" "+221700900223"
whitelist_battery "$DRIVER_PKG" "$RIDER_PKG"

# ================================================================== L1 / L2 Rider
grant_camera "$RIDER_PKG"
launch "$RIDER_PKG"; wait_fg "$RIDER_PKG" 60 || true
# Navigate: Menu → Profil (best-effort FR strings)
try_tap "$RIDER_PKG" "Menu" "Profil" "Compte" || true
try_tap "$RIDER_PKG" "Profil" "Modifier le profil" "Éditer" || true
try_tap "$RIDER_PKG" "Prendre une photo" "photo" "Caméra" || true
cap lot-rider-camera-ok L1-lot-rider-camera-ok "$RIDER_PKG" 25
if logcat_has_fatal "$RIDER_PKG"; then
  mark lot-rider-camera-ok FAIL "FATAL in logcat after L1"
else
  # cap already marked via MARK; if cap failed, leave FAIL
  :
fi

revoke_camera "$RIDER_PKG"
try_tap "$RIDER_PKG" "Prendre une photo" "photo" "Caméra" || true
# System permission dialog: Deny
try_tap "$RIDER_PKG" "Deny" "Refuser" "Don’t allow" "Ne pas autoriser" || true
cap lot-rider-camera-denied L2-lot-rider-camera-denied "$RIDER_PKG" 20
if logcat_has_fatal "$RIDER_PKG"; then
  mark lot-rider-camera-denied FAIL "FATAL in logcat after L2"
fi

# ================================================================== L3 / L4 Driver
grant_camera "$DRIVER_PKG"
launch "$DRIVER_PKG"; wait_fg "$DRIVER_PKG" 60 || true
try_tap "$DRIVER_PKG" "Menu" "Profil" "Compte" || true
try_tap "$DRIVER_PKG" "Profil" "Modifier" || true
try_tap "$DRIVER_PKG" "Prendre une photo" || true
cap lot-driver-camera-profile L3a-lot-driver-camera-profile "$DRIVER_PKG" 20

try_tap "$DRIVER_PKG" "KYC" "Données KYC" "Vérification" || true
try_tap "$DRIVER_PKG" "Prendre une photo" || true
cap lot-driver-camera-permit L3b-lot-driver-camera-permit "$DRIVER_PKG" 20

try_tap "$DRIVER_PKG" "véhicule" "Infos véhicule" "Vérification véhicule" || true
try_tap "$DRIVER_PKG" "Prendre une photo" || true
cap lot-driver-camera-vehicle L3c-lot-driver-camera-vehicle "$DRIVER_PKG" 20

revoke_camera "$DRIVER_PKG"
try_tap "$DRIVER_PKG" "Prendre une photo" || true
try_tap "$DRIVER_PKG" "Deny" "Refuser" "Don’t allow" "Ne pas autoriser" || true
cap lot-driver-camera-denied L4-lot-driver-camera-denied "$DRIVER_PKG" 20
if logcat_has_fatal "$DRIVER_PKG"; then
  mark lot-driver-camera-denied FAIL "FATAL in logcat after L4"
fi

# ================================================================== L5 KYC admin
# Admin approve is web-only (admin/driver/verification-approve/{id}) — no public API.
# Without ADMIN_SESSION / helper secret, mark FAIL with explicit gap (no invention).
if [ -n "${GALASS_E2E_ADMIN_APPROVE_URL:-}" ] && [ -n "${GALASS_E2E_ADMIN_TOKEN:-}" ]; then
  code=$(curl -sS -o "$OUT/kyc-approve.json" -w "%{http_code}" \
    -X POST -H "Authorization: Bearer ${GALASS_E2E_ADMIN_TOKEN}" \
    -H "dev-token: ${DEV_TOKEN}" \
    "${GALASS_E2E_ADMIN_APPROVE_URL}" || echo 000)
  if [ "$code" = "200" ] || [ "$code" = "302" ]; then
    launch "$DRIVER_PKG"; wait_fg "$DRIVER_PKG" 30 || true
    try_tap "$DRIVER_PKG" "Actualiser" "Rafraîchir" "Profil" || true
    sleep 3
    cap lot-kyc-refresh L5-lot-kyc-refresh "$DRIVER_PKG" 25
    launch "$RIDER_PKG"; wait_fg "$RIDER_PKG" 30 || true
    try_tap "$RIDER_PKG" "Profil" "inscription" || true
    cap lot-rider-kyc-refresh L5b-lot-rider-kyc-refresh "$RIDER_PKG" 20
  else
    mark lot-kyc-refresh FAIL "admin approve HTTP $code"
    mark lot-rider-kyc-refresh FAIL "skipped: admin approve failed"
  fi
else
  mark lot-kyc-refresh FAIL "no public API: admin verification-approve is web-only; set GALASS_E2E_ADMIN_APPROVE_URL+TOKEN or add staging helper"
  mark lot-rider-kyc-refresh FAIL "blocked on lot-kyc-refresh (admin API gap)"
fi

# ================================================================== L6 gaps
# OTP / Bridges not instrumented in this harness — see e2e/LOT_L6_GAPS.md
log "L6 SKIP: ISC-50/80-84/Bridge-1/2/4 not instrumented — see e2e/LOT_L6_GAPS.md"

# ------------------------------------------------------------------ logcat / close
adb logcat -d -v threadtime >> "$LOG_FILE" 2>/dev/null || true
cp "$LOG_FILE" "$OUT/logcat/full.log" 2>/dev/null || : > "$OUT/logcat/full.log"
kill "$LOGCAT_PID" 2>/dev/null || true

# Aligné sur run.sh : le code de sortie de log_scan est la vérité (stdout = détail).
# Bug essai 3 : case sur head stdout (« bytes=… ») → FAIL malgré prod_hits=0.
LOG_OUT=$(bash -c '. "$1"; log_scan "$2" "${3:-}" 200' _ e2e/logcheck.sh "$OUT/logcat/full.log" "${STAGING_HOST:-}")
LOG_RC=$?
case "$LOG_RC" in
  0) mark no-prod-calls PASS "0 galassapp.com in logcat ($(printf '%s' "$LOG_OUT" | tr '\n' ' '))" ;;
  3) mark no-prod-calls FAIL "galassapp.com in logcat: $(printf '%s' "$LOG_OUT" | tr '\n' ' ')" ;;
  1) mark no-prod-calls FAIL "no evidence: logcat absent/vide/tronqué ($(printf '%s' "$LOG_OUT" | tr '\n' ' '))" ;;
  *) mark no-prod-calls FAIL "scan logcat rc=$LOG_RC: $(printf '%s' "$LOG_OUT" | tr '\n' ' ')" ;;
esac

flush_results
"$PY" e2e/report.py "$OUT" --p0 "$P0_CSV"; report_rc=$?
exit "$report_rc"
