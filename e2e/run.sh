#!/usr/bin/env bash
# Staging-only multistop E2E on a GitHub-hosted Android emulator.
# Artemis may drive the UI; api_flow.py only PREPARES data and unblocks the flow after a
# UI failure — each fallback leaves an explicit FAIL mark next to it, so no API call can
# ever turn a failed UI step into a PASS. Every P0 step owns one capture + one explicit
# screen marker; WARN/SKIP on a P0 point makes the run FAIL. Nothing here touches prod.
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

# S6: nothing from a previous run may leak into this verdict.
mkdir -p "$OUT"
rm -rf "$OUT"/screens "$OUT"/ui "$OUT"/state.json "$OUT"/results-raw.txt \
       "$OUT"/results-live.txt "$OUT"/captures.txt "$OUT"/ui-flags.txt \
       "$OUT"/last-ride-state.txt "$OUT"/watchdog-report.txt
mkdir -p "$OUT/screens" "$OUT/ui" "$OUT/logcat" "$OUT/artemis"
: > "$OUT/run.log"
: > "$OUT/results-live.txt"
: > "$OUT/captures.txt"

declare -a RESULTS=()
. "$(dirname "$0")/ui.sh"

# S21: details go to $OUT/run.log only; stdout carries POINT|RESULT and nothing else,
# so a ride uid can never leak into the public Actions log.
log() { printf '[e2e %s] %s\n' "$(date -u +%H:%M:%S)" "$*" >> "$OUT/run.log"; }
mark() {
  RESULTS+=("$1|$2|$3")
  printf '%s|%s|%s\n' "$1" "$2" "$3" >> "$OUT/results-live.txt"
  log "RESULT $1 = $2 ($3)"
  printf '%s|%s\n' "$1" "$2"
}
# S7: every exit path leaves the points that were actually marked.
flush_results() {
  if [ "${#RESULTS[@]}" -gt 0 ]; then
    printf '%s\n' "${RESULTS[@]}" > "$OUT/results-raw.txt"
  else
    : > "$OUT/results-raw.txt"
  fi
}
count_in() { # pattern file -> count on stdout, rc=2 on grep error
  local n rc
  n=$(grep -a -F -c "$1" "$2" 2>/dev/null); rc=$?
  [ "$rc" -gt 1 ] && return 2
  printf '%s' "${n:-0}"
}

# P0 = parcours critique. Un WARN ou un SKIP sur l'un de ces points => verdict FAIL.
P0_POINTS="api-preflight apk-base-url emulator-boot apk-install \
rider-home-loaded rider-destination-selected rider-stop-picker rider-multistop-summary \
rider-driver-assigned rider-running-start rider-stop1-active rider-stop1-done \
rider-stop2-done rider-destination-arrived rider-cash-methods rider-cash-waiting \
rider-final-itinerary rider-rating rider-home-after \
driver-offer-with-stops driver-accepted-details driver-pickup-arrived driver-running-stop1 \
driver-stop1-done driver-stop2-done driver-cash-request driver-cash-confirmed driver-summary \
backend-flow no-prod-calls"
export P0_POINTS

# ---------------------------------------------------------------- MARK table
# Unique source of truth for every screen marker. Grammar: '+regex' present, '-regex'
# absent, '|' = OR, the kind propagates over the alternatives. 'cap' resolves a point's
# marker through `local -n`, so a point with no MARK_ entry can never be captured PASS.
MARK_rider_home_loaded='+Où allez-vous|+Partons pour une course|-Chargement'
MARK_rider_destination_selected='+E2E Dest Point E|-Où allez-vous'
MARK_rider_stop_picker='+Arrêt 1|Rechercher un lieu|-Où allez-vous'
MARK_rider_multistop_summary='+Arrêt 2|Ajouter un arrêt|Changer|-Où allez-vous'
MARK_rider_driver_assigned='+E2E Driver|+E2E-0001'
# B2 — chaque marqueur exige un état qui ne peut pas pré-exister à la transition.
# Un libellé présent dès l'écran précédent (« Arrêt 1 », « Destination ») ne prouve rien.
MARK_rider_running_start='+En course|Terminez votre course|-Arrêt 1|-Chargement'
MARK_rider_stop1_active='+Arrêt en cours|En route vers|ETA|\bmin\b|-Arrêt 1'
MARK_rider_stop1_done='+2/2|1/2 terminé|Arrêt 1\s+terminé|-Chargement'
MARK_rider_stop2_done='+Vous êtes arrivé|Payer maintenant|Facture à payer|-Chargement'
MARK_rider_destination_arrived='+Vous êtes arrivé à destination|+Payer maintenant|Facture à payer|-En attente de la réponse du chauffeur'
MARK_rider_cash_methods='+Espèces|Cash|+Wave|Orange Money'
MARK_rider_cash_waiting='+En attente de la réponse du chauffeur|-Payer maintenant'
MARK_rider_final_itinerary='+Course terminée|+Lieu de prise en charge|+Destination|-Chargement'
MARK_rider_rating='+Commentaire|Notez|étoile|-Chargement'
MARK_rider_home_after='+Où allez-vous|+Partons pour une course|-Course terminée'
MARK_driver_offer_with_stops='+Accepter\s+la\s+course|+REFUSER|+Prix recommandé|+Consignes du client|-Accueil'
MARK_driver_accepted_details='+Point de prise en charge|+Arrêt 1|-Accepter\s+la\s+course'
# B2 — pickup-only : « Point de prise en charge » appartient à l'écran accepté, pas ici.
MARK_driver_pickup_arrived='+Je suis arrivé|Prise en charge du client|-Accepter\s+la\s+course'
MARK_driver_running_stop1='+Arrêt 1|-Accepter\s+la\s+course|+Terminer l.arrêt|Confirmer l.arrêt|\b1\s*/\s*2\b'
MARK_driver_stop1_done='+2/2|Arrêt 2|-Arrêt 1|-Accepter\s+la\s+course'
MARK_driver_stop2_done='+2/2|Arrivée|Vous êtes arrivé|-Accepter\s+la\s+course'
MARK_driver_cash_request='+Reçois le paiement|+Confirmer le paiement|+Tarif de la course'
MARK_driver_cash_confirmed='+Tarif de la course|Distance|-Reçois\s+le\s+paiement|-Confirmer\s+le\s+paiement'
MARK_driver_summary='+Gains|Résumé|Activité|-Reçois\s+le\s+paiement'
# Non-P0 captures (same table, same rules).
MARK_driver_home_online='+En ligne'
# S3 : la capture in-ride doit nier l'offre (-REFUSER) pour être distinguable de (a).
MARK_driver_inride_stops='+Terminer la course|+Arrêt 1|-Accepter\s+la\s+course|-REFUSER'

# Les 9 écrans Driver n'ont jamais été atteints par le run 39992138248 (acceptation UI
# impossible) : leurs libellés sont des hypothèses, taguées dans le sidecar.
MARK_HYPOTHESIS="driver_offer_with_stops driver_accepted_details driver_pickup_arrived \
driver_running_stop1 driver_stop1_done driver_stop2_done driver_cash_request \
driver_cash_confirmed driver_summary"

marker_source() {
  case " $MARK_HYPOTHESIS " in
    *" $1 "*) echo hypothesis ;;
    *) echo grounded ;;
  esac
}

# Flags UI_* : une transition n'est acquise que si l'écran SUIVANT le prouve.
UI_ACCEPT=0; UI_START=0; UI_STOP0=0; UI_STOP1=0; UI_END=0; UI_CASH=0
write_flags() {
  printf 'UI_ACCEPT=%s\nUI_START=%s\nUI_STOP0=%s\nUI_STOP1=%s\nUI_END=%s\nUI_CASH=%s\n' \
    "$UI_ACCEPT" "$UI_START" "$UI_STOP0" "$UI_STOP1" "$UI_END" "$UI_CASH" > "$OUT/ui-flags.txt"
}

# S13: a wedged emulator must still leave a verdict — AND release the step.
# B10 (run 40177022780): l'ancien `( sleep 8100; ... ) &` forkait un `sleep`
# enfant que le `kill $WATCHDOG_PID` de l'EXIT ne touchait PAS: l'orphelin
# survivait en tenant le pipe stdout du step — jamais d'EOF, le step
# n'a jamais rendu la main (2h02 d'attente, job coupé à timeout-minutes:150).
# Fix: le watchdog tourne dans son PROPRE groupe (setsid), ses fds sont
# détachés du pipe (`</dev/null >log 2>&1`), le trap tue tout le groupe
# (`kill -- -PGID`), et à l'échéance le watchdog flush le verdict partiel
# PUIS tue le script — le step ne peut plus dépasser son budget mur.
P0_CSV="$(echo $P0_POINTS | tr ' ' ',')"
# La chaîne des échéances doit rester strictement ordonnée (le test la vérifie) :
# watchdog < timeout-minutes du step < timeout-minutes du job.
RUN_DEADLINE_MIN="${RUN_DEADLINE_MIN:-90}"
WD_SLEEP="${WATCHDOG_SLEEP_S:-$((RUN_DEADLINE_MIN * 60))}"
STEP_START=$SECONDS
RUN_PID=$$
export OUT P0_CSV PY RUN_DEADLINE_MIN WD_SLEEP RUN_PID
export -f log write_flags
WATCHDOG_PGID=""
spawn_watchdog() {
  if ! command -v setsid >/dev/null 2>&1; then
    log "watchdog: setsid absent — pas de watchdog (step protégé par timeout-minutes)"
    return 1
  fi
  : > "$OUT/watchdog.pgid"
  setsid bash -c "$(cat <<'WD'
# setsid fait de ce bash un chef de session : son PID EST le PGID. On l'écrit
# nous-mêmes, car $! côté parent peut être le PID de setsid s'il a forké.
echo $$ > "$OUT/watchdog.pgid"
sleep "$WD_SLEEP"
cp "$OUT/results-live.txt" "$OUT/results-raw.txt" 2>/dev/null || :
echo "watchdog: deadline ${RUN_DEADLINE_MIN}min — flush partiel + kill du script (S13)" >> "$OUT/run.log"
write_flags
"$PY" e2e/report.py "$OUT" --p0 "$P0_CSV" > "$OUT/watchdog-report.txt" 2>&1 || :
echo "watchdog: kill -TERM $RUN_PID" >> "$OUT/run.log"
kill -TERM "$RUN_PID" 2>/dev/null || :
WD
)" > "$OUT/watchdog-live.txt" 2>&1 < /dev/null &
  WATCHDOG_PGID=$!
  for _ in $(seq 1 25); do
    [ -s "$OUT/watchdog.pgid" ] && break
    sleep 0.2
  done
  [ -s "$OUT/watchdog.pgid" ] && WATCHDOG_PGID="$(tr -dc '0-9' < "$OUT/watchdog.pgid")"
  log "watchdog: deadline ${RUN_DEADLINE_MIN}min pgid=${WATCHDOG_PGID:-?}"
}
release_watchdog() {
  [ -n "${WATCHDOG_PGID:-}" ] || return 0
  kill -- "-${WATCHDOG_PGID}" 2>/dev/null || kill "$WATCHDOG_PGID" 2>/dev/null || true
}
spawn_watchdog || true
trap 'release_watchdog; kill "${LOGCAT_PID:-}" 2>/dev/null; pkill -P "$$" 2>/dev/null; true' EXIT
# Le watchdog TERM le script à l'échéance. Sans handler, bash meurt sans rien
# flusher (le seul verdict serait celui du watchdog) : on rend le verdict partiel
# et un code 124 explicite, et l'EXIT ferme les groupes derrière.
trap 'log "deadline: SIGTERM — verdict partiel"; flush_results; write_flags; "$PY" e2e/report.py "$OUT" --p0 "$P0_CSV" > "$OUT/watchdog-report.txt" 2>&1 || true; exit 124' TERM INT

# Frontière de phase : un sous-étape qui a mangé le budget mur rend un verdict
# partiel tout de suite, au lieu d'attendre le watchdog puis le timeout du job.
deadline_check() {
  local limit_s=$(( RUN_DEADLINE_MIN * 60 ))
  [ $(( SECONDS - STEP_START )) -lt "$limit_s" ] && return 0
  log "deadline: budget ${RUN_DEADLINE_MIN}min épuisé avant '$1' — verdict partiel"
  flush_results
  write_flags
  "$PY" e2e/report.py "$OUT" --p0 "$P0_CSV" > "$OUT/deadline-report.txt" 2>&1 || true
  exit 124
}

# cap point name pkg timeout : capture + verdict strict sur le marqueur du point.
# The point and its source are passed to snap explicitly (S4), never inherited.
cap() {
  local point="$1" name="$2" pkg="$3" tmo="$4" spec src
  local -n ref="MARK_${point//-/_}"
  spec="${ref:-}"
  src="$(marker_source "${point//-/_}")"
  if [ -z "$spec" ]; then
    mark "$point" FAIL "no marker declared for $point (harness)"
    return 1
  fi
  if snap "$name" "$pkg" "$tmo" "$spec" "" "$point" "$src"; then
    mark "$point" PASS "ui cap=$name.png marker ok ($src)"
  else
    mark "$point" FAIL "ui cap=$name.png marker absent ($spec)"
  fi
}

api() { STEP="$1" "$PY" e2e/api_flow.py; }
# ui_action exige le dialogue de confirmation ; try_tap ne l'exige pas (navigation).
try_confirm() { local pkg="$1"; shift; local l; for l in "$@"; do ui_action "$pkg" "$l" && return 0; done; return 1; }
try_tap() { local pkg="$1"; shift; tap_when "$pkg" "$@"; }
ride_state() {
  STEP=ride_state "$PY" e2e/api_flow.py > "$OUT/last-ride-state.txt" 2>&1
  log "ride_state: $(tr '\n' ' ' < "$OUT/last-ride-state.txt")"
}

launch() { adb shell monkey -p "$1" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1; }
# wait_fg : un cold start de debug Flutter dépasse largement 6s. Attendre le premier
# plan évite un `wait_ui` qui rend rc=3 sur le launcher avant même la première frame.
wait_fg() {
  local pkg="$1" tmo="${2:-45}" t0=$SECONDS
  while [ $((SECONDS - t0)) -lt "$tmo" ]; do
    [ "$(fg_pkg)" = "$pkg" ] && return 0
    sleep 2
  done
  log "wait_fg: $pkg jamais au premier plan (${tmo}s, fg=$(fg_pkg))"
  return 1
}
# Bascule explicite entre étapes (jamais depuis un wait).
bring_fg() {
  [ "$(fg_pkg)" = "$1" ] && return 0
  log "bring_fg $(fg_pkg) -> $1"
  adb shell monkey -p "$1" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1
  wait_fg "$1" 30
}

notif_probe() { # pkg token -> json
  adb shell dumpsys notification --noredact 2>/dev/null \
    | "$PY" e2e/notif.py "$1" "${2:-}" 2>/dev/null \
    || echo '{"records_total":0,"records_pkg":0,"records_token":0}'
}
json_field() { "$PY" -c 'import json,sys;print(json.load(sys.stdin).get(sys.argv[1],0))' "$1"; }

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
  if [ "$rc" -ne 0 ]; then
    log "artemis fallback: $name indisponible ($( [ "$rc" = 124 ] && echo timeout || echo "rc=$rc" )) — reprise par taps + API"
    tail -n 5 "$OUT/artemis/$name.log" 2>/dev/null | sed 's/^/artemis-log: /' >> "$OUT/run.log" || true
    adb exec-out screencap -p > "$OUT/screens/artemis-$name-fail.png" 2>/dev/null || true
  fi
  return $rc
}

write_prefs() {
  # pkg token user_id name email phone
  local pkg="$1" access="$2" uid="$3" name="$4" email="$5" phone="$6"
  # S18: the XML is piped straight into the device — no /tmp file holding an access token.
  # B10 (run 40177022780): le dossier doit exister AVANT le cat. L'ordre ancien
  # (mkdir APRÈS le pipe) échouait pour les deux apps (« can't create
  # shared_prefs/FlutterSharedPreferences.xml ») et les deux apps quittaient le
  # premier plan pour le launcher — 33 points UI en FAIL sur un seul bug d'ordre.
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
  adb shell "run-as $pkg chmod 660 shared_prefs/FlutterSharedPreferences.xml" >/dev/null 2>&1
  # Readback: sans ce contrôle, un prefs non écrit ne se voyait qu'à l'écran
  # (app renvoyée au launcher) et coûtait 33 points UI (run 40177022780).
  if adb shell "run-as $pkg test -s shared_prefs/FlutterSharedPreferences.xml" >/dev/null 2>&1; then
    log "prefs injected: $pkg (readback ok)"
  else
    log "prefs injected: $pkg (READBACK FAILED)"
  fi
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

# APK actually targets staging, not prod (Rider + Driver — H10)
for a in rider driver; do
  case "$a" in rider) apk="$APK_RIDER";; driver) apk="$APK_DRIVER";; esac
  if "$PY" - "$apk" <<'PY'
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
    mark "apk-base-url-$a" PASS "APK $a built with API_BASE_URL=staging"
  else
    mark "apk-base-url-$a" FAIL "APK $a does not embed the staging base URL"
  fi
done
# compat: le point P0 historique couvre les deux APK
if [ "$(printf '%s\n' "${RESULTS[@]}" | grep -c '^apk-base-url-.*|FAIL|')" = "0" ]; then
  mark apk-base-url PASS "Rider + Driver embed the staging base URL"
else
  mark apk-base-url FAIL "at least one APK does not embed the staging base URL"
fi

# SHA-1 des APK, calculé à l'étape de build et transmis par GITHUB_ENV
if [ -n "${APK_SHA1_RIDER:-}" ] || [ -n "${APK_SHA1_DRIVER:-}" ]; then
  if [ "${APK_SHA1_RIDER:-x}" = "${APK_SHA1_TARGET:-y}" ] && [ "${APK_SHA1_DRIVER:-x}" = "${APK_SHA1_TARGET:-y}" ]; then
    mark apk-sha1 PASS "rider=${APK_SHA1_RIDER:-?} driver=${APK_SHA1_DRIVER:-?}"
  else
    mark apk-sha1 WARN "rider=${APK_SHA1_RIDER:-?} driver=${APK_SHA1_DRIVER:-?} attendu=${APK_SHA1_TARGET:-?}"
  fi
fi

# ------------------------------------------------------------------- device
# boot and install legitimately exceed the per-call budget; everything else does not.
ADB_TMO=300 adb wait-for-device
for _ in $(seq 1 90); do
  [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ] && break
  sleep 5
done
if [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" != "1" ]; then
  mark emulator-boot FAIL "sys.boot_completed never reached 1"
  # S7: flush every point marked before the failure, not just emulator-boot.
  flush_results
  write_flags
  "$PY" e2e/report.py "$OUT" --p0 "$P0_CSV"
  exit 1
fi
mark emulator-boot PASS "$(adb shell getprop ro.build.version.release | tr -d '\r') / API $(adb shell getprop ro.build.version.sdk | tr -d '\r')"
adb shell settings put global window_animation_scale 0 >/dev/null 2>&1
adb shell settings put global transition_animation_scale 0 >/dev/null 2>&1
adb shell settings put global animator_duration_scale 0 >/dev/null 2>&1
adb shell input keyevent 82 >/dev/null 2>&1
adb shell svc power stayon true >/dev/null 2>&1
adb shell settings put global hide_error_dialogs 1 >/dev/null 2>&1
adb emu geo fix -17.4677 14.7167 >/dev/null 2>&1 || true
adb logcat -c >/dev/null 2>&1 || true
# B9/S16: the buffer is streamed from boot, so a rotation mid-run cannot erase the
# evidence, and the scan below reads the whole window instead of a single snapshot.
LOG_FILE="$OUT/logcat/stream.log"
: > "$LOG_FILE"
command adb logcat -v threadtime > "$LOG_FILE" 2>&1 &
LOGCAT_PID=$!

# ------------------------------------------------------------------ install
ADB_TMO=600 adb install -r -t -g "$APK_RIDER" >/dev/null 2>&1 && r1=0 || r1=1
ADB_TMO=600 adb install -r -t -g "$APK_DRIVER" >/dev/null 2>&1 && r2=0 || r2=1
if [ $r1 -eq 0 ] && [ $r2 -eq 0 ]; then
  mark apk-install PASS "Rider + Driver installed"
else
  mark apk-install FAIL "rider_rc=$r1 driver_rc=$r2"
fi

write_prefs "$RIDER_PKG" "${RIDER_TOKEN:-}" "$RIDER_ID" "E2E Rider" "e2e.rider@galass.sn" "+221700000106"
write_prefs "$DRIVER_PKG" "${DRIVER_TOKEN:-}" "$DRIVER_ID" "E2E Driver" "e2e.driver@galass.sn" "+221700000023"

# ------------------------------------------------------------------ launch
launch "$RIDER_PKG"; wait_fg "$RIDER_PKG" 60 || true
cap rider-home-loaded 14-rider-home-loaded "$RIDER_PKG" 90
launch "$DRIVER_PKG"; wait_fg "$DRIVER_PKG" 60 || true
if snap 33-driver-home-online "$DRIVER_PKG" 60 "$MARK_driver_home_online" "" "driver-home-online" "grounded"; then
  mark driver-home-online PASS "ui cap=33-driver-home-online.png"
else
  mark driver-home-online WARN "driver home sans « En ligne » (33)"
fi
# Baseline B7 prise AVANT toute composition de course : l'offre peut être poussée dès
# la création, donc une baseline juste avant `api pick` serait déjà polluée.
NOTIF_BEFORE=$(notif_probe "$DRIVER_PKG" "")
NOTIF_BEFORE_PKG=$(printf '%s' "$NOTIF_BEFORE" | json_field records_pkg)
log "notif baseline: $NOTIF_BEFORE"

# Préflight vision (inchangé) + smoke Artemis.
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
except Exception:
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
  mark llm-vision WARN "aucun modèle freellmapi n'accepte les images (voir artemis/vision-preflight.txt)"
  ARTEMIS_OK=0
elif ! printf '%s' "$VISION_MODEL" | grep -Eq '^[A-Za-z0-9._:/-]{1,80}$'; then
  # B6: the id is untrusted (remote /models listing). Refuse anything that could
  # become a sed/shell token instead of splicing it into a command line.
  mark llm-vision FAIL "VISION_MODEL rejeté (caractères non sûrs): $(printf '%s' "$VISION_MODEL" | tr -c 'A-Za-z0-9._:/-\n' '?')"
  ARTEMIS_OK=0
else
  export ARTEMIS_DEFAULT_MODEL="$VISION_MODEL"
  if "$PY" e2e/set_model.py vendor/artemis/config/artemis.jsonc "$VISION_MODEL" >/dev/null; then
    grep -c -F "\"model\": \"$VISION_MODEL\"" vendor/artemis/config/artemis.jsonc > "$OUT/artemis/profile-model-count.txt" || true
    mark llm-vision PASS "modèle vision accepté: $VISION_MODEL"
  else
    mark llm-vision FAIL "réécriture de la config Artemis refusée"
    ARTEMIS_OK=0
  fi
fi
if [ "$ARTEMIS_OK" = "1" ] && artemis_run smoke "Open the Settings app and report the Android version shown on screen." 200; then
  mark artemis-smoke PASS "LLM + device loop operational"
else
  ARTEMIS_OK=0
  # B10 (run 40177022780): le smoke timeout systématique (rc=124 sur ce runner,
  # déjà observing le 39992138248) ne doit pas verrouiller le verdict: Artemis
  # est indisponible -> fallback taps + API, et le smoke est WARN, pas FAIL.
  mark artemis-smoke WARN "Artemis unavailable — fallback taps + API pour toutes les étapes UI (voir artemis/smoke.log)"
fi

# -------------------------------------------------- rider compose (UI only)
deadline_check rider-compose  # budget mur : verdict partiel plutôt qu'un step qui traîne
api reset >> "$OUT/run.log" 2>&1
bring_fg "$RIDER_PKG" || true

artemis_run rider-destination \
  "Open the Galass Rider app. On the home screen choose the destination \"E2E Dest Point E\" (search it if needed) and return to the ride summary. Do not add stops yet." \
  180 "$RIDER_PKG"
rc=$?
mark artemis-rider-destination "$([ $rc -eq 0 ] && echo PASS || echo SKIP)" "artemis fallback (rc=$rc — étape reprise par taps + API, voir artemis/rider-destination.log)"
bring_fg "$RIDER_PKG" || true
cap rider-destination-selected 15-rider-destination-selected "$RIDER_PKG" 25

# 16 : picker ouvert par « Ajouter un arrêt »
try_tap "$RIDER_PKG" "Ajouter un arrêt" >/dev/null 2>&1 || true
cap rider-stop-picker 16-rider-stop-picker "$RIDER_PKG" 20

artemis_run rider-stop1 \
  "In the Galass Rider app, in the stop picker you are on, choose the stop \"E2E Stop 1 Médina\" and confirm. Return to the ride summary." \
  180 "$RIDER_PKG"
rc=$?
mark artemis-rider-stop1 "$([ $rc -eq 0 ] && echo PASS || echo SKIP)" "artemis fallback (rc=$rc — étape reprise par taps + API, voir artemis/rider-stop1.log)"

try_tap "$RIDER_PKG" "Ajouter un arrêt" >/dev/null 2>&1 || true
artemis_run rider-stop2 \
  "In the Galass Rider app, in the stop picker you are on, choose the stop \"E2E Stop 2 Fann\", confirm, select cash payment, then confirm the ride request. Stay on the ride screen." \
  210 "$RIDER_PKG"
rc=$?
mark artemis-rider-stop2 "$([ $rc -eq 0 ] && echo PASS || echo SKIP)" "artemis fallback (rc=$rc — étape reprise par taps + API, voir artemis/rider-stop2.log)"
bring_fg "$RIDER_PKG" || true
cap rider-multistop-summary 17-rider-multistop-summary "$RIDER_PKG" 25

# ------------------------------------------------------------- driver offer
deadline_check driver-offer  # budget mur : verdict partiel plutôt qu'un step qui traîne
bring_fg "$DRIVER_PKG" || true
api pick >> "$OUT/run.log" 2>&1
RIDE_ID=$("$PY" -c "import json;print(json.load(open('$OUT/state.json')).get('ride_id') or '')" 2>/dev/null)
# S14: the ride identifier is a uid, never called a token (it is not a credential).
RIDE_UID=$("$PY" -c "
import json
st=json.load(open('$OUT/state.json'))
r=st.get('ride_picked') or {}
print(r.get('uid') or (st.get('ride_create') or {}).get('uid') or '')
" 2>/dev/null)
RIDE_SOURCE=$("$PY" -c "import json;print(json.load(open('$OUT/state.json')).get('ride_source') or 'unknown')" 2>/dev/null)
[ -n "$RIDE_SOURCE" ] || RIDE_SOURCE=unknown
# B8: a ride composed by the harness proves nothing about the Rider UI.
if [ "$RIDE_SOURCE" = "api" ]; then
  mark ride-composed FAIL "api fallback: compose"
elif [ "$RIDE_SOURCE" = "ui" ]; then
  mark ride-composed PASS "course composée dans l'app (adoptée par l'API)"
else
  mark ride-composed WARN "ride_source inconnu ($RIDE_SOURCE)"
fi
if [ -n "$RIDE_ID" ]; then
  mark ride-exists PASS "ride_id=$RIDE_ID uid=$RIDE_UID (rider $RIDER_ID, 2 stops, cash)"
else
  mark ride-exists FAIL "no ride created"
fi
ride_state

cap driver-offer-with-stops 34-driver-offer-with-stops "$DRIVER_PKG" 25
# B2 (a) — l'offre montre-t-elle les arrêts ? (même XML que la capture)
if [ "$RIDE_SOURCE" != "ui" ]; then
  mark bug-b2-offer-stops WARN "B2 non concluant: ride composée par l'API (ride_source=$RIDE_SOURCE)"
elif ! [ -s "$OUT/ui/34-driver-offer-with-stops.xml" ]; then
  mark bug-b2-offer-stops WARN "B2 non concluant: aucune preuve (34) — no evidence"
elif ! grep -qiE "Accepter" "$OUT/ui/34-driver-offer-with-stops.xml" 2>/dev/null; then
  mark bug-b2-offer-stops WARN "B2 non concluant: carte d'offre non atteinte (34)"
elif grep -qiE "Arrêt [12]|2 arrêts|E2E Stop" "$OUT/ui/34-driver-offer-with-stops.xml" 2>/dev/null; then
  mark bug-b2-offer-stops PASS "arrêts visibles dans l'offre (34)"
else
  mark bug-b2-offer-stops WARN "B2 reproduit: offre sans arrêt visible (34)"
fi

# S10: declines observed while the harness itself retries the accept are not evidence.
DECLINES_BEFORE=$(count_in "ride_unavailable" "$LOG_FILE" 2>/dev/null || echo 0)
if tap_exact "Accepter la course"; then
  if wait_ui "$DRIVER_PKG" 25 "$MARK_driver_accepted_details"; then
    UI_ACCEPT=1; log "acceptation par l'UI (écran suivant prouvé)"
  else
    mark driver-accepted-details FAIL "api fallback: accept"
    api accept >> "$OUT/run.log" 2>&1
  fi
else
  log "tap_exact « Accepter la course » manqué"
  mark driver-accepted-details FAIL "api fallback: accept"
  api accept >> "$OUT/run.log" 2>&1
fi
sleep 2
ride_state
DECLINES_AFTER=$(count_in "ride_unavailable" "$LOG_FILE" 2>/dev/null || echo 0)
declines_pre=$(( DECLINES_AFTER - DECLINES_BEFORE ))
[ "$declines_pre" -lt 0 ] && declines_pre=0

bring_fg "$RIDER_PKG" || true
cap rider-driver-assigned 19-rider-driver-assigned "$RIDER_PKG" 30
bring_fg "$DRIVER_PKG" || true
cap driver-accepted-details 35-driver-accepted-details "$DRIVER_PKG" 30

# B7 — sonde APRÈS acceptation (S9) : la notification est émise à la création, donc
# toujours attribuable, mais l'offre n'a plus le temps d'expirer pendant la sonde.
if [ -z "$RIDE_UID" ] || ! printf '%s' "$RIDE_UID" | grep -Eq '^[A-Za-z0-9_-]{8,}$' \
   || printf '%s' "$RIDE_UID" | grep -Eq '^[0-9]+$'; then
  mark bug-b7-push WARN "unattributed: no ride uid"
  mark push-probe WARN "unattributed: no ride uid (ride_id=$RIDE_ID)"
else
  sleep 20
  NOTIF_AFTER=$(notif_probe "$DRIVER_PKG" "$RIDE_UID")
  NOTIF_AFTER_PKG=$(printf '%s' "$NOTIF_AFTER" | json_field records_pkg)
  NOTIF_AFTER_TOKEN=$(printf '%s' "$NOTIF_AFTER" | json_field records_token)
  NOTIF_DELTA=$(( NOTIF_AFTER_PKG - NOTIF_BEFORE_PKG ))
  log "notif after: $NOTIF_AFTER (baseline pkg=$NOTIF_BEFORE_PKG delta=$NOTIF_DELTA)"
  adb shell cmd statusbar expand-notifications >/dev/null 2>&1 || true
  sleep 3
  adb exec-out screencap -p > "$OUT/screens/46b-notification-shade.png" 2>/dev/null
  adb shell cmd statusbar collapse >/dev/null 2>&1 || true
  if [ "$RIDE_SOURCE" != "ui" ]; then
    mark bug-b7-push WARN "non concluant: ride composée par l'API (ride_source=$RIDE_SOURCE)"
    mark push-probe WARN "non concluant: ride composée par l'API"
  elif [ "$NOTIF_DELTA" -ge 1 ] && [ "$NOTIF_AFTER_TOKEN" -ge 1 ]; then
    mark bug-b7-push PASS "delta_pkg=$NOTIF_DELTA records_uid=$NOTIF_AFTER_TOKEN (uid=$RIDE_UID)"
    mark push-probe PASS "notification attribuée à la course (uid=$RIDE_UID)"
  elif [ "$NOTIF_DELTA" -ge 1 ]; then
    mark bug-b7-push WARN "unattributed: delta_pkg=$NOTIF_DELTA mais 0 notification portant l'uid $RIDE_UID"
    mark push-probe WARN "delta sans uid => non attribuée (jamais PASS)"
  else
    mark bug-b7-push WARN "B7 reproduit: aucune notification driver (delta_pkg=$NOTIF_DELTA)"
    mark push-probe WARN "notif_records=$NOTIF_AFTER_PKG (46b)"
  fi
fi

# ---------------------------------------------------------- pickup → start
adb emu geo fix -17.4677 14.7167 >/dev/null 2>&1 || true
sleep 3
bring_fg "$DRIVER_PKG" || true
cap driver-pickup-arrived 36-driver-pickup-arrived "$DRIVER_PKG" 20

if try_tap "$DRIVER_PKG" "Prise en charge du client" "Prise en charge" "Démarrer la course"; then
  if wait_ui "$DRIVER_PKG" 25 "$MARK_driver_running_stop1"; then
    UI_START=1; log "start par l'UI chauffeur (écran suivant prouvé)"
  else
    mark rider-running-start FAIL "api fallback: start"
    api start >> "$OUT/run.log" 2>&1
  fi
else
  mark rider-running-start FAIL "api fallback: start"
  api start >> "$OUT/run.log" 2>&1
fi
ride_state
bring_fg "$DRIVER_PKG" || true
cap driver-running-stop1 37-driver-running-stop1 "$DRIVER_PKG" 25
# B2 (b) — seconde capture Driver distincte, en course, après le start.
bring_fg "$DRIVER_PKG" || true
cap driver-inride-stops 35b-driver-inride "$DRIVER_PKG" 25
if ! grep -qiE "Terminer la course|Arrêt 1" "$OUT/ui/35b-driver-inride.xml" 2>/dev/null; then
  mark bug-b2-inride-stops WARN "B2 non concluant: écran de course non atteint (35b)"
elif grep -qiE "Arrêt [12]|E2E Stop" "$OUT/ui/35b-driver-inride.xml" 2>/dev/null; then
  mark bug-b2-inride-stops PASS "arrêts présents dans l'écran de course (35b)"
else
  mark bug-b2-inride-stops WARN "B2: aucun arrêt dans l'écran de course (35b)"
fi

bring_fg "$RIDER_PKG" || true
cap rider-running-start 21-rider-running-start "$RIDER_PKG" 30

# ------------------------------------------------------------------- stops
deadline_check stops  # budget mur : verdict partiel plutôt qu'un step qui traîne
STOP_LAT=(14.71 14.702); STOP_LNG=(-17.462 -17.455)
ui_stops=0
for i in 0 1; do
  n=$((i + 1))
  adb emu geo fix "${STOP_LNG[$i]}" "${STOP_LAT[$i]}" >/dev/null 2>&1 || true
  STOP_INDEX=$i api loc_stop >> "$OUT/run.log" 2>&1
  ride_state
  bring_fg "$RIDER_PKG" || true
  if [ "$i" -eq 0 ]; then
    cap rider-stop1-active 22-rider-stop1-active "$RIDER_PKG" 25
  fi
  bring_fg "$DRIVER_PKG" || true
  next_marker="MARK_driver_stop${n}_done"
  ui_stop_ok=0
  if try_confirm "$DRIVER_PKG" "Confirmer l'arrêt" \
     && wait_ui "$DRIVER_PKG" 20 "${!next_marker}"; then
    ui_stop_ok=1
  fi
  if [ "$ui_stop_ok" = "1" ]; then
    if [ "$i" -eq 0 ]; then UI_STOP0=1; else UI_STOP1=1; fi
    ui_stops=$((ui_stops + 1))
    log "arrêt $n confirmé par l'UI (écran suivant prouvé)"
  else
    mark "driver-stop${n}-done" FAIL "api fallback: stop $n"
    STOP_INDEX=$i api stop_one >> "$OUT/run.log" 2>&1
  fi
  ride_state
  bring_fg "$RIDER_PKG" || true
  if [ "$i" -eq 0 ]; then
    cap rider-stop1-done 23-rider-stop1-done "$RIDER_PKG" 25
  else
    cap rider-stop2-done 24-rider-stop2-done "$RIDER_PKG" 25
  fi
  bring_fg "$DRIVER_PKG" || true
  if [ "$i" -eq 0 ]; then
    cap driver-stop1-done 38-driver-stop1-done "$DRIVER_PKG" 25
  else
    cap driver-stop2-done 39-driver-stop2-done "$DRIVER_PKG" 25
  fi
done
if ! grep -q "stops_done=2" "$OUT/last-ride-state.txt" 2>/dev/null; then
  log "état API: arrêts incomplets après les deux tours"
fi
mark ui-stops "$([ "$ui_stops" -eq 2 ] && echo PASS || echo FAIL)" "taps UI confirmés: $ui_stops/2"

# ---------------------------------------------------------------- ride end
adb emu geo fix -17.4467 14.6928 >/dev/null 2>&1 || true
api loc_dest >> "$OUT/run.log" 2>&1
bring_fg "$DRIVER_PKG" || true
if tap_exact "Terminer la course"; then
  bring_fg "$RIDER_PKG" || true
  if wait_ui "$RIDER_PKG" 25 "$MARK_rider_destination_arrived"; then
    UI_END=1; log "fin par l'UI chauffeur (écran rider prouvé)"
  else
    mark rider-destination-arrived FAIL "api fallback: end"
    api end >> "$OUT/run.log" 2>&1
  fi
else
  mark rider-destination-arrived FAIL "api fallback: end"
  api end >> "$OUT/run.log" 2>&1
fi
ride_state
bring_fg "$RIDER_PKG" || true
cap rider-destination-arrived 25-rider-destination-arrived "$RIDER_PKG" 30
cap rider-cash-methods 26-rider-cash-methods "$RIDER_PKG" 15

# --------------------------------------------------------------- cash flow
deadline_check cash  # budget mur : verdict partiel plutôt qu'un step qui traîne
api pay_rider >> "$OUT/run.log" 2>&1
bring_fg "$RIDER_PKG" || true
cap rider-cash-waiting 27-rider-cash-waiting "$RIDER_PKG" 25
bring_fg "$DRIVER_PKG" || true
cap driver-cash-request 41-driver-cash-request "$DRIVER_PKG" 35
if ! [ -s "$OUT/ui/41-driver-cash-request.xml" ]; then
  mark bug-b5-estimated-time WARN "no evidence: capture 41 absente ou vide"
elif grep -qi "Temps estimé" "$OUT/ui/41-driver-cash-request.xml" 2>/dev/null; then
  mark bug-b5-estimated-time WARN "B5 reproduit: « Temps estimé » affiché après la fin (41)"
else
  mark bug-b5-estimated-time PASS "pas de « Temps estimé » après la fin"
fi
if tap_exact "Confirmer le paiement"; then
  if wait_ui "$DRIVER_PKG" 25 "$MARK_driver_cash_confirmed"; then
    UI_CASH=1; log "cash confirmé par l'UI chauffeur (écran suivant prouvé)"
  else
    mark driver-cash-confirmed FAIL "api fallback: cash"
    api cash_driver >> "$OUT/run.log" 2>&1
  fi
else
  mark driver-cash-confirmed FAIL "api fallback: cash"
  api cash_driver >> "$OUT/run.log" 2>&1
fi
ride_state
api status >> "$OUT/run.log" 2>&1
bring_fg "$DRIVER_PKG" || true
cap driver-cash-confirmed 42-driver-cash-confirmed "$DRIVER_PKG" 30
cap driver-summary 43-driver-summary "$DRIVER_PKG" 25
bring_fg "$RIDER_PKG" || true
cap rider-final-itinerary 29-rider-final-itinerary "$RIDER_PKG" 35
# B3 — la timeline finale doit porter prise en charge + 2 arrêts + destination.
if ! [ -s "$OUT/ui/29-rider-final-itinerary.xml" ]; then
  mark bug-b3-final-timeline WARN "no evidence: capture 29 absente ou vide"
else
  rows=$(grep -o -iE "Arrêt [12]|Destination|Lieu de prise en charge|Prise en charge" "$OUT/ui/29-rider-final-itinerary.xml" | wc -l)
  if [ "$rows" -le 2 ]; then
    mark bug-b3-final-timeline WARN "B3 reproduit: timeline finale à $rows noeuds (29)"
  else
    mark bug-b3-final-timeline PASS "timeline finale: $rows noeuds (29)"
  fi
fi

# ---------------------------------------------------------------- rating
try_tap "$RIDER_PKG" "Noter la course" "Notez la course" "Avis" >/dev/null 2>&1 || true
cap rider-rating 30-rider-rating "$RIDER_PKG" 20
adb shell input keyevent 4 >/dev/null 2>&1 || true
# S11: home-after only means something if the app never relaunched on the way there,
# and only after point 29 was actually observed.
home_left=0
for _ in $(seq 1 10); do
  [ "$(fg_pkg)" = "$RIDER_PKG" ] || { home_left=1; break; }
  sleep 1
done
if [ "$home_left" = "1" ]; then
  mark rider-home-after FAIL "l'app a quitté le premier plan (relance) après back"
elif ! printf '%s\n' "${RESULTS[@]}" | grep -q '^rider-final-itinerary|PASS|'; then
  mark rider-home-after FAIL "point 29 (rider-final-itinerary) non observé"
else
  bring_fg "$RIDER_PKG" || true
  cap rider-home-after 31-rider-home-after "$RIDER_PKG" 25
fi
# B1 — le dialogue « Terminez votre course » ne doit pas flotter au-dessus de l'accueil.
if ! [ -s "$OUT/ui/31-rider-home-after.xml" ]; then
  mark bug-b1-dialog-over-home WARN "no evidence: capture 31 absente ou vide"
elif grep -qi "Terminez votre course" "$OUT/ui/31-rider-home-after.xml" 2>/dev/null; then
  mark bug-b1-dialog-over-home WARN "B1 reproduit: « Terminez votre course » au-dessus de l'accueil (31)"
else
  mark bug-b1-dialog-over-home PASS "pas de dialogue de fin au-dessus de l'accueil"
fi

api cancel >> "$OUT/run.log" 2>&1

# ------------------------------------------------------------- assertions
deadline_check assertions  # budget mur : verdict partiel plutôt qu'un step qui traîne
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
    and int(r.get("payment_status") or 0) == 1
    and d.get("payment_status") in (None, 1)
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

# ------------------------------------------------------------------ logcat
kill "${LOGCAT_PID:-}" 2>/dev/null || true
adb logcat -d -v threadtime >> "$LOG_FILE" 2>/dev/null || true
cp "$LOG_FILE" "$OUT/logcat/full.log" 2>/dev/null || : > "$OUT/logcat/full.log"
grep -a -iE "galassapp\.com|${STAGING_HOST:-__none__}" "$OUT/logcat/full.log" > "$OUT/logcat/filtered.log" 2>/dev/null || true

# B9: an empty or truncated log is "no evidence", never a clean pass. log_scan
# returns 0 clean / 1 no evidence / 2 scan error / 3 forbidden prod host.
LOG_OUT=$(bash -c '. "$1"; log_scan "$2" "${3:-}" 200' _ e2e/logcheck.sh "$OUT/logcat/full.log" "${STAGING_HOST:-}")
LOG_RC=$?
printf '%s\n' "$LOG_OUT" >> "$OUT/logcat/scan.txt"
PROD_HITS=$(printf '%s\n' "$LOG_OUT" | sed -n 's/^prod_hits=//p')
LOG_BYTES=$(printf '%s\n' "$LOG_OUT" | sed -n 's/^bytes=//p')
case "$LOG_RC" in
  0) mark no-prod-calls PASS "0 galassapp.com occurrence (logcat ${LOG_BYTES:-?} octets)" ;;
  3) mark no-prod-calls FAIL "${PROD_HITS:-?} galassapp.com occurrences in logcat" ;;
  1) mark no-prod-calls FAIL "no evidence: logcat absent, vide ou tronqué ($(printf '%s' "$LOG_OUT" | tr '\n' ' '))" ;;
  *) mark no-prod-calls FAIL "scan logcat en erreur: $(printf '%s' "$LOG_OUT" | tr '\n' ' ')" ;;
esac

if ! [ -s "$OUT/logcat/full.log" ]; then
  mark maps-auth WARN "no evidence: logcat/full.log absent ou vide"
else
  maps_auth=$(count_in "Authorization failure" "$OUT/logcat/full.log" || echo "")
  if [ -z "$maps_auth" ]; then
    mark maps-auth WARN "no evidence: scan 'Authorization failure' en erreur"
  elif [ "$maps_auth" -eq 0 ]; then
    mark maps-auth PASS "0 'Authorization failure' (Google Maps)"
  else
    mark maps-auth WARN "$maps_auth 'Authorization failure' (Google Maps)"
  fi
fi

# S10: only declines outside the harness's own accept-retry window count for B4.
if ! [ -s "$OUT/logcat/full.log" ]; then
  mark bug-b4-decline-409 WARN "no evidence: logcat absent ou vide"
else
  declines=$(count_in "ride_unavailable" "$OUT/logcat/full.log" || echo "")
  outside=$(( ${declines:-0} - ${declines_pre:-0} ))
  if [ -z "$declines" ]; then
    mark bug-b4-decline-409 WARN "no evidence: scan ride_unavailable en erreur"
  elif [ "$outside" -le 0 ]; then
    mark bug-b4-decline-409 WARN "non concluant: $declines ride_unavailable, toutes dans la fenêtre de retry du harnais"
  elif [ "$declines" -eq "$outside" ]; then
    mark bug-b4-decline-409 WARN "B4: $outside réponses ride_unavailable hors fenêtre de retry ($declines au total)"
  else
    mark bug-b4-decline-409 WARN "B4: $outside réponses ride_unavailable hors fenêtre ($declines au total, $declines_pre en retry)"
  fi
fi

# --------------------------------------------------------------- report
deadline_check report  # budget mur : verdict partiel plutôt qu'un step qui traîne
D="$OUT/diag"; mkdir -p "$D"
find "$HOME/.artemis" vendor/artemis \( -name "daemon-*.log" -o -name "mcp_server.log" \) 2>/dev/null | head -10 | while read -r f; do cp "$f" "$D/" 2>/dev/null; done
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
pkill -f artemis-admin >/dev/null 2>&1 || true
kill "${LOGCAT_PID:-}" 2>/dev/null || true
flush_results
write_flags
"$PY" e2e/report.py "$OUT" --p0 "$P0_CSV"
exit $?
