#!/usr/bin/env bash
# Staging-only multistop E2E on a GitHub-hosted Android emulator.
# Artemis may drive the UI; api_flow.py only PREPARES data and unblocks the flow after a UI
# failure. Fallbacks stay explicit and provable: accept publishes accept-via + capture 35,
# elsewhere a FAIL mark stays, so no API call can turn a failed UI step into a PASS.
# Every P0 step owns one capture + one explicit screen marker; WARN/SKIP on a P0 point makes
# the run FAIL. Nothing here touches prod.
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
# Chaîne de repli ORDONNÉE de modèles vision (FreeLLMAPI, vérifiés le 2026-10-27).
# L'override workflow (ARTEMIS_DEFAULT_MODEL) passe en TÊTE, dédoublonné.
# La route llama vision est MORTE (429 « lacks vision ») : jamais dans la chaîne.
ARTEMIS_BASE_CHAIN="${ARTEMIS_BASE_CHAIN:-gpt-4.1,gemini-2.5-flash,gpt-4o}"
ARTEMIS_BACKOFF="${ARTEMIS_BACKOFF:-5 15 30}"
# Vide = un essai par modèle de la chaîne ; toute valeur est bornée à la chaîne.
ARTEMIS_MAX_ATTEMPTS="${ARTEMIS_MAX_ATTEMPTS:-}"
# Budget d'une sonde vision (une image par modèle) : une route qui pend ne doit
# pas consommer le budget du step. Passé à vision_preflight.py --timeout.
VISION_PROBE_TIMEOUT="${VISION_PROBE_TIMEOUT:-20}"
# Après un double-429, Artemis attend LLM_PAUSE_TIMEOUT_SECONDS (900 s par défaut)
# avant d'échouer : ce budget mangerait tout le step. 15 s laisse le retry du
# harnais basculer sur la route suivante. Un override utilisateur est respecté.
export LLM_PAUSE_TIMEOUT_SECONDS="${LLM_PAUSE_TIMEOUT_SECONDS:-15}"
# >>> B1
# Artemis est lancé depuis vendor/artemis : un chemin RELATIF qu'on lui passe est
# résolu depuis CE cwd, donc introuvable -> il retombe sur la config vendor et la
# chaîne est inerte. Normalise une fois, au démarrage, en absolu.
# La config Artemis réécrite vit dans $OUT : vendor/ n'est JAMAIS modifié.
abs_path() {
  case "${1:-}" in
    "") printf '%s' "" ;;
    /*) printf '%s' "$1" ;;
    *) printf '%s/%s' "$(pwd)" "$1" ;;
  esac
}
OUT_ABS=$(abs_path "$OUT")
ARTEMIS_CONFIG=$(abs_path "${ARTEMIS_CONFIG:-vendor/artemis/config/artemis.jsonc}")
ARTEMIS_CONFIG_RUNTIME=$(abs_path "${ARTEMIS_CONFIG_RUNTIME:-$OUT/artemis/artemis.jsonc}")
ARTEMIS_CHAIN_FILE=$(abs_path "${ARTEMIS_CHAIN_FILE:-$OUT/artemis/vision-chain.txt}")
# <<< B1
PY="${PY:-python3}"
. "$(dirname "$0")/smoke_log.sh"

# S6: nothing from a previous run may leak into this verdict.
mkdir -p "$OUT"
rm -rf "$OUT"/screens "$OUT"/ui "$OUT"/state.json "$OUT"/results-raw.txt \
       "$OUT"/results-live.txt "$OUT"/captures.txt "$OUT"/ui-flags.txt \
       "$OUT"/last-ride-state.txt "$OUT"/watchdog-report.txt \
       "$OUT"/accept.json "$OUT"/accept-via.txt
mkdir -p "$OUT/screens" "$OUT/ui" "$OUT/logcat" "$OUT/artemis"
: > "$OUT/run.log"
: > "$SMOKE_LOG"
: > "$OUT/results-live.txt"
: > "$OUT/captures.txt"
# Chaîne de repli / config runtime / compteur d'usage : jamais hérités d'un run précédent.
rm -f "$ARTEMIS_CHAIN_FILE" "$ARTEMIS_CONFIG_RUNTIME" "$OUT/artemis/model-usage.txt" \
      "$OUT/artemis/attempts.txt" "$OUT/artemis/vision-model.txt" \
      "$OUT/artemis/vision-chain-requested.txt" "$OUT"/cash.json
mkdir -p "$(dirname "$ARTEMIS_CHAIN_FILE")" "$(dirname "$ARTEMIS_CONFIG_RUNTIME")"

declare -a RESULTS=()
. "$(dirname "$0")/ui.sh"

# S21: details go to $OUT/run.log only; stdout carries POINT|RESULT and nothing else,
# so a ride uid can never leak into the public Actions log. A sanitized copy also goes
# to stderr and e2e/smoke.log (public artifact).
log() {
  local line
  line=$(printf '[e2e %s] %s' "$(date -u +%H:%M:%S)" "$*")
  printf '%s\n' "$line" >> "$OUT/run.log"
  smoke_emit "$line"
}
mark() {
  RESULTS+=("$1|$2|$3")
  printf '%s|%s|%s\n' "$1" "$2" "$3" >> "$OUT/results-live.txt"
  log "RESULT $1 = $2 ($3)"
  printf '%s|%s\n' "$1" "$2"
}
# S7: every exit path leaves the points that were actually marked.
flush_results() {
  smoke_emit_file "$OUT/api-flow.err"
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
MARK_driver_offer_with_stops='+Accepter\s+la\s+course|+REFUSER|+Prix recommandé|+Consignes du client'
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
# UI_TAP_FAILED : un tap a bien été joué mais n'a pas produit l'écran suivant.
UI_TAP_FAILED=0
# ACCEPT_VIA : ui | api | failed — écrit aussi dans $OUT/accept-via.txt, car le
# watchdog est forké AVANT la phase d'accept et ne verrait pas une variable tardive.
ACCEPT_VIA=""
write_flags() {
  local via="${ACCEPT_VIA:-}"
  [ -n "$via" ] || via="$(cat "$OUT/accept-via.txt" 2>/dev/null || true)"
  [ -n "$via" ] || via="none"
  printf 'UI_ACCEPT=%s\nUI_START=%s\nUI_STOP0=%s\nUI_STOP1=%s\nUI_END=%s\nUI_CASH=%s\nACCEPT_VIA=%s\n' \
    "$UI_ACCEPT" "$UI_START" "$UI_STOP0" "$UI_STOP1" "$UI_END" "$UI_CASH" "$via" > "$OUT/ui-flags.txt"
}
set_accept_via() { ACCEPT_VIA="$1"; printf '%s\n' "$1" > "$OUT/accept-via.txt"; }
# Un crédit `ui` exige un tap prouvé (Driver devant AVANT et APRÈS). Un marker 35
# prouve l'écran, pas le tap : sans preuve, la course avance mais le crédit reste
# `unknown` — même vérité que `accepted_via()` côté API.
ui_accept_via() { if [ "${UI_TAP_PROVEN:-0}" = 1 ]; then echo ui; else echo unknown; fi; }

# D2 (run 40205373538): UN SEUL libellé accept_via, dérivé une fois des preuves
# réelles (tap prouvé, écran prouvé, API 2xx) et journalisé UNE seule fois. Deux
# logs dispersés pouvaient se contredire — c'est exactement ce qui a rendu le
# verdict ambigu.
resolve_accept_label() {
  local api_via="" out="" via="" cause="" api_ok=0 api_2xx=0 assigned=0
  # `assigned` = la course est-elle à nous, preuve backend (accept.json .ok) ou
  # écran d'après-acceptation (UI_ACCEPT) : sans lui, un 4xx qui n'assignait rien
  # empruntait la cause "assigned_without_tap_proof".
  assigned=$("$PY" -c 'import json,sys
try:
    print(1 if json.load(open(sys.argv[1])).get("ok") else 0)
except Exception:
    print(0)' "$OUT/accept.json" 2>/dev/null || true)
  if [ "$assigned" != "1" ] && [ "${UI_ACCEPT:-0}" = "1" ]; then
    assigned=1
  fi
  if [ "${ACCEPT_OK:-0}" != "1" ]; then
    # Aucun via n'a prouvé l'acceptation : "failed", avec la cause exacte du
    # repli API — jamais un "unknown" qui laisserait croire à un succès.
    cause=$("$PY" -c 'import json,sys
try:
    print(json.load(open(sys.argv[1])).get("cause") or "unknown")
except Exception:
    print("unknown")' "$OUT/accept.json" 2>/dev/null || true)
    ACCEPT_VIA="failed"
    ACCEPT_CAUSE="${cause:-unknown}"
  else
    api_via=$("$PY" -c 'import json,sys
try:
    print(json.load(open(sys.argv[1])).get("via") or "")
except Exception:
    print("")' "$OUT/accept.json" 2>/dev/null || true)
    # accepted_via() ne renvoie "api" que sur un 2xx classé offer_accepted.
    if [ "$api_via" = "api" ]; then api_ok=1; api_2xx=1; fi
    out=$("$PY" e2e/offer_flow.py label \
            --ui-tap-proven "${UI_TAP_PROVEN:-0}" \
            --ui-screen-proven "${UI_ACCEPT:-0}" \
            --api-ok "$api_ok" --api-2xx "$api_2xx" \
            --ui-tap-failed "${UI_TAP_FAILED:-0}" \
            --assigned "$assigned" 2>/dev/null) \
      || out="via=unknown cause=not_assigned"
    via="${out#via=}"; via="${via%% *}"
    cause=""
    case "$out" in *"cause="*) cause="${out#*cause=}" ;; esac
    ACCEPT_VIA="$via"
    case "$cause" in none|"") ACCEPT_CAUSE="" ;; *) ACCEPT_CAUSE="$cause" ;; esac
  fi
  set_accept_via "$ACCEPT_VIA"
  log "accept_via=$ACCEPT_VIA cause=${ACCEPT_CAUSE:-none}"
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
# Repli d'acceptation : accept.json est la seule source de ok/via (ISC-3). Posé en
# helper pour que le mark FAIL du point reste ADJACENT à l'appel de repli
# (contrat vérifié par test_markers.py), sans être émis avant un succès possible.
api_accept_ok() {
  "$PY" -c 'import json,sys;sys.exit(0 if json.load(open(sys.argv[1])).get("ok") else 1)' \
       "$OUT/accept.json" 2>/dev/null || return 1
  ACCEPT_OK=1
  set_accept_via "$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("via") or "unknown")' "$OUT/accept.json")"
}
# ui_action exige le dialogue de confirmation ; try_tap ne l'exige pas (navigation).
try_confirm() { local pkg="$1"; shift; local l; for l in "$@"; do ui_action "$pkg" "$l" && return 0; done; return 1; }
try_tap() { local pkg="$1"; shift; tap_when "$pkg" "$@"; }
ride_state() {
  STEP=ride_state "$PY" e2e/api_flow.py > "$OUT/last-ride-state.txt" 2>"$OUT/api-flow.err"
  smoke_emit_file "$OUT/api-flow.err"
  log "ride_state: $(tr '\n' ' ' < "$OUT/last-ride-state.txt")"
}

# launch pkg : démarre le composant launcher RÉSOLU, en gardant monkey en repli.
# `monkey` choisit parfois une activité secondaire (et ne prouve pas le composant) :
# resolve-activity -> `am start -W -n pkg/pkg.Activity` rend le lancement explicite.
launch() {
  local pkg="$1" comp="" out=""
  out=$(adb shell cmd package resolve-activity --brief \
        -c android.intent.category.LAUNCHER "$pkg" 2>/dev/null) || out=""
  comp=$(printf '%s\n' "$out" | grep -F '/' | tail -1 | tr -d '\r' \
         | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
  if printf '%s' "$comp" | grep -Eq '^[A-Za-z0-9_.]+/[A-Za-z0-9_.$]+$'; then
    adb shell am start -W -n "$comp" >/dev/null 2>&1
    return 0
  fi
  log "launch: composant launcher introuvable pour $pkg — repli monkey"
  adb shell monkey -p "$pkg" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1
}
# wait_fg vit dans ui.sh (attente ACTIVE, défaut WAIT_FG_SEC=600) : un cold start
# de debug Flutter dépasse largement 60 s, et un `wait_ui` rendrait rc=3 sur le
# launcher avant la première frame.
# Bascule explicite entre étapes (jamais depuis un wait).
# Un overlay système (dialogue Réglages) est refermé AVANT la bascule : sinon le
# monkey lance l'app derrière un dialogue qui garde le focus.
bring_fg() {
  close_system_overlay || true
  [ "$(fg_pkg)" = "$1" ] && return 0
  log "bring_fg $(fg_pkg) -> $1"
  adb shell monkey -p "$1" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1
  wait_fg "$1" 30
}

# grant_app_permissions pkg : accorde en amont ce qu'`adb install -g` ne couvre pas.
# Run 40375590679 : le dashboard Driver appelle requestIgnoreBatteryOptimization() ;
# sans exemption batterie (`isIgnoringBatteryOptimizations == false`), Android ouvre
# le dialogue d'exemption (activité de com.android.settings), qui vole le premier
# plan ~60 s et empêche de prouver l'accueil « En ligne ». L'exemption est donc
# posée AVANT le premier lancement, puis RELUE (`user,pkg,uid`), jamais supposée.
grant_app_permissions() {
  local pkg="$1" perm whitelist
  for perm in POST_NOTIFICATIONS ACCESS_FINE_LOCATION ACCESS_COARSE_LOCATION \
              ACCESS_BACKGROUND_LOCATION; do
    adb shell pm grant "$pkg" "android.permission.$perm" >/dev/null 2>&1 \
      || log "grant_app_permissions: pm grant $perm refusé pour $pkg"
  done
  adb shell appops set "$pkg" RUN_IN_BACKGROUND allow >/dev/null 2>&1 \
    || log "grant_app_permissions: appops RUN_IN_BACKGROUND refusé pour $pkg"
  adb shell appops set "$pkg" RUN_ANY_IN_BACKGROUND allow >/dev/null 2>&1 \
    || log "grant_app_permissions: appops RUN_ANY_IN_BACKGROUND refusé pour $pkg"
  adb shell cmd deviceidle whitelist "+$pkg" >/dev/null 2>&1 \
    || log "grant_app_permissions: deviceidle whitelist +$pkg refusé"
  whitelist=$(adb shell cmd deviceidle whitelist 2>/dev/null) || whitelist=""
  # Les deux formes réelles : `user,<pkg>,<uid>` et `<pkg>,<uid>` en tête de ligne.
  # Lecture EXACTE (-F) : un point du nom de paquet n'est jamais un joker ERE, donc
  # `user,foo.<pkg>,<uid>` et `user,<pkg>2,<uid>` ne matchent jamais. Le préfixe `@`
  # ancre la seconde forme au début de ligne sans faire du paquet une regex.
  if printf '%s\n' "$whitelist" | sed 's/^/@/' \
       | grep -Fq -e ",$pkg," -e "@$pkg,"; then
    log "battery exemption: $pkg (ok)"
  else
    log "battery exemption: $pkg (READBACK FAILED)"
  fi
}

notif_probe() { # pkg token -> json
  adb shell dumpsys notification --noredact 2>/dev/null \
    | "$PY" e2e/notif.py "$1" "${2:-}" 2>/dev/null \
    || echo '{"records_total":0,"records_pkg":0,"records_token":0}'
}
json_field() { "$PY" -c 'import json,sys;print(json.load(sys.stdin).get(sys.argv[1],0))' "$1"; }

# publish_artemis_log name : tail borné (200 lignes), secrets masqués, vers run.log
# ET le smoke artifact. Un journal brut peut porter la clé du provider : rien ne sort
# d'ici sans passer par sanitize.py --redact-env.
# Une SEULE passe groupée : le masquage coûte un processus python par run, pas un par
# ligne (200 lignes × 6 runs ≈ 1200 processus). Les deux destinations reçoivent le
# même tampon, donc exactement les mêmes lignes masquées.
publish_artemis_log() {
  local name="$1" src="$OUT/artemis/$1.log" masked
  [ -f "$src" ] || return 0
  mkdir -p "$OUT" "$(dirname "$SMOKE_LOG")" 2>/dev/null || true
  masked=$(mktemp "${TMPDIR:-/tmp}/artemis-log.XXXXXX") || return 0
  # Le provider peut être OpenRouter-like (OPENAI_*) ou FreeLLMAPI (FREELLM_*) :
  # les deux clés et les deux bases sont masquées. sanitize.py ignore une variable
  # absente ou vide, donc l'option reste sûre quand un seul backend est configuré.
  if ! tail -n 200 "$src" 2>/dev/null \
       | sed 's/^/artemis-log: /' \
       | "$PY" e2e/sanitize.py --redact-env OPENAI_API_KEY \
             --redact-env FREELLM_API_KEY --redact-env OPENAI_BASE_URL \
             --redact-env FREELLM_BASE_URL > "$masked"; then
    rm -f "$masked"
    return 0
  fi
  cat "$masked" >> "$OUT/run.log" 2>/dev/null || true
  cat "$masked" >> "$SMOKE_LOG" 2>/dev/null || true
  rm -f "$masked"
}

# masked_tail path [lines] : queue bornée d'UN essai, prête à publier.
# Deux fuites à tuer avant publication : le blob base64 (une capture complète dans
# un journal) et les chemins absolus du runner. Les secrets passent par sanitize.py,
# comme publish_artemis_log, pour une seule définition du masquage des clés.
masked_tail() {
  local src="$1" lines="${2:-40}"
  [ -f "$src" ] || return 0
  tail -n "$lines" "$src" 2>/dev/null \
    | sed -E 's#data:image/[a-z]+;base64,[A-Za-z0-9+/=]+#[B64]#g; s#[A-Za-z0-9+/=]{120,}#[B64]#g; s#(/[A-Za-z0-9._-]+){2,}#[PATH]#g' \
    | "$PY" e2e/sanitize.py --redact-env OPENAI_API_KEY \
          --redact-env FREELLM_API_KEY --redact-env OPENAI_BASE_URL \
          --redact-env FREELLM_BASE_URL 2>/dev/null
}

# Série épinglée : Artemis doit viser l'émulateur DU JOB, pas un device arbitraire
# (un runner peut en exposer plusieurs). Résolution PARESSEUSE : au chargement du
# script l'émulateur peut ne pas être encore visible, et un `adb get-serialno` trop
# tôt désactiverait Artemis pour tout le run. Une série invalide, elle, désactive
# Artemis sans casser le harnais : taps + API restent la voie de secours.
ARTEMIS_SERIAL="${ARTEMIS_SERIAL:-${ANDROID_SERIAL:-}}"
# Max m1: every disable site names ITS cause, so the skip line never claims
# "smoke failed" for a relay that was down or a serial that was invalid.
ARTEMIS_SKIP_CAUSE="${ARTEMIS_SKIP_CAUSE:-smoke failed}"
case "$ARTEMIS_SERIAL" in
  ""|unknown) ;;  # résolue (ou refusée) au premier artemis_run
  *[!A-Za-z0-9._:-]*)
    log "artemis: série invalide ('$ARTEMIS_SERIAL') — smoke désactivé, taps + API"
    ARTEMIS_SERIAL=""
    ARTEMIS_OK=0
    ARTEMIS_SKIP_CAUSE="série invalide" ;;
esac
ARTEMIS_OK="${ARTEMIS_OK:-1}"
artemis_serial() {
  local serial="${ARTEMIS_SERIAL:-}"
  [ -n "$serial" ] || serial=$(adb get-serialno 2>/dev/null | tr -d '\r\n')
  case "$serial" in
    ""|unknown|*[!A-Za-z0-9._:-]*) return 1 ;;
  esac
  ARTEMIS_SERIAL="$serial"
  printf '%s' "$serial"
}
artemis_run() {
  # name goal timeout_s [locked_pkg]
  local name="$1" goal="$2" tmo="${3:-240}" pkg="${4:-}"
  if [ "$ARTEMIS_OK" != "1" ]; then
    log "artemis skip: $name (${ARTEMIS_SKIP_CAUSE:-smoke failed})"
    return 2
  fi
  if ! artemis_serial >/dev/null; then
    log "artemis: aucune série exploitable — smoke désactivé, taps + API"
    ARTEMIS_OK=0
    ARTEMIS_SKIP_CAUSE="série introuvable"
    return 2
  fi
  # Chaîne de repli ordonnée. Absente ou à UN modèle => un seul essai, comportement
  # historique (les tests unitaires du harnais reposent sur cet argv unique).
  local chain_file="${ARTEMIS_CHAIN_FILE:-}"
  local -a chain=()
  if [ -n "$chain_file" ] && [ -s "$chain_file" ]; then
    local line
    while IFS= read -r line; do
      [ -n "${line//[[:space:]]/}" ] && chain+=("$line")
    done < "$chain_file"
  fi
  local chain_n="${#chain[@]}"
  local max_attempts=1
  if [ "$chain_n" -gt 1 ]; then
    max_attempts="${ARTEMIS_MAX_ATTEMPTS:-$chain_n}"
    case "$max_attempts" in ''|*[!0-9]*) max_attempts="$chain_n" ;; esac
    [ "$max_attempts" -lt 1 ] && max_attempts=1
    [ "$max_attempts" -gt "$chain_n" ] && max_attempts="$chain_n"
  fi
  # Un token non numérique ("abc", "-2") ferait planter `[ "$delay" -gt … ]` et
  # annoncerait un backoff bidon dans le log : on ne garde que les entiers.
  local -a backoff=()
  if [ -n "${ARTEMIS_BACKOFF:-}" ]; then
    local -a raw_backoff=()
    read -r -a raw_backoff <<< "${ARTEMIS_BACKOFF}"
    local token
    if [ "${#raw_backoff[@]}" -gt 0 ]; then
      for token in "${raw_backoff[@]}"; do
        case "$token" in ''|*[!0-9]*) ;; *) backoff+=("$token") ;; esac
      done
    fi
  fi
  [ "${#backoff[@]}" -gt 0 ] || backoff=(5 15 30)
  # Deux essais 502 consécutifs suffisent à couper la chaîne (R3).
  local relay_streak_max="${RELAY_502_STREAK_MAX:-2}"
  case "$relay_streak_max" in ''|*[!0-9]*) relay_streak_max=2 ;; esac
  [ "$relay_streak_max" -lt 1 ] && relay_streak_max=1

  # B1: Artemis tourne depuis vendor/artemis; un chemin relatif y serait résolu.
  local out_abs="${OUT_ABS:-}"
  if [ -z "$out_abs" ]; then
    out_abs="$OUT"
    case "$out_abs" in /*) ;; *) out_abs="$(pwd)/$out_abs" ;; esac
  fi

  # B2: la piste d'audit décrit le DERNIER essai, pas la chaîne demandée.
  # primary = modèle de cet essai, fallback = son repli Artemis (niveau unique),
  # fallback_events = marqueurs "Falling back"/llm_fallback trouvés dans SON journal.
  local attempt=1 rc=0 attempts_run=0 signals="" relay_streak=0
  local primary="none" fallback="none" fallback_events=0 served="unknown"
  local last_primary="none" last_fallback="none" last_events=0
  local deadline=0   # posé au premier appel Artemis: la config ne mange pas le budget
  while :; do
    local model="default" next_model="-"
    if [ "$chain_n" -gt 0 ]; then
      model="${chain[$((attempt - 1))]}"
      # apply(offset=attempt-1) donne au nœud de tête la paire
      # (chain[attempt-1], chain[attempt % n]).
      [ "$chain_n" -gt 1 ] && next_model="${chain[$((attempt % chain_n))]}"
    fi
    local offset=$((attempt - 1))

    # Budget PAR TENTATIVE (run 40205373538) : le premier essai recevait TOUT le
    # step, une route pendue consommait la fenêtre et le repli API démarrait après
    # expiration. Part = min(cap, restant / essais restants), dernier essai = restant.
    if [ "$attempt" -eq 1 ]; then
      deadline=$((SECONDS + tmo))
    fi
    local remaining=$((deadline - SECONDS))
    [ "$remaining" -ge 0 ] || remaining=0
    local attempts_left=$((max_attempts - attempt + 1))
    local cap="${ARTEMIS_MODEL_TIMEOUT_SEC:-45}"
    # Contrat A (driver-accept): diviser la fenêtre par TOUTES les routes restantes
    # donnait 22/6 = 3 s par modèle, sous le plancher de 5 s — run.sh refusait alors
    # toute tentative et Artemis n'avait jamais sa chance dans l'offre. On réduit le
    # NOMBRE de tentatives, jamais le plancher : 22 s => 4 routes à 5 s.
    local affordable=""
    affordable=$("$PY" e2e/model_chain.py attempts-affordable --remaining "$remaining" \
                   --left "$attempts_left" --min 5 2>/dev/null) || affordable=""
    case "$affordable" in ''|*[!0-9]*) affordable="$attempts_left" ;; esac
    attempts_left="$affordable"
    local tmo_left
    tmo_left=$("$PY" e2e/model_chain.py attempt-timeout --remaining "$remaining" \
                 --left "$attempts_left" --cap "$cap" 2>/dev/null) || tmo_left="$remaining"
    case "$tmo_left" in ''|*[!0-9]*) tmo_left="$remaining" ;; esac
    # Un essai de moins de 5 s ne peut rien produire : mieux vaut rendre la main
    # tout de suite au repli taps + API qu'annoncer une tentative bidon.
    if [ "$tmo_left" -lt 5 ]; then
      log "artemis retry: $name budget épuisé (remaining=${remaining}s part=${tmo_left}s cap=${cap}s affordable=${attempts_left}) — aucune tentative"
      # Aucun essai n'a tourné : rc INCONNU ("-"), jamais 124 qui se lit comme un
      # timeout observé. `budget` est le signal, et la raison de l'arrêt.
      printf '%s|%s|%s|%s|%s|%s|%s\n' "$name" "$attempt" "-" "-" "0" "budget" "budget" \
        >> "$OUT/artemis/attempts.txt"
      # Aucun essai n'a tourné : rc=0 annoncerait un succès sans preuve.
      [ "$attempts_run" -gt 0 ] || rc=2
      break
    fi

    # Config runtime régénérée AVANT chaque essai : offset=0 -> tête de chaîne,
    # offset=1 -> route suivante. vendor/ n'est que lu ($ARTEMIS_CONFIG).
    # Run 40602473815 : le relais FreeLLMAPI a répondu 200 puis 502 sur la capture
    # ~300 Ko, et le SDK a consommé toute la fenêtre avec son timeout par défaut de
    # 60 s — `ModelEndpoint` n'expose aucun `max_retries` (router.py:87-113). Le seul
    # levier est le timeout PAR REQUÊTE : il est calculé pour que (1 + réessais SDK)
    # appels + marge tiennent DANS la part de cet essai.
    # ISC-5 : le plancher est PAR MODÈLE. Chaque route vivante mesurée par le
    # préflight a SA ligne `model=<id> floor=<ceil_s>` dans vision-floor.txt ;
    # l'ancien MAX sur toute la chaîne refusait TOUTES les tentatives dès qu'une
    # route lente y figurait (chaîne [gpt-4.1 2 s, gpt-4o 15 s] => plancher 15 =>
    # la route rapide était refusée dès l'essai 1). Un modèle sans mesure (ou
    # aucune mesure du tout) => 0 => argv historique, comportement inchangé.
    local lat_floor=0
    local floor_map="$OUT/artemis/vision-floor.txt"
    if [ "$chain_n" -gt 0 ] && [ -s "$floor_map" ]; then
      lat_floor=$(awk -v want="$model" '
        split($1, a, "=") == 2 && a[1] == "model" && a[2] == want {
          split($2, b, "=")
          if (b[1] == "floor" && b[2] ~ /^[0-9]+$/) { print b[2]; exit }
        }
      ' "$floor_map" 2>/dev/null) || lat_floor=0
    fi
    case "$lat_floor" in ''|*[!0-9]*) lat_floor=0 ;; esac
    local -a rt_floor_args=()
    [ "$lat_floor" -gt 0 ] && rt_floor_args=(--latency-floor "$lat_floor")
    # ISC-4 : le code de sortie distingue les DEUX causes de T=0. rc=4 = refus
    # de plancher ; rc=0 + T=0 = cas historique (part qui ne finance aucun appel
    # borné, le timeout extérieur gouverne). Les confondre étiquetait un cas
    # legacy en `latency_floor` (audit R5).
    local sdk_retries="${RELAY_SDK_RETRIES:-2}"
    case "$sdk_retries" in ''|*[!0-9]*) sdk_retries=2 ;; esac
    local req_timeout="" req_rc=0 rt_err="$OUT/artemis/request-timeout.err"
    req_timeout=$("$PY" e2e/model_chain.py request-timeout --share "$tmo_left" \
                    --sdk-retries "$sdk_retries" \
                    ${rt_floor_args[@]+"${rt_floor_args[@]}"} 2> "$rt_err") \
      || req_rc=$?
    [ -s "$rt_err" ] && cat "$rt_err" >> "$OUT/run.log"
    if [ "$req_rc" -eq 4 ]; then
      # La VRAIE comparaison est borne finançable vs plancher — jamais
      # "part < latence mesurée", qui était arithmétiquement faux. La borne est
      # lue dans le message du CLI (`bound=<n>s`), même formule, rien d'inventé.
      local funded_bound=""
      funded_bound=$(sed -n 's/.*bound=\([0-9][0-9]*\)s.*/\1/p' "$rt_err" 2>/dev/null \
                       | tail -n 1)
      case "$funded_bound" in ''|*[!0-9]*) funded_bound=0 ;; esac
      log "artemis timeout: $name timeout par requête ${funded_bound}s <= plancher ${lat_floor}s — AUCUN essai sur cette route (part=${tmo_left}s, sdk_retries=$sdk_retries, route trop lente pour le budget par requête)"
      # Le champ modèle (3e colonne du ledger) porte LA ROUTE REFUSÉE : elle est
      # connue ici, et un refus anonyme ne dit pas quelle route était trop lente.
      printf '%s|%s|%s|%s|%s|%s|%s\n' "$name" "$attempt" "$model" "-" "$tmo_left" \
        "latency_floor" "latency_floor" >> "$OUT/artemis/attempts.txt"
      # Comme `budget` : aucun essai n'a tourné sur CETTE tentative => rc=2 si
      # aucun essai n'a tourné du tout ; sinon le rc de l'essai précédent reste.
      [ "$attempts_run" -gt 0 ] || rc=2
      # Le plancher est PAR ROUTE : un refus sur la route lente ne tue pas la
      # chaîne, la rotation passe à la suivante dont le plancher peut financer
      # l'appel (l'ancien arrêt inconditionnel laissait la route rapide intacte).
      # Le refus n'entre pas dans attempts_run (aucun essai lancé) ; la borne
      # max_attempts ferme la boucle, jamais un refus.
      if [ "$attempt" -ge "$max_attempts" ]; then
        log "artemis end: $name dernier essai $attempt/$max_attempts refusé (plancher ${lat_floor}s) — arrêt de la chaîne"
        break
      fi
      local next_floor_model="${chain[$attempt]}"
      log "artemis retry: $name switch from=$model to=${next_floor_model:-next} cause=latency_floor (attempt $((attempt + 1))/$max_attempts, aucun essai lancé)"
      attempt=$((attempt + 1))
      continue
    fi
    # Tout autre rc : échec de calcul => repli historique (T=0 => défaut 5).
    [ "$req_rc" -eq 0 ] || req_timeout=""
    case "$req_timeout" in ''|*[!0-9]*) req_timeout=5 ;; esac
    # R2 : une part trop petite pour financer un seul appel borné (T=0) n'écrit
    # AUCUNE clé `timeout` — le timeout extérieur de l'essai gouverne. La rotation
    # de chaîne reste faite, sinon l'essai partirait sur le mauvais modèle.
    local -a rt_args=()
    local rt_label="none"
    if [ "$req_timeout" -gt 0 ]; then
      rt_args=(--request-timeout "$req_timeout" --sdk-retries "$sdk_retries")
      rt_label="${req_timeout}s"
    else
      log "artemis timeout: $name part=${tmo_left}s ne finance aucun appel borné (sdk_retries=$sdk_retries) — pas de timeout vendor, le timeout extérieur gouverne"
    fi
    if [ "$chain_n" -gt 0 ] && [ -n "${ARTEMIS_CONFIG:-}" ] \
       && [ -n "${ARTEMIS_CONFIG_RUNTIME:-}" ] && [ -n "$chain_file" ]; then
      if "$PY" e2e/model_chain.py apply --in "$ARTEMIS_CONFIG" \
           --out "$ARTEMIS_CONFIG_RUNTIME" --chain-file "$chain_file" \
           --offset "$offset" ${rt_args[@]+"${rt_args[@]}"} >> "$OUT/run.log" 2>&1; then
        log "artemis config: $name offset=$offset primary=$model fallback=$next_model runtime=$ARTEMIS_CONFIG_RUNTIME request_timeout=$rt_label sdk_retries=$sdk_retries"
      else
        # Pas de config à l'offset demandé => AUCUNE tentative : lancer Artemis sur
        # l'ancienne config servirait silencieusement le mauvais modèle.
        log "artemis config: $name régénération refusée (offset=$offset) — étape abandonnée rc=2"
        rc=2
        break
      fi
    fi

    local attempt_log="$OUT/artemis/$name.attempt$attempt.log"
    local -a args=(run "$goal" --profile flash --test-name "$name"
                   --traces-path "$out_abs/artemis"
                   --standalone --device-serial "$ARTEMIS_SERIAL")
    [ -n "$pkg" ] && args+=(--locked-app "$pkg")
    log "artemis start: $name attempt $attempt/$max_attempts primary=$model fallback=$next_model offset=$offset timeout=${tmo_left}s affordable=${attempts_left} device=$ARTEMIS_SERIAL"
    # F1 : `attempts_run` compte les LANCEMENTS réels, jamais l'indice de créneau.
    # Publier $attempt annonçait 2/3 essais après un refus de plancher alors qu'un
    # seul Artemis avait démarré. Incrémenté AVANT le lancement : l'invocation a
    # bien eu lieu même si le binaire échoue.
    attempts_run=$((attempts_run + 1))
    ( cd vendor/artemis && timeout "$tmo_left" "./.venv/bin/artemis" "${args[@]}" ) \
      > "$attempt_log" 2>&1
    rc=$?
    # Chaque essai garde SON journal (audit) ; $name.log reste la DERNIÈRE tentative,
    # c'est elle que publish_artemis_log publie.
    cp "$attempt_log" "$OUT/artemis/$name.log" 2>/dev/null || true
    local events=0
    events=$(grep -E -c 'Falling back|llm_fallback' "$attempt_log" 2>/dev/null) || events=0
    last_primary="$model"
    last_fallback="$next_model"
    last_events="$events"
    log "artemis llm: $name attempt=$attempt/$max_attempts primary=$model fallback=$next_model fallback_events=$events"
    log "artemis end: $name attempt $attempt/$max_attempts rc=$rc remaining=$((deadline - SECONDS))s"
    # Relais 502 (run 40602473815) : « 200 puis 502 » ressemble à un modèle mort si
    # personne ne compte. Chaque essai publie SES compteurs et SA queue masquée.
    local count502=0 retrying=0 tail_rel="artemis/$name.attempt$attempt.tail.txt"
    count502=$(grep -c -E '\b502\b' "$attempt_log" 2>/dev/null) || count502=0
    retrying=$(grep -c 'Retrying request' "$attempt_log" 2>/dev/null) || retrying=0
    masked_tail "$attempt_log" 40 > "$OUT/$tail_rel" 2>/dev/null || true
    printf '%s|%s|%s|%s|%s\n' "$name" "$attempt" "$count502" "$retrying" "$tail_rel" \
      >> "$OUT/artemis/relay-attempts.txt"
    log "artemis relay: $name attempt=$attempt 502=$count502 retrying=$retrying tail=$tail_rel"

    # Journal PAR TENTATIVE (run 40205373538) : qui a tourné, avec quel budget, et
    # comment ça s'est terminé. Le rapport ne devine plus rien à partir du seul rc.
    local attempt_signal="" outcome="terminal"
    attempt_signal=$("$PY" e2e/model_chain.py classify --rc "$rc" \
                       --log "$attempt_log" 2>/dev/null) || attempt_signal=""
    if [ "$rc" -eq 0 ]; then
      outcome="success"
    elif [ "$rc" -eq 124 ]; then
      outcome="timeout"; [ -n "$attempt_signal" ] || attempt_signal="timeout"
    elif [ -n "$attempt_signal" ]; then
      outcome="retryable"
    elif [ "$rc" -ge 128 ]; then
      outcome="signal-id"
    fi
    printf '%s|%s|%s|%s|%s|%s|%s\n' "$name" "$attempt" "$model" "$rc" "$tmo_left" \
      "${attempt_signal:--}" "$outcome" >> "$OUT/artemis/attempts.txt"

    # Relais 502 (audit R3) : deux essais consécutifs portant une signature 502
    # suffisent. La policy PROVIDER_UNAVAILABLE du vendor (reliability.py:154-158,
    # gelée et inatteignable depuis la config) dort 5+10+20 s entre SES réessais :
    # sur la part de ~33 s observée, ce sleep seul produit rc=124. Un 3e essai ne
    # peut pas répondre autrement — on coupe et on écrit un verdict, jamais un PASS.
    if [ "$count502" -gt 0 ] || [ "$retrying" -gt 0 ]; then
      relay_streak=$((relay_streak + 1))
    else
      relay_streak=0
    fi
    if [ "$relay_streak" -ge "$relay_streak_max" ] && [ "$rc" -ne 0 ] \
       && [ "$attempt" -lt "$max_attempts" ]; then
      [ -n "$attempt_signal" ] && signals="${signals:+$signals,}$attempt_signal"
      log "artemis chainoff: $name relais 502 sur $relay_streak essais consécutifs — chaîne coupée après $attempt/$max_attempts essais (rc=$rc, aucun PASS)"
      printf '%s|%s|%s|%s|%s\n' "$name" "$attempt" "$max_attempts" "$rc" "relay_502" \
        >> "$OUT/artemis/relay-chainoff.txt"
      break
    fi

    if [ "$rc" -eq 0 ]; then break; fi
    if [ "$attempt" -ge "$max_attempts" ]; then
      log "artemis end: $name dernier essai $attempt/$max_attempts rc=$rc — arrêt"
      break
    fi
    # 429 / rate-limit / routing_exhausted / 5xx / timeout => route suivante.
    # 400 / 401 / autre échec propre => pas de bascule (rejouer ne changerait rien).
    local signal="$attempt_signal"
    if [ -z "$signal" ]; then
      log "artemis end: $name échec non rejouable (rc=$rc) — pas de bascule"
      break
    fi
    local next_model_retry="${chain[$attempt]}"
    signals="${signals:+$signals,}$signal"
    local delay="${backoff[$((attempt - 1))]:-30}"
    # Un timeout n'est pas un quota : attendre ne rend pas la route plus rapide et
    # brûle la fenêtre. 429/5xx gardent leur backoff.
    [ "$signal" = "timeout" ] && delay=0
    local remaining=$((deadline - SECONDS))
    [ "$delay" -gt "$remaining" ] && delay="$remaining"
    [ "$delay" -lt 0 ] && delay=0
    log "artemis retry: $name switch from=$model to=$next_model_retry signal=$signal backoff=${delay}s (attempt $((attempt + 1))/$max_attempts)"
    [ "$delay" -gt 0 ] && sleep "$delay"
    attempt=$((attempt + 1))
  done

  # Piste d'audit HONNÊTE : quel modèle a RÉELLEMENT servi, combien d'essais,
  # quels signaux. rc!=0 => served=unknown (jamais prétendre qu'un modèle a servi).
  local signals_out="${signals:--}"
  if [ "$attempts_run" -gt 0 ]; then
    primary="$last_primary"
    fallback="$last_fallback"
    fallback_events="$last_events"
    if [ "$rc" -eq 0 ]; then
      if [ "$fallback_events" -gt 0 ]; then served="fallback"; else served="primary"; fi
    fi
  fi
  # served_model = le modèle qui a RÉELLEMENT servi, jamais celui qu'on espérait.
  # rc != 0 => unknown (aucune réponse exploitable) ; sinon primary, ou fallback
  # quand Artemis a lui-même basculé (fallback_events > 0).
  local served_model="unknown"
  if [ "$rc" -eq 0 ] && [ "$attempts_run" -gt 0 ]; then
    if [ "$fallback_events" -gt 0 ]; then served_model="$fallback"; else served_model="$primary"; fi
  fi
  local chain_csv="default"
  if [ "$chain_n" -gt 0 ]; then chain_csv=$(IFS=,; printf '%s' "${chain[*]}"); fi
  log "artemis models: $name primary=$primary fallback=$fallback fallback_events=$fallback_events served=$served attempts=$attempts_run/$max_attempts signals=$signals_out rc=$rc served_model=$served_model chain=$chain_csv"
  printf '%s|%s|%s|head=%s|primary=%s|fallback=%s|fallback_events=%s|served=%s|signals=%s|rc=%s|served_model=%s|chain=%s\n' \
    "$name" "$attempts_run" "$max_attempts" "${chain[0]:-default}" \
    "$primary" "$fallback" "$fallback_events" "$served" "$signals_out" "$rc" \
    "$served_model" "$chain_csv" \
    >> "$OUT/artemis/model-usage.txt"
  # Publié APRÈS CHAQUE exécution : le smoke réussi est précisément le journal qu'on
  # veut voir dans run.log et dans l'artefact e2e/smoke.log. Un échec publie le même
  # tampon masqué, puis sa capture d'écran (inchangée).
  publish_artemis_log "$name"
  if [ "$rc" -ne 0 ]; then
    log "artemis fallback: $name indisponible ($( [ "$rc" = 124 ] && echo timeout || echo "rc=$rc" )) — reprise par taps + API"
    adb exec-out screencap -p > "$OUT/screens/artemis-$name-fail.png" 2>/dev/null || true
  fi
  return $rc
}

# --- fenêtre d'offre (run 40205373538) ---------------------------------------
# OFFER_TIMEOUT_SEC est un MIROIR LECTURE SEULE de la config backend (défaut 30 s):
# on ne l'exporte jamais pour le run, on ne l'injecte jamais via adb/setprop — le
# harnais doit survivre à la valeur réelle du staging, pas la fabriquer.
OFFER_TIMEOUT_SEC="${OFFER_TIMEOUT_SEC:-30}"
OFFER_SAFETY_SEC="${OFFER_SAFETY_SEC:-8}"
OFFER_CAPTURE_SEC="${OFFER_CAPTURE_SEC:-7}"
OFFER_T0=0   # posé au dispatch de l'offre, pas au démarrage du step
OFFER_T0_SOURCE=step_start
OFFER_STEP_START=0
# ISC-5: budget d'Artemis et du tap = restant MOINS cette réserve, gardée intacte
# pour le repli API. Sans elle, Artemis consomme `left` en entier et le repli
# d'acceptation démarre à/après l'expiration de l'offre.
API_RESERVE_SEC="${API_RESERVE_SEC:-4}"
OFFER_BUDGET=$((OFFER_TIMEOUT_SEC - OFFER_SAFETY_SEC))
# Secondes d'offre restantes (0 au pire, jamais négatif) : borne l'attente du popup
# et le repli API PENDANT la fenêtre, au lieu d'un timeout fixe de 45 s.
offer_left() {
  local left=$((OFFER_BUDGET - (SECONDS - OFFER_T0)))
  [ "$left" -gt 0 ] || left=0
  echo "$left"
}
# ISC-5: T0 = création réelle de la course, pas le retour de `api pick` (optimiste
# de plusieurs secondes). Sortie: "<t0> <source>" où t0 vit dans l'horloge $SECONDS.
# created_at accepté en epoch s, epoch ms ou ISO-8601; absent/illisible => repère
# local pris AVANT le pick (`step_start`), donc sous-estimation, jamais sur-estimation.
offer_t0() {
  local state="${1:-$OUT/state.json}" step_start="${2:-$SECONDS}"
  "$PY" - "$state" "$step_start" "$SECONDS" <<'PY' 2>/dev/null || printf '%s step_start\n' "$step_start"
import datetime, json, sys, time
path, step_start, now = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
try:
    with open(path, encoding="utf-8") as fh:
        st = json.load(fh)
except Exception:
    st = {}
raw = ((st.get("ride_picked") or {}).get("created_at")
       or (st.get("ride_create") or {}).get("created_at"))
epoch = None
if raw not in (None, ""):
    try:
        if isinstance(raw, (int, float)) or str(raw).strip().isdigit():
            epoch = float(raw)
            if epoch > 1e12:          # millisecondes
                epoch /= 1000.0
        else:
            epoch = datetime.datetime.fromisoformat(
                str(raw).strip().replace("Z", "+00:00")).timestamp()
    except Exception:
        epoch = None
age = None if epoch is None else int(round(time.time() - epoch))
if age is None or age < 0:            # absent, illisible ou horloge incohérente
    print(step_start, "step_start")
else:
    print(max(0, now - age), "created_at")
PY
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
launch "$RIDER_PKG"; wait_fg "$RIDER_PKG" "$WAIT_FG_SEC" || true
cap rider-home-loaded 14-rider-home-loaded "$RIDER_PKG" 90
# Démarrage Driver PROUVÉ (run 40375590679) : permissions + exemption batterie
# AVANT le premier lancement (sinon le dialogue Réglages ouvre et vole le focus),
# puis `launch_fg` (am start -W sur le composant résolu, 0 seulement si le Driver
# est devant), puis `wait_app_ui` qui referme les overlays et relance l'app si elle
# perd le premier plan. Le marqueur et le verdict PASS/WARN restent inchangés.
grant_app_permissions "$DRIVER_PKG"
launch_fg "$DRIVER_PKG" "$WAIT_FG_SEC" \
  || log "launch_fg: $DRIVER_PKG jamais prouvé au premier plan — capture 33 tentée quand même"
wait_app_ui "$DRIVER_PKG" "${DRIVER_ONLINE_WAIT_SEC:-120}" "$MARK_driver_home_online" \
  || log "wait_app_ui: accueil Driver (« En ligne ») non prouvé en ${DRIVER_ONLINE_WAIT_SEC:-120}s"
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

# Préflight vision: chaîne ORDONNÉE (override workflow en TÊTE), chaque modèle sonde
# une image, seuls les 200 survivent. L'ancien heredoc s'arrêtait au PREMIER 200 :
# une route rate-limitée en cours de run tuait toutes les étapes UI suivantes.
CHAIN_REQUESTED="$OUT/artemis/vision-chain-requested.txt"
chain_args=(chain --base "$ARTEMIS_BASE_CHAIN")
if [ -n "${ARTEMIS_DEFAULT_MODEL:-}" ]; then
  chain_args+=(--override "$ARTEMIS_DEFAULT_MODEL")
fi
if ! "$PY" e2e/model_chain.py "${chain_args[@]}" > "$CHAIN_REQUESTED" \
     2> "$OUT/artemis/vision-chain.err"; then
  log "llm-vision: override refusé — chaîne de base seule (voir artemis/vision-chain.err)"
  if ! "$PY" e2e/model_chain.py chain --base "$ARTEMIS_BASE_CHAIN" > "$CHAIN_REQUESTED" \
       2>> "$OUT/artemis/vision-chain.err"; then
    : > "$CHAIN_REQUESTED"
    log "llm-vision: chaîne de base invalide (voir artemis/vision-chain.err)"
  fi
fi
: > "$ARTEMIS_CHAIN_FILE"
# ISC-5 : le plancher est PAR MODÈLE, jamais un MAX sur la chaîne. Le préflight
# mesure chaque route vivante ; on écrit une ligne par route mesurée dans
# `$OUT/artemis/vision-floor.txt` :
#     model=<id> floor=<ceil_s>
# `artemis_run` lit le plancher de LA route qu'il s'apprête à lancer.
# Parsing POSITIONNEL : seuls les champs fixes de tête d'une ligne `stage=realistic`
# comptent (stage, model, sample, http, latency_ms) ; tout ce qui suit le premier
# `body=` vient du relais et est ignoré — un body contenant « http=200
# latency_ms=99000 » ne doit jamais devenir un plancher.
vision_floor_map() {
  local log="${1:-}" chain="${2:-}" out="${3:-}"
  if [ -z "$out" ]; then
    printf '0'
    return 0
  fi
  : > "$out"
  if [ ! -s "$log" ] || [ ! -s "$chain" ]; then
    printf '0'
    return 0
  fi
  awk '
    NR == FNR {
      if (length($0) > 0) { order[++n] = $0; alive[$0] = 1 }
      next
    }
    $1 == "stage=realistic" {
      split($2, f, "="); if (f[1] != "model") next
      model = f[2]
      split($3, f, "="); if (f[1] != "sample") next
      split($4, f, "="); if (f[1] != "http") next
      http = f[2]
      split($5, f, "="); if (f[1] != "latency_ms") next
      lat = f[2]
      if (http == "200" && lat ~ /^[0-9]+$/ && (model in alive) && lat + 0 > max[model])
        max[model] = lat + 0
    }
    END {
      for (i = 1; i <= n; i++) {
        m = order[i]
        if (m in max) printf "model=%s floor=%d\n", m, int((max[m] + 999) / 1000)
      }
    }
  ' "$chain" "$log" > "$out" 2>/dev/null || : > "$out"
  local count=0
  count=$(grep -c . "$out" 2>/dev/null) || count=0
  printf '%s' "$count"
}
VISION_UNREACHABLE=0
if [ -s "$CHAIN_REQUESTED" ]; then
  # VISION_PROBE_TIMEOUT borne UNE sonde : une route qui pend est morte, elle ne
  # doit pas manger le budget du step (le défaut 20 s est documenté dans le log).
  VISION_PROBE_TIMEOUT="${VISION_PROBE_TIMEOUT:-20}"
  # Une sonde 1x1 ne prouve que la vivacité : le relais 502 sur la VRAIE capture
  # (~300 Ko). La sonde réaliste décide donc l'usage, et une chaîne qui y échoue
  # part en chain-off avant de brûler le budget du step. 0 = ancien comportement.
  VISION_PROBE_BYTES="${VISION_PROBE_BYTES:-307200}"
  case "$VISION_PROBE_BYTES" in ''|*[!0-9]*) VISION_PROBE_BYTES=307200 ;; esac
  log "llm-vision: préflight timeout=${VISION_PROBE_TIMEOUT}s payload=${VISION_PROBE_BYTES}o par modèle ($(grep -c . "$CHAIN_REQUESTED" 2>/dev/null || echo 0) demandé(s))"
  if "$PY" e2e/vision_preflight.py --chain-file "$CHAIN_REQUESTED" \
         --out "$ARTEMIS_CHAIN_FILE" --log "$OUT/artemis/vision-preflight.txt" \
         --timeout "$VISION_PROBE_TIMEOUT" \
         --probe-bytes "$VISION_PROBE_BYTES" \
         >> "$OUT/run.log" 2>&1; then
    :
  else
    vision_rc=$?
    if [ "$vision_rc" -eq 3 ]; then
      # ISC-8 : exit 3 = relais injoignable (0 sonde réseau n'a de statut HTTP).
      # Nommer la cause et ne PAS lancer un seul essai Artemis.
      VISION_UNREACHABLE=1
      log "llm-vision: relay unreachable — toutes les sondes http=0 (voir artemis/vision-preflight.txt)"
    else
      log "llm-vision: préflight en erreur (rc=$vision_rc, voir artemis/vision-preflight.txt)"
    fi
  fi
fi
VISION_FLOOR_COUNT=$(vision_floor_map "$OUT/artemis/vision-preflight.txt" \
  "$ARTEMIS_CHAIN_FILE" "$OUT/artemis/vision-floor.txt")
case "$VISION_FLOOR_COUNT" in ''|*[!0-9]*) VISION_FLOOR_COUNT=0 ;; esac
if [ "$VISION_FLOOR_COUNT" -gt 0 ]; then
  log "llm-vision: plancher de latence par modèle ($VISION_FLOOR_COUNT route(s) mesurée(s), stage réaliste http=200) -> artemis/vision-floor.txt"
fi
if [ "$VISION_UNREACHABLE" = "1" ]; then
  mark llm-vision WARN "relay unreachable (toutes les sondes http=0) (voir artemis/vision-preflight.txt)"
  ARTEMIS_OK=0
  ARTEMIS_SKIP_CAUSE="relay unreachable"
else
  VISION_MODEL=$(head -n 1 "$ARTEMIS_CHAIN_FILE" 2>/dev/null | tr -d '\r\n')
  printf '%s' "$VISION_MODEL" > "$OUT/artemis/vision-model.txt"
  ARTEMIS_CHAIN_N=$(grep -c . "$ARTEMIS_CHAIN_FILE" 2>/dev/null || true)
  [ -n "$ARTEMIS_CHAIN_N" ] || ARTEMIS_CHAIN_N=0
  if [ -z "$VISION_MODEL" ]; then
    # Max m5: an empty alive file has two causes. When EVERY probe got an HTTP
    # status (none 200, no network failure) the relay answered and refused — that
    # is not a capability fact about the models. Only that case gets the new
    # sentence; the http=0 verdict (ISC-3/ISC-8) is untouched.
    vision_http_codes=$(awk '
      {
        for (i = 1; i <= NF; i++) {
          if ($i ~ /^body=/) break
          if ($i ~ /^http=[0-9]+$/) {
            n++
            if ($i == "http=0") z++
            if ($i == "http=200") k++
          }
        }
      }
      END { printf "%d %d %d", n + 0, z + 0, k + 0 }
    ' "$OUT/artemis/vision-preflight.txt" 2>/dev/null) || vision_http_codes="0 0 0"
    case "$vision_http_codes" in
      [0-9]*" "[0-9]*" "[0-9]*) ;;
      *) vision_http_codes="0 0 0" ;;
    esac
    read -r vision_probes vision_http0 vision_http200 <<< "$vision_http_codes"
    if [ "$vision_probes" -gt 0 ] && [ "$vision_http0" -eq 0 ] \
       && [ "$vision_http200" -eq 0 ]; then
      mark llm-vision WARN "aucun modèle n'a répondu 200 (clé invalide ou service dégradé — voir artemis/vision-preflight.txt)"
      ARTEMIS_SKIP_CAUSE="aucun modèle n'a répondu 200"
    else
      mark llm-vision WARN "aucun modèle freellmapi n'accepte les images (voir artemis/vision-preflight.txt)"
      ARTEMIS_SKIP_CAUSE="aucun modèle vision"
    fi
    ARTEMIS_OK=0
  elif ! printf '%s' "$VISION_MODEL" | grep -Eq '^[A-Za-z0-9._:/-]{1,80}$'; then
    # B6: l'id vient d'une sonde distante. Refuse tout ce qui pourrait devenir un
    # token sed/shell au lieu de l'injecter dans une ligne de commande.
    mark llm-vision FAIL "VISION_MODEL rejeté (caractères non sûrs): $(printf '%s' "$VISION_MODEL" | tr -c 'A-Za-z0-9._:/-\n' '?')"
    ARTEMIS_OK=0
    ARTEMIS_SKIP_CAUSE="VISION_MODEL rejeté"
  else
    export ARTEMIS_DEFAULT_MODEL="$VISION_MODEL"
    # Config runtime réécrite DANS $OUT (vendor/ jamais modifié), puis vérifiée: le
    # modèle de tête doit bien être présent dans le fichier produit.
    if "$PY" e2e/model_chain.py apply --in "$ARTEMIS_CONFIG" --out "$ARTEMIS_CONFIG_RUNTIME" \
         --chain-file "$ARTEMIS_CHAIN_FILE" --offset 0 >> "$OUT/run.log" 2>&1 \
       && "$PY" e2e/set_model.py --check "$ARTEMIS_CONFIG_RUNTIME" "$VISION_MODEL" >/dev/null; then
      grep -c -F "\"model\": \"$VISION_MODEL\"" "$ARTEMIS_CONFIG_RUNTIME" > "$OUT/artemis/profile-model-count.txt" || true
      export ARTEMIS_ARTEMIS_JSONC="$ARTEMIS_CONFIG_RUNTIME"
      mark llm-vision PASS "chaîne vision (${ARTEMIS_CHAIN_N}): $(paste -sd, "$ARTEMIS_CHAIN_FILE")"
    else
      mark llm-vision FAIL "réécriture de la config Artemis refusée"
      ARTEMIS_OK=0
      ARTEMIS_SKIP_CAUSE="config Artemis refusée"
    fi
  fi
fi
if [ "$ARTEMIS_OK" = "1" ] && artemis_run smoke "Open the Settings app and report the Android version shown on screen." 200; then
  mark artemis-smoke PASS "LLM + device loop operational"
else
  # Only a smoke call that actually RAN can be "smoke failed": every earlier
  # disable keeps the cause it set (Max m1).
  if [ "$ARTEMIS_OK" = "1" ]; then
    ARTEMIS_SKIP_CAUSE="smoke failed"
  fi
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
# ISC-5: repère conservateur du bloc, pris AVANT bring_fg/api pick. Sert de T0 de
# secours quand l'état ne date pas la course : il sous-estime la fenêtre, jamais l'inverse.
OFFER_STEP_START=$SECONDS
bring_fg "$DRIVER_PKG" || true
api pick >> "$OUT/run.log" 2>&1
# T0 de l'offre : ancré sur la CRÉATION de la course quand state.json la date,
# sinon sur le début du bloc ci-dessus. Jamais sur l'horloge post-pick.
OFFER_T0_LINE=$(offer_t0 "$OUT/state.json" "$OFFER_STEP_START")
OFFER_T0=${OFFER_T0_LINE%% *}
OFFER_T0_SOURCE=${OFFER_T0_LINE##* }
case "$OFFER_T0" in
  ''|*[!0-9]*) OFFER_T0=$OFFER_STEP_START; OFFER_T0_SOURCE=step_start ;;
esac
log "offer_t0 source=$OFFER_T0_SOURCE left=$(offer_left)s"
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

cap driver-offer-with-stops 34-driver-offer-with-stops "$DRIVER_PKG" "$OFFER_CAPTURE_SEC"
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

# D2 (run 40205373538): Artemis observe d'abord, le tap réel vient ensuite, le
# repli API en dernier — les trois DANS la fenêtre d'offre, jamais un 45 s fixe.
# ISC-5: Artemis ne reçoit que `ui_left` = restant - API_RESERVE_SEC, pour que le
# repli d'acceptation garde toujours de quoi répondre dans la fenêtre.
left=$(offer_left)
ui_left=$(( left - API_RESERVE_SEC ))
if [ "$ui_left" -gt 0 ]; then
  artemis_run driver-accept \
    "Open the Galass Driver app, open the incoming ride offer and tap 'Accepter la course'." \
    "$ui_left" "$DRIVER_PKG"
  ARTEMIS_ACCEPT_RC=$?
else
  # Fenêtre consommée ou trop courte : pas de timeout 0, on va droit au repli API.
  ui_left=0
  ARTEMIS_ACCEPT_RC=2
  log "artemis skip: driver-accept (ui_left=${ui_left}s <= 0 — réserve API ${API_RESERVE_SEC}s)"
fi
if [ "$ARTEMIS_ACCEPT_RC" -eq 0 ]; then
  mark artemis-driver-accept PASS "offre acceptée via Artemis"
else
  mark artemis-driver-accept SKIP "artemis fallback (rc=$ARTEMIS_ACCEPT_RC — tap puis API)"
fi

# S10: declines observed while the harness itself retries the accept are not evidence.
DECLINES_BEFORE=$(count_in "ride_unavailable" "$LOG_FILE" 2>/dev/null || echo 0)
ACCEPT_OK=0
# ISC-5: budget UI recalculé après Artemis — même réserve, jamais tout `left`.
ui_left=$(( $(offer_left) - API_RESERVE_SEC ))
[ "$ui_left" -gt 0 ] || ui_left=0
# Crédit UI : un tap n'est « prouvé » que si le Driver était devant AVANT et APRÈS.
# Sinon l'API peut avoir gagné en parallèle, et le sidecar ne doit pas l'affirmer.
UI_TAP_PROVEN=0
FG_BEFORE=$(fg_pkg)
if [ "$ui_left" -gt 0 ] && tap_exact "Accepter la course"; then
  if [ "$FG_BEFORE" = "$DRIVER_PKG" ] && [ "$(fg_pkg)" = "$DRIVER_PKG" ]; then
    UI_TAP_PROVEN=1
  else
    log "offre: tap hors premier plan Driver ($FG_BEFORE -> $(fg_pkg)) — crédit UI non prouvé"
  fi
  if wait_ui "$DRIVER_PKG" "$ui_left" "$MARK_driver_accepted_details"; then
    ACCEPT_OK=1; UI_ACCEPT=1; set_accept_via "$(ui_accept_via)"
  else
    UI_TAP_FAILED=1
  fi
else
  log "offre: tap « Accepter la course » non joué ou manqué (ui_left=${ui_left}s)"
fi
if [ "$ACCEPT_OK" = "0" ]; then
  log "offre: repli API (left=$(offer_left)s)"
  export E2E_UI_TAP_PROVEN="$UI_TAP_PROVEN"
  api accept >> "$OUT/run.log" 2>&1
  api_accept_ok || true
  export E2E_UI_TAP_PROVEN=0
fi
resolve_accept_label  # un seul libellé, calculé et journalisé une fois (D2)
if [ "$ACCEPT_OK" != "1" ]; then
  mark driver-accepted-details FAIL "api fallback: accept"
  [ -n "${ACCEPT_CAUSE:-}" ] || ACCEPT_CAUSE=unknown
  # ISC-3: FAIL seulement si aucun via n'a prouvé l'acceptation (worst-wins).
  # Le détail ne porte QUE des valeurs mesurées : pas de prose inventée sur
  # l'indisponibilité de l'API ou de l'UI.
  mark accept-via FAIL "accept_via=$ACCEPT_VIA cause=$ACCEPT_CAUSE"
  flush_results
  write_flags
  "$PY" e2e/report.py "$OUT" --p0 "$P0_CSV" > "$OUT/early-report.txt" 2>&1 || true
  exit 1
fi
if [ "$ACCEPT_OK" = "1" ]; then
  # Chemin SUCCÈS : seule une preuve réelle (api ou ui) vaut PASS. `unknown` =
  # assigné sans tap prouvé : la course continue, mais le crédit reste WARN.
  case "$ACCEPT_VIA" in
    api|ui)
      mark accept-via PASS "accept_via=$ACCEPT_VIA cause=${ACCEPT_CAUSE:-none} (renewed: voir accept.json si repli API)" ;;
    unknown)
      mark accept-via WARN "accept_via=unknown cause=${ACCEPT_CAUSE:-assigned_without_tap_proof}" ;;
    *)
      mark accept-via WARN "accept_via=${ACCEPT_VIA:-none} cause=${ACCEPT_CAUSE:-inconnue}" ;;
  esac
  # Un renouvellement a créé une NOUVELLE course côté API : l'identifiant et la
  # provenance doivent suivre, sinon les sondes suivantes visent l'ancienne.
  RIDE_ID=$("$PY" -c "import json;print(json.load(open('$OUT/state.json')).get('ride_id') or '')" 2>/dev/null)
  RIDE_UID=$("$PY" -c "
import json
st=json.load(open('$OUT/state.json'))
r=st.get('ride_picked') or {}
print(r.get('uid') or (st.get('ride_create') or {}).get('uid') or '')
" 2>/dev/null)
  RIDE_SOURCE=$("$PY" -c "import json;print(json.load(open('$OUT/state.json')).get('ride_source') or 'unknown')" 2>/dev/null)
  [ -n "$RIDE_SOURCE" ] || RIDE_SOURCE=unknown
  log "ride après acceptation: id=$RIDE_ID uid=$RIDE_UID source=$RIDE_SOURCE"
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
# geofence_stop <index> : fix GPS appliqué ET vérifié avant tout complete-stop.
# Les coordonnées de l'arrêt viennent de $OUT/state.json (détail de course déjà
# sauvegardé), donc le fix injecté ne peut pas diverger de la course réelle — et
# `api loc_stop` relit CES coordonnées : le backend voit l'arrêt que la géofence
# a vérifié, jamais une copie codée en dur.
# `stops_ride` doit être la course courante : après un renouvellement d'offre,
# les arrêts persistés sont périmés (D4).
# Échec => aucun `complete-stop`, ni tap ni repli API.
geofence_stop() {
  local i="$1" n=$(( $1 + 1 )) lat="" lng="" coords="" reason=""
  reason=$("$PY" - "$OUT/state.json" "$i" "${RIDE_ID:-}" <<'PYCOORDS' 2>&1
import json, sys
path, idx, ride = sys.argv[1], int(sys.argv[2]), sys.argv[3]
try:
    with open(path, encoding="utf-8") as fh:
        st = json.load(fh)
except Exception as exc:
    sys.exit(f"{path} illisible ({exc.__class__.__name__})")
stops = st.get("stops") or []
if idx < 0 or idx >= len(stops):
    sys.exit(f"arret {idx} absent de {path}")
tag = str(st.get("stops_ride") or "")
if not ride or tag != ride:
    sys.exit(f"stale stops: stops_ride={tag or 'absent'} != ride_id={ride or 'absent'}")
stop = stops[idx] or {}
try:
    lat, lng = float(stop["latitude"]), float(stop["longitude"])
except (KeyError, TypeError, ValueError):
    sys.exit(f"arret {idx} sans coordonnees numeriques")
print(f"ok {lat} {lng}")
PYCOORDS
)
  case "$reason" in
    "ok "*) coords="${reason#ok }" ;;
    *)
      log "geofence: arrêt $n inutilisable (${reason:-state.json vide}) — complete-stop bloqué"
      return 1 ;;
  esac
  lat="${coords%% *}"; lng="${coords##* }"
  # Un `geo fix` rc=0 ne prouve RIEN : l'émulateur accuse réception avant que le
  # provider GPS ne publie la position (run 40602473815 — rc=0, puis un unique
  # dumpsys 3 s plus tard encore périmé, arrêt bloqué, complete-stop impossible).
  # Chaque round RÉINJECTE, attend, relit, et vérifie ; GEO_RETRY_ROUNDS compte les
  # essais SUPPLÉMENTAIRES (défaut 2, borné 0..5 pour ne pas boucler sans fin).
  local rounds="${GEO_RETRY_ROUNDS:-2}"
  case "$rounds" in ''|*[!0-9]*) rounds=2 ;; esac
  [ "$rounds" -gt 5 ] && rounds=5
  local total=$((rounds + 1)) k=1 ok=0
  mkdir -p "$OUT/diag"
  # Provider FIGÉ sur gps : un knob permettrait de « vérifier » un fix network
  # périmé, exactement le bypass que geo.py existe pour empêcher. La limite reste
  # réglable, bornée à la plage qui a un sens backend (100..2000 m). Le retry ne
  # touche NI l'un NI l'autre : il retente la même preuve, il ne l'affaiblit pas.
  local limit="${GEO_STOP_LIMIT_M:-500}"
  case "$limit" in ''|*[!0-9]*) limit=500 ;; esac
  [ "$limit" -lt 100 ] && limit=100
  [ "$limit" -gt 2000 ] && limit=2000
  while [ "$k" -le "$total" ]; do
    if ! adb emu geo fix "$lng" "$lat" >/dev/null 2>&1; then
      log "geofence: adb emu geo fix a échoué (arrêt $n, $lat,$lng)"
      return 1
    fi
    sleep "${GEO_SETTLE_SEC:-3}"
    local dump="$OUT/diag/geo-fix-stop$n-r$k.txt"
    adb shell dumpsys location > "$dump" 2>/dev/null || true
    # Le dernier relevé garde son nom historique : report et artefacts le publient.
    cp "$dump" "$OUT/diag/geo-stop$n.txt" 2>/dev/null || true
    log "geofence: arrêt $n fix GPS essai $k/$total (fix=diag/geo-fix-stop$n-r$k.txt)"
    if "$PY" e2e/geo.py check --fix "$dump" \
         --stop-lat "$lat" --stop-lng "$lng" \
         --limit "$limit" \
         --provider gps >> "$OUT/run.log" 2>&1; then
      ok=1
      break
    fi
    k=$((k + 1))
  done
  if [ "$ok" -ne 1 ]; then
    log "geofence: fix GPS non appliqué pour l'arrêt $n après $total essai(s) (voir diag/geo-fix-stop$n-r$total.txt)"
    return 1
  fi
  # Le backend doit voir le chauffeur à l'arrêt : POST seulement APRÈS vérification,
  # et un refus de loc_stop (arrêt disparu/invalide) bloque le complete-stop.
  if ! STOP_INDEX="$i" api loc_stop >> "$OUT/run.log" 2>&1; then
    log "geofence: loc_stop a refusé l'arrêt $n — complete-stop bloqué"
    return 1
  fi
  return 0
}

deadline_check stops  # budget mur : verdict partiel plutôt qu'un step qui traîne
ui_stops=0
for i in 0 1; do
  n=$((i + 1))
  # D4: aucun complete-stop sans un fix GPS vérifié (les coordonnées viennent de
  # state.json, jamais de constantes qui divergent de la course réelle).
  if ! geofence_stop "$i"; then
    # ISC-5: un fix non vérifié interdit TOUT complete-stop — pas seulement celui
    # de cet arrêt. Le repli API (stop_one) complète le premier arrêt PENDING quel
    # que soit STOP_INDEX : continuer sur l'arrêt suivant compléterait un arrêt
    # jamais vérifié. On sort de la boucle, le run continue après elle.
    mark "driver-stop${n}-done" FAIL "geofence: fix GPS non appliqué (arrêt $n)"
    break
  fi
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
    # `geofence_stop` a déjà injecté ET vérifié le fix : l'assertion est portée
    # par cette SEULE commande (préfixe d'environnement), jamais exportée.
    STOP_INDEX=$i E2E_GEOFENCE_OK=1 api stop_one >> "$OUT/run.log" 2>&1
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
# D3 (run 40205373538): `received-cash-payment` a été appelé AVANT que le rider
# demande le cash — le backend a répondu not_found et le verdict a accusé le
# harnais. On sonde l'état backend (payment_status==3) avant toute action, et on
# n'appelle JAMAIS l'endpoint si le cash n'a pas été demandé.
# Le détail du FAIL vient de cash.json : `cash_status_unavailable` (état
# illisible) n'est pas `cash_not_requested` (statut lu, différent de 3), et une
# config invalide (rc 4) ne dit rien du comportement du rider.
CASH_REQUESTED=0
CASH_RC=0
api cash_wait >> "$OUT/run.log" 2>&1 || CASH_RC=$?
CASH_CAUSE=$("$PY" -c 'import json,sys
try:
    print(json.load(open(sys.argv[1])).get("cause") or "")
except Exception:
    print("")' "$OUT/cash.json" 2>/dev/null || true)
if [ "$CASH_RC" = "0" ]; then
  CASH_REQUESTED=1
  log "cash: le rider a demandé le paiement espèces (payment_status=3)"
elif [ "$CASH_RC" = "4" ]; then
  mark driver-cash-confirmed FAIL "cash config_error"
  log "cash: config_error (${CASH_CAUSE:-config_error}) — aucun POST, comportement rider non observé"
else
  mark driver-cash-confirmed FAIL "${CASH_CAUSE:-cash_status_unavailable}"
  log "cash: ${CASH_CAUSE:-cash_status_unavailable} (rc=$CASH_RC) — tap et received-cash-payment sautés (aucun POST)"
fi
CASH_UI_OK=0
if [ "$CASH_REQUESTED" = "1" ]; then
  if tap_exact "Confirmer le paiement"; then
    if wait_ui "$DRIVER_PKG" 25 "$MARK_driver_cash_confirmed"; then
      UI_CASH=1; CASH_UI_OK=1
      log "cash confirmé par l'UI chauffeur (écran suivant prouvé)"
    fi
  fi
  if [ "$CASH_UI_OK" != "1" ]; then
    # Le POST n'est pas la preuve (run 40602473815) : `received-cash-payment` a
    # répondu 200 avec `remark: "not_found"` — un refus du backend séparé. Le
    # statut HTTP seul ne peut pas écrire PASS ici : le rc ET la cause relue dans
    # cash.json décident, et un refus reste un FAIL explicite porteur du cause.
    CASH_DRIVER_RC=0
    api cash_driver >> "$OUT/run.log" 2>&1 || CASH_DRIVER_RC=$?
    CASH_CAUSE=$("$PY" -c 'import json,sys;print((json.load(open(sys.argv[1])) or {}).get("cause") or "")' "$OUT/cash.json" 2>/dev/null || true)
    if [ "$CASH_DRIVER_RC" != "0" ]; then
      mark driver-cash-confirmed FAIL "${CASH_CAUSE:-backend_refused}"
      log "cash: backend a refusé la confirmation (rc=$CASH_DRIVER_RC, cause=${CASH_CAUSE:-inconnue}) — FAIL maintenu"
    else
      mark driver-cash-confirmed FAIL "api fallback: cash"
      log "cash: confirmation par API seulement (UI chauffeur non prouvée) — FAIL maintenu"
    fi
  fi
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
  # OUT_ABS (B1) : un OUT absolu doublait le préfixe et le log partait ailleurs.
  ( cd vendor/artemis && nohup ./.venv/bin/artemis-admin > "$OUT_ABS/diag/artemis-admin.log" 2>&1 & )
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
