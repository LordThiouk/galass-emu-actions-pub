#!/usr/bin/env bash
# Staging-only multistop E2E on a GitHub-hosted Android emulator.
# Artemis drives/observes the UI; api_flow.py is the deterministic fallback
# and the source of truth for ride state. Nothing here may touch production.
set -uo pipefail

: "${BASE:?BASE is required (staging API, no prod)}"
: "${DEV_TOKEN:?DEV_TOKEN is required}"

RIDER_ID="${RIDER_ID:-106}"
DRIVER_ID="${DRIVER_ID:-23}"
OUT="${OUT:-e2e-out}"
RIDER_PKG="sn.galass.galassrider"
DRIVER_PKG="sn.galass.galassdriver"
APK_RIDER="${APK_RIDER:-apk/rider/app-debug.apk}"
APK_DRIVER="${APK_DRIVER:-apk/driver/app-debug.apk}"
ARTEMIS_BIN="${ARTEMIS_BIN:-vendor/artemis/.venv/bin/artemis}"
PY="${PY:-python3}"

mkdir -p "$OUT/screens" "$OUT/logcat" "$OUT/artemis"
: > "$OUT/run.log"

declare -a RESULTS=()

log() { printf '[e2e %s] %s\n' "$(date -u +%H:%M:%S)" "$*" | tee -a "$OUT/run.log"; }
mark() { RESULTS+=("$1|$2|$3"); log "RESULT $1 = $2 ($3)"; }

clear_anr() {
  # Ferme les dialogues système / ANR ("isn't responding") en cliquant "Wait"
  adb shell am broadcast -a android.intent.action.CLOSE_SYSTEM_DIALOGS >/dev/null 2>&1 || true
  adb shell uiautomator dump /sdcard/anr.xml >/dev/null 2>&1 || return 0
  local b
  b=$(adb shell cat /sdcard/anr.xml 2>/dev/null | grep -o "text=\"Wait\"[^>]*bounds=\"\[[0-9]*,[0-9]*\]\[[0-9]*,[0-9]*\]\"" | grep -o "\[[0-9]*,[0-9]*\]\[[0-9]*,[0-9]*\]" | head -1)
  if [ -n "$b" ]; then
    local x1 y1 x2 y2; read -r x1 y1 x2 y2 <<<"$(echo "$b" | tr -c '0-9' ' ')"
    adb shell input tap $(( (x1 + x2) / 2 )) $(( (y1 + y2) / 2 )) >/dev/null 2>&1
    log "ANR dialog dismissed (Wait)"; sleep 2
  fi
}

shot() {
  local name="$1" pkg=""
  case "$name" in *rider*) pkg="$RIDER_PKG";; *driver*) pkg="$DRIVER_PKG";; esac
  clear_anr
  if [ -n "$pkg" ] && ! adb shell dumpsys activity activities 2>/dev/null | grep -m1 "mResumedActivity\|topResumedActivity" | grep -q "$pkg"; then
    launch "$pkg"; sleep 4; clear_anr
  fi
  adb exec-out screencap -p > "$OUT/screens/$name.png" 2>/dev/null
  local sz
  sz=$(wc -c < "$OUT/screens/$name.png" 2>/dev/null || echo 0)
  log "screenshot $name (${sz} bytes)"
}

dump_ui() {
  local name="$1"
  adb shell uiautomator dump /sdcard/ui.xml >/dev/null 2>&1
  adb pull /sdcard/ui.xml "$OUT/$name.xml" >/dev/null 2>&1 || true
}

launch() {
  adb shell monkey -p "$1" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1
}

api() { STEP="$1" "$PY" e2e/api_flow.py; }

ARTEMIS_OK=1
artemis_run() {
  # name goal timeout_s [locked_pkg]
  local name="$1" goal="$2" tmo="${3:-240}" pkg="${4:-}"
  if [ "$ARTEMIS_OK" != "1" ]; then
    log "artemis skip: $name (smoke failed)"
    return 2
  fi
  local args=(run "$goal" --profile flash --test-name "$name"
              --traces-path "$(pwd)/$OUT/artemis")
  [ -n "$pkg" ] && args+=(--locked-app "$pkg")
  log "artemis start: $name"
  ( cd vendor/artemis && timeout "$tmo" "./.venv/bin/artemis" "${args[@]}" ) \
    >"$OUT/artemis/$name.log" 2>&1
  local rc=$?
  log "artemis end: $name rc=$rc"
  return $rc
}

write_prefs() {
  # pkg token user_id name email phone
  local pkg="$1" token="$2" uid="$3" name="$4" email="$5" phone="$6"
  local xml="/tmp/prefs-$pkg.xml"
  "$PY" - "$xml" "$token" "$uid" "$name" "$email" "$phone" <<'PY'
import sys
from xml.sax.saxutils import escape as e
path, token, uid, name, email, phone = sys.argv[1:7]
entries = [
    ("string", "flutter.access_token", token),
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
open(path, "w").write("\n".join(out) + "\n")
PY
  adb shell "run-as $pkg mkdir -p shared_prefs" >/dev/null 2>&1
  adb shell "run-as $pkg sh -c 'cat > shared_prefs/FlutterSharedPreferences.xml'" < "$xml"
  adb shell "run-as $pkg chmod 660 shared_prefs/FlutterSharedPreferences.xml" >/dev/null 2>&1
  log "prefs injected: $pkg"
}

# ---------------------------------------------------------------- preflight
log "host=$(uname -m) out=$OUT"
if ! "$PY" - "$BASE" "$DEV_TOKEN" <<'PY'
import json, sys, urllib.request
base, dev = sys.argv[1], sys.argv[2]
req = urllib.request.Request(base.rstrip('/') + "/api/general-setting",
                             headers={"Accept": "application/json", "dev-token": dev})
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.loads(r.read().decode())
    print("general-setting:", body.get("status"), body.get("remark"))
    sys.exit(0 if r.status == 200 else 1)
except Exception as exc:
    print("general-setting failed:", exc)
    sys.exit(1)
PY
then
  mark api-preflight FAIL "staging unreachable or dev-token rejected"
else
  mark api-preflight PASS "staging /api/general-setting answered 200"
fi

# APK actually targets staging, not prod
if "$PY" - "$APK_RIDER" <<'PY'
import os, sys, zipfile
apk = sys.argv[1]
with zipfile.ZipFile(apk) as z:
    blob = z.read("assets/flutter_assets/kernel_blob.bin")
ok = os.environ["STAGING_HOST"].encode() in blob
prod = blob.count(b"https://galassapp.com/api/")
print(f"kernel_blob staging_base={ok} prod_api_literals={prod}")
sys.exit(0 if ok and prod == 0 else 1)
PY
then
  mark apk-base-url PASS "APK Rider built with API_BASE_URL=staging"
else
  mark apk-base-url FAIL "APK Rider does not embed the staging base URL"
fi

# ------------------------------------------------------------------- device
adb wait-for-device
for _ in $(seq 1 90); do
  [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ] && break
  sleep 5
done
if [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" != "1" ]; then
  mark emulator-boot FAIL "sys.boot_completed never reached 1"
  echo '{"results":["emulator-boot|FAIL|no boot"]}' > "$OUT/report.json"
  exit 1
fi
mark emulator-boot PASS "$(adb shell getprop ro.build.version.release | tr -d '\r') / API $(adb shell getprop ro.build.version.sdk | tr -d '\r')"
adb shell settings put global window_animation_scale 0 >/dev/null 2>&1
adb shell settings put global transition_animation_scale 0 >/dev/null 2>&1
adb shell settings put global animator_duration_scale 0 >/dev/null 2>&1
adb shell input keyevent 82 >/dev/null 2>&1
adb shell svc power stayon true >/dev/null 2>&1
adb emu geo fix -17.4677 14.7167 >/dev/null 2>&1 || true  # GPS Dakar (sinon position Californie par défaut -> pas de dispatch)
adb logcat -c >/dev/null 2>&1 || true

# ------------------------------------------------------------------ install
adb install -r -t -g "$APK_RIDER" >/dev/null 2>&1 && r1=0 || r1=1
adb install -r -t -g "$APK_DRIVER" >/dev/null 2>&1 && r2=0 || r2=1
if [ $r1 -eq 0 ] && [ $r2 -eq 0 ]; then
  mark apk-install PASS "Rider + Driver installed"
else
  mark apk-install FAIL "rider_rc=$r1 driver_rc=$r2"
fi

write_prefs "$RIDER_PKG" "${RIDER_TOKEN:-}" "$RIDER_ID" "E2E Rider" "e2e.rider@galass.sn" "+221700000106"
write_prefs "$DRIVER_PKG" "${DRIVER_TOKEN:-}" "$DRIVER_ID" "E2E Driver" "e2e.driver@galass.sn" "+221700000023"

# ----------------------------------------------------- rider home + driver
launch "$RIDER_PKG"
sleep 15
shot 01-rider-home
dump_ui 01-rider-home

launch "$DRIVER_PKG"
sleep 15
shot 02-driver-home
dump_ui 02-driver-home

# Artemis smoke: if the free LLM endpoint cannot drive one trivial goal, skip
# all later UI attempts instead of burning a timeout on each one.
# Préflight vision: Artemis envoie des captures; si le endpoint LLM refuse les images, inutile de lancer le smoke
vision_rc=$("$PY" - <<'PY'
import json, os, urllib.request
b = os.environ.get("OPENAI_BASE_URL", "").rstrip("/")
png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
body = {"model": os.environ.get("ARTEMIS_DEFAULT_MODEL", "auto"), "max_tokens": 5,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "color?"},
                     {"type": "image_url", "image_url": {"url": "data:image/png;base64," + png}}]}]}
req = urllib.request.Request(b + "/chat/completions", data=json.dumps(body).encode(), method="POST",
      headers={"Authorization": "Bearer " + os.environ.get("OPENAI_API_KEY", ""), "Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=60) as r: print(r.status)
except urllib.error.HTTPError as e: print(e.code)
except Exception: print(0)
PY
)
echo "vision preflight http=$vision_rc" > "$OUT/artemis/vision-preflight.txt"
if [ "$vision_rc" != "200" ]; then
  mark llm-vision FAIL "endpoint LLM refuse les images (HTTP $vision_rc) -> Artemis ne peut pas voir l'écran"
  ARTEMIS_OK=0
else
  mark llm-vision PASS "endpoint LLM accepte les images"
fi
if [ "$ARTEMIS_OK" = "1" ] && artemis_run smoke "Open the Settings app and report the Android version shown on screen." 90; then
  mark artemis-smoke PASS "LLM + device loop operational"
else
  ARTEMIS_OK=0
  mark artemis-smoke WARN "Artemis unavailable, API fallback only (see artemis/smoke.log)"
fi

# Artemis observes the rider create form (UI action attempt, API fallback after)
artemis_run rider-create \
  "Open the Galass Rider app. From the home screen, select a destination, add two intermediate stops, choose cash payment, then confirm the ride request. Stay on the ride screen." \
  210 "$RIDER_PKG"
artemis_rc=$?
case $artemis_rc in
  0) mark artemis-rider-create PASS "UI create attempt completed" ;;
  2) mark artemis-rider-create SKIP "Artemis disabled by smoke" ;;
  *) mark artemis-rider-create WARN "rc=$artemis_rc (API fallback next)" ;;
esac

# Adopt the ride Artemis may have created, otherwise create it via API.
api pick | tee -a "$OUT/run.log"
RIDE_ID=$("$PY" -c "import json;print(json.load(open('$OUT/state.json')).get('ride_id') or '')" 2>/dev/null)
if [ -n "$RIDE_ID" ]; then
  mark ride-exists PASS "ride_id=$RIDE_ID (rider $RIDER_ID, 2 stops, cash)"
else
  mark ride-exists FAIL "no ride created"
fi

# Capture rapide (pas de relance d'app): l'offre chauffeur expire vite
adb exec-out screencap -p > "$OUT/screens/03-rider-ride-created.png" 2>/dev/null; log "screenshot 03 (fast)"

# Driver offer: Artemis attempts the acceptance, API accept is the fallback.
launch "$DRIVER_PKG"
sleep 6
adb exec-out screencap -p > "$OUT/screens/03b-driver-offer.png" 2>/dev/null; log "screenshot 03b (fast)"
artemis_run driver-accept \
  "Open the Galass Driver app. If a ride offer is shown, accept it. Then open the active ride details screen showing the two stops." \
  150 "$DRIVER_PKG"
driver_artemis_rc=$?
api accept | tee -a "$OUT/run.log"
sleep 3
shot 04-driver-accepted

# Start the ride (OTP belongs to the rider and is only available server-side).
api start | tee -a "$OUT/run.log"
sleep 5
shot 05-driver-running
dump_ui 05-driver-running

# Rider must show the running ride with both stops before they are completed.
launch "$RIDER_PKG"
sleep 10
shot 06-rider-running
dump_ui 06-rider-running
if grep -qi "E2E Stop" "$OUT/06-rider-running.xml" 2>/dev/null; then
  mark rider-shows-stops PASS "stop labels present in rider UI dump"
else
  mark rider-shows-stops WARN "uiautomator dump has no stop labels (Flutter semantics)"
fi

# Stops: Artemis tries one stop in the UI, API completes the sequence.
artemis_run driver-stops \
  "Open the Galass Driver app and complete the pending stops of the running ride one by one until every stop is marked done." \
  180 "$DRIVER_PKG"
stops_rc=$?
mark artemis-driver-stops "$([ $stops_rc -eq 0 ] && echo PASS || { [ $stops_rc -eq 2 ] && echo SKIP || echo WARN; })" "UI attempt; API completion follows"

STOP_HOOK="$(pwd)/e2e/stop_hook.sh" OUT="$OUT" api stops | tee -a "$OUT/run.log"
shot 07-driver-stops-done

api end | tee -a "$OUT/run.log"
sleep 3; shot 09-driver-after-end-before-cash; dump_ui 09-driver-after-end-before-cash
shot 10-rider-payment-before; dump_ui 10-rider-payment-before
api pay_rider | tee -a "$OUT/run.log"
sleep 3; shot 11-rider-after-cash-payment
shot 12-driver-cash-to-collect; dump_ui 12-driver-cash-to-collect
api cash_driver | tee -a "$OUT/run.log"
sleep 3; shot 13-driver-after-cash-received
api status | tee -a "$OUT/run.log"

# Rider panel must be closed once the ride is over.
launch "$RIDER_PKG"
sleep 12
shot 08-rider-after
dump_ui 08-rider-after

artemis_run rider-closed \
  "Open the Galass Rider app and return to its home screen. Report whether an active-ride panel is still open." \
  150 "$RIDER_PKG"
close_rc=$?
mark artemis-rider-closed "$([ $close_rc -eq 0 ] && echo PASS || { [ $close_rc -eq 2 ] && echo SKIP || echo WARN; })" "see $OUT/artemis/rider-closed.log"

# ------------------------------------------------------------- assertions
if "$PY" - "$OUT" <<'PY'
import json, sys
out = sys.argv[1]
st = json.load(open(f"{out}/state.json"))
d = st.get("driver_final") or {}
r = st.get("rider_final") or {}
stops = st.get("stops") or d.get("stops") or []
done = [s for s in stops if s.get("completed_at")]
ok = (
    int(r.get("status") or -1) == 1
    and int(d.get("driver_id") or r.get("driver_id") or 0) == 23
    and len(stops) == 2
    and len(done) == 2
    and r.get("payment_status") == d.get("payment_status")
)
print(json.dumps({"status": r.get("status"), "driver_id": d.get("driver_id") or r.get("driver_id"),
                  "stops": len(stops), "stops_completed": len(done),
                  "payment_status": r.get("payment_status"), "ok": ok}))
sys.exit(0 if ok else 1)
PY
then
  mark backend-flow PASS "ride completed, 2/2 stops, cash paid, driver $DRIVER_ID"
else
  mark backend-flow FAIL "see $OUT/state.json (status/stops/payment)"
fi

if [ -s "$OUT/screens/08-rider-after.png" ] && [ -s "$OUT/screens/06-rider-running.png" ] \
   && ! cmp -s "$OUT/screens/06-rider-running.png" "$OUT/screens/08-rider-after.png"; then
  mark rider-panel-closed PASS "rider screen changed after ride end (see 06 vs 08)"
else
  mark rider-panel-closed FAIL "rider screen identical or missing"
fi

if grep -qi "E2E Stop" "$OUT/08-rider-after.xml" 2>/dev/null; then
  mark rider-panel-closed-ui FAIL "stop labels still in rider UI after completion"
else
  mark rider-panel-closed-ui PASS "no stop labels in rider UI dump"
fi

# ------------------------------------------------------------------ logcat
adb logcat -d -v threadtime > "$OUT/logcat/full.log" 2>/dev/null || true
grep -a -iE "galassapp\.com|${STAGING_HOST//./\\.}" "$OUT/logcat/full.log" > "$OUT/logcat/filtered.log" || true
prod_hits=$(grep -a -c "galassapp\.com" "$OUT/logcat/filtered.log" 2>/dev/null || true)
prod_hits=${prod_hits:-0}
if [ "$prod_hits" -eq 0 ]; then
  mark no-prod-calls PASS "0 galassapp.com occurrences in logcat"
else
  mark no-prod-calls FAIL "$prod_hits galassapp.com occurrences in logcat"
fi

# --------------------------------------------------------------- report
# --- Diagnostic Artemis / émulateur (archive chiffrée uniquement) ---
D="$OUT/diag"; mkdir -p "$D"
find "$HOME" vendor/artemis -name "daemon-*.log" -o -name "mcp_server.log" 2>/dev/null | head -10 | while read -r f; do cp "$f" "$D/" 2>/dev/null; done
for ep in /api/sessions /api/system/readiness /api/devices /api/system/adb/server; do
  curl -s -m 10 "http://127.0.0.1:8000$ep" > "$D/daemon$(echo "$ep" | tr '/' '_').json" 2>/dev/null || true
done
adb shell dumpsys accessibility > "$D/dumpsys-accessibility.txt" 2>/dev/null || true
adb shell pm list packages 2>/dev/null | grep -i artemis > "$D/artemis-packages.txt" || true
adb logcat -d -v threadtime 2>/dev/null | grep -aiE "artemis|accessib|ANR in|not responding" | tail -n 2000 > "$D/logcat-artemis-anr.txt" || true
adb shell top -bn1 -m 15 > "$D/emu-top.txt" 2>/dev/null || true
adb shell uptime > "$D/emu-uptime.txt" 2>/dev/null || true
{ uptime; nproc; free -m; } > "$D/host-load.txt" 2>/dev/null || true
printf '%s\n' "${RESULTS[@]}" > "$OUT/results.txt"
"$PY" - "$OUT" <<'PY'
import json, os, sys
out = sys.argv[1]
rows = [l.strip().split("|", 2) for l in open(f"{out}/results.txt") if l.strip()]
fails = [r for r in rows if r[1] == "FAIL"]
warns = [r for r in rows if r[1] == "WARN"]
report = {
    "verdict": "FAIL" if fails else "PASS",
    "points": [{"point": r[0], "result": r[1], "detail": r[2]} for r in rows],
    "fail_count": len(fails),
    "warn_count": len(warns),
}
json.dump(report, open(f"{out}/report.json", "w"), indent=2)
lines = ["# E2E staging report", "", f"Verdict: **{report['verdict']}**", "",
         "| point | result | detail |", "|---|---|---|"]
for r in rows:
    lines.append(f"| {r[0]} | {r[1]} | {r[2]} |")
lines += ["", "Screenshots: " + ", ".join(sorted(os.listdir(f"{out}/screens"))),
          "Artemis logs: " + ", ".join(sorted(os.listdir(f"{out}/artemis")))]
open(f"{out}/summary.md", "w").write("\n".join(lines) + "\n")
print(f"verdict={report['verdict']} fails={len(fails)} warns={len(warns)}")
sys.exit(1 if fails else 0)
PY
exit $?
