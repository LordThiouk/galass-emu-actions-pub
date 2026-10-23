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
. "$(dirname "$0")/ui.sh"
: > "$OUT/captures.txt"

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
snap 01-rider-home "$RIDER_PKG" 'content-desc="[^"]+"|text="[^"]+"' 60 || true
snap 02-driver-home "$DRIVER_PKG" 'En ligne' 60 || true

# Préflight vision: le modèle demandé d'abord, puis les modèles vision gratuits listés par freellmapi.
"$PY" - "$OUT/artemis" <<'PY' > "$OUT/artemis/vision-model.txt"
import json, os, re, sys, urllib.request
out = sys.argv[1]
b = os.environ.get("OPENAI_BASE_URL", "").rstrip("/")
H = {"Authorization": "Bearer " + os.environ.get("OPENAI_API_KEY", ""), "Content-Type": "application/json"}
key = os.environ.get("OPENAI_API_KEY", "")
def clean(t): return t.replace(key, "***") if key else t
png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
ids = []
try:
    with urllib.request.urlopen(urllib.request.Request(b + "/models", headers=H), timeout=30) as r:
        ids = [m.get("id") for m in json.load(r).get("data", []) if m.get("id")]
except Exception as e:
    ids = []
open(f"{out}/models.txt", "w").write("\n".join(ids) + "\n")
want = os.environ.get("ARTEMIS_DEFAULT_MODEL") or "auto"
vis = [i for i in ids if re.search(r"vl|vision|gemini|gpt-4o|gpt-4\.1|llama-4|pixtral|gemma-3|qwen.*vl|omni|scout|maverick", i, re.I)]
cands = [want] + [i for i in vis if i != want][:8]
log = open(f"{out}/vision-preflight.txt", "w")
chosen = ""
for m in cands:
    body = {"model": m, "max_tokens": 300, "messages": [{"role": "user", "content": [
        {"type": "text", "text": "color?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + png}}]}]}
    req = urllib.request.Request(b + "/chat/completions", data=json.dumps(body).encode(), method="POST", headers=H)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            code, txt = r.status, r.read().decode()[:300]
    except urllib.error.HTTPError as e:
        code, txt = e.code, e.read().decode(errors="replace")[:300]
    except Exception as e:
        code, txt = 0, repr(e)[:200]
    log.write(f"model={m} http={code} body={clean(txt)}\n"); log.flush()
    if code == 200:
        chosen = m; break
print(chosen)
PY
VISION_MODEL=$(tr -d '\n' < "$OUT/artemis/vision-model.txt")
if [ -z "$VISION_MODEL" ]; then
  mark llm-vision FAIL "aucun modèle freellmapi n'accepte les images (voir artemis/vision-preflight.txt)"
  ARTEMIS_OK=0
else
  export ARTEMIS_DEFAULT_MODEL="$VISION_MODEL"
  mark llm-vision PASS "modèle vision accepté: $VISION_MODEL"
fi
if [ "$ARTEMIS_OK" = "1" ] && artemis_run smoke "Open the Settings app and report the Android version shown on screen." 90; then
  mark artemis-smoke PASS "LLM + device loop operational"
else
  ARTEMIS_OK=0
  mark artemis-smoke WARN "Artemis unavailable, API fallback only (see artemis/smoke.log)"
fi

artemis_run rider-create \
  "Open the Galass Rider app. From the home screen, select a destination, add two intermediate stops, choose cash payment, then confirm the ride request. Stay on the ride screen." \
  210 "$RIDER_PKG"
artemis_rc=$?
case $artemis_rc in
  0) mark artemis-rider-create PASS "UI create attempt completed" ;;
  2) mark artemis-rider-create SKIP "Artemis disabled by smoke" ;;
  *) mark artemis-rider-create WARN "rc=$artemis_rc (API fallback next)" ;;
esac

# Driver app au premier plan AVANT la création: l'offre arrive en direct.
ensure_fg "$DRIVER_PKG"
api pick | tee -a "$OUT/run.log"
RIDE_ID=$("$PY" -c "import json;print(json.load(open('$OUT/state.json')).get('ride_id') or '')" 2>/dev/null)
if [ -n "$RIDE_ID" ]; then
  mark ride-exists PASS "ride_id=$RIDE_ID (rider $RIDER_ID, 2 stops, cash)"
else
  mark ride-exists FAIL "no ride created"
fi

# Offre chauffeur: capture puis acceptation UI immédiate (évite le 409 offer_expired).
snap 03b-driver-offer "$DRIVER_PKG" 'Accepter la course' 20 && tap_ui 'Accepter la course' || true
sleep 2
api accept | tee -a "$OUT/run.log"
# 03 = rider: course créée / chauffeur trouvé
snap 03-rider-ride-created "$RIDER_PKG" 'E2E Driver|Recherche|chauffeur|E2E Dest' 30 || true
snap 04-driver-accepted "$DRIVER_PKG" 'E2E Pickup|Démarrer|OTP|Arr[eê]t|Prise en charge' 20 || true

api start | tee -a "$OUT/run.log"
# Course en cours côté chauffeur: si l'accueil reste affiché, ouvrir l'onglet Courses.
if ! wait_ui "$DRIVER_PKG" 'Arr[eê]t|E2E Stop' 15; then
  tap_ui 'Courses' && sleep 2 && tap_ui 'E2E|HGD|en cours|Continuer' || true
fi
snap 05-driver-running "$DRIVER_PKG" 'Arr[eê]t|E2E Stop' 20 || true
snap 06-rider-running "$RIDER_PKG" 'Arr[eê]t 1|E2E Stop' 30 || true
if grep -qi "E2E Stop" "$OUT/ui/06-rider-running.xml" 2>/dev/null; then
  mark rider-shows-stops PASS "stop labels present in rider UI dump"
else
  mark rider-shows-stops WARN "uiautomator dump has no stop labels (Flutter semantics)"
fi

artemis_run driver-stops \
  "Open the Galass Driver app and complete the pending stops of the running ride one by one until every stop is marked done." \
  180 "$DRIVER_PKG"
stops_rc=$?
mark artemis-driver-stops "$([ $stops_rc -eq 0 ] && echo PASS || { [ $stops_rc -eq 2 ] && echo SKIP || echo WARN; })" "UI attempt; API completion follows"

STOP_HOOK="$(pwd)/e2e/stop_hook.sh" OUT="$OUT" api stops | tee -a "$OUT/run.log"
snap 07-driver-stops-done "$DRIVER_PKG" 'Terminer|Destination|E2E Dest|Arriv' 20 || true

api end | tee -a "$OUT/run.log"
snap 08-driver-ride-ended "$DRIVER_PKG" 'paiement|Paiement|terminée|CFA|XOF' 30 || true
snap 09-rider-ended-before-payment "$RIDER_PKG" 'Course terminée|arrivé à destination|Payer maintenant' 30 || true
api pay_rider | tee -a "$OUT/run.log"
snap 10-rider-after-cash-choice "$RIDER_PKG" 'attente|chauffeur' 30 || true
snap 11-driver-cash-to-collect "$DRIVER_PKG" 'Reçois le paiement|Confirmer le paiement' 40 || true
tap_ui 'Confirmer le paiement' || true
sleep 3
api cash_driver | tee -a "$OUT/run.log"
wait_gone "$DRIVER_PKG" 'Reçois le paiement|Confirmer le paiement' 30 || true
snap 12-driver-after-cash-confirmed "$DRIVER_PKG" 'content-desc="[^"]+"' 15 || true
api status | tee -a "$OUT/run.log"
snap 13-rider-after "$RIDER_PKG" 'Course terminée|Avis' 40 || true
cp "$OUT/ui/13-rider-after.xml" "$OUT/08-rider-after.xml" 2>/dev/null || true

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

if [ -s "$OUT/screens/13-rider-after.png" ] && [ -s "$OUT/screens/06-rider-running.png" ] \
   && ! cmp -s "$OUT/screens/06-rider-running.png" "$OUT/screens/13-rider-after.png"; then
  mark rider-panel-closed PASS "rider screen changed after ride end (see 06 vs 13)"
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
if ! curl -s -m 3 http://127.0.0.1:8000/ >/dev/null 2>&1; then
  ( cd vendor/artemis && nohup ./.venv/bin/artemis-admin > "$(pwd)/../../$D/artemis-admin.log" 2>&1 & )
  for _ in $(seq 1 30); do curl -s -m 2 http://127.0.0.1:8000/ >/dev/null 2>&1 && break; sleep 2; done
fi
echo "artemis service http=$(curl -s -o /dev/null -w '%{http_code}' -m 5 http://127.0.0.1:8000/)" > "$D/daemon-status.txt"
cp -r vendor/artemis/logs "$D/artemis-logs" 2>/dev/null || true
find "$HOME/.artemis" vendor/artemis -name "*.log" -newer "$OUT/run.log" 2>/dev/null | head -20 | while read -r f; do cp "$f" "$D/" 2>/dev/null; done
for ep in /api/sessions /api/system/readiness /api/devices /api/system/adb/server; do
  curl -s -m 10 "http://127.0.0.1:8000$ep" > "$D/daemon$(echo "$ep" | tr '/' '_').json" 2>/dev/null || true
done
adb shell dumpsys accessibility > "$D/dumpsys-accessibility.txt" 2>/dev/null || true
adb shell pm list packages 2>/dev/null | grep -i artemis > "$D/artemis-packages.txt" || true
adb logcat -d -v threadtime 2>/dev/null | grep -aiE "artemis|accessib|ANR in|not responding" | tail -n 2000 > "$D/logcat-artemis-anr.txt" || true
adb shell top -bn1 -m 15 > "$D/emu-top.txt" 2>/dev/null || true
adb shell uptime > "$D/emu-uptime.txt" 2>/dev/null || true
{ uptime; nproc; free -m; } > "$D/host-load.txt" 2>/dev/null || true
pkill -f artemis-admin >/dev/null 2>&1 || true  # sinon le runner émulateur ne rend jamais la main
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
