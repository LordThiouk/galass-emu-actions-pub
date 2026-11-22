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
. "$(dirname "$0")/smoke_log.sh"

# S6: nothing from a previous run may leak into this verdict.
mkdir -p "$OUT"
rm -rf "$OUT"/screens "$OUT"/ui "$OUT"/state.json "$OUT"/results-raw.txt \
       "$OUT"/results-live.txt "$OUT"/captures.txt "$OUT"/ui-flags.txt \
       "$OUT"/last-ride-state.txt "$OUT"/assign-pre.txt "$OUT"/watchdog-report.txt \
       "$OUT"/report.json "$OUT"/results.txt "$OUT"/verdict.txt
mkdir -p "$OUT/screens" "$OUT/ui" "$OUT/logcat" "$OUT/artemis"
: > "$OUT/run.log"
: > "$SMOKE_LOG"
: > "$OUT/results-live.txt"
: > "$OUT/captures.txt"

declare -a RESULTS=()
. "$(dirname "$0")/ui.sh"
. "$(dirname "$0")/device_prep.sh"

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
count_in_ci() { # comme count_in, insensible à la casse (l'app journalise DRIVER_ASSIGNED en majuscules)
  local n rc
  n=$(grep -a -i -F -c "$1" "$2" 2>/dev/null); rc=$?
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
MARK_rider_destination_selected='+E2E Dest Point E|Point E|-Où allez-vous'
# Picker ouvert par « Ajouter un arrêt » : my_strings.dart confirmLocation (766) + stopPickerHint (47).
MARK_rider_stop_picker='+Confirmer le lieu|Adresse de l.arrêt|-Où allez-vous'
MARK_rider_multistop_summary='+Arrêt 2|Ajouter un arrêt|Changer|-Où allez-vous'
MARK_rider_driver_assigned='+E2E Driver|+E2E-0001'
# B2 — chaque marqueur exige un état qui ne peut pas pré-exister à la transition.
# Un libellé présent dès l'écran précédent ne prouve rien ; « Arrêt 1 » est rendu en
# course (ride_itinerary_timeline : titre de la jambe courante), donc jamais un négatif.
# RUNNING = currentLegBanner « En route vers <stop> » (ride_details_bottom_sheet_widget:78-96),
# null sauf RIDE_ACTIVE/RIDE_RUNNING : seul signal exclusif à la jambe. Le libellé du stop
# (« E2E Stop 1 ») est rendu dès PENDING par la timeline complète, donc jamais un positif.
MARK_rider_running_start='+En route vers|-Chargement'
# Jambe 1 = bannière présente ET « Arrêt 2 » absent : la timeline réduite à la jambe
# courante (ride_itinerary_timeline:288-295) ne rend le libellé du stop 2 qu'après l'ack du 1.
MARK_rider_stop1_active='+En route vers|-Arrêt 2'
# B2 — avancement des arrêts (my_strings.dart rider 64-65, stopsProgressSingular/Plural) :
# l'app corrigée rend le compteur « X/2 arrêt(s) terminé(s) » en course (décision Papa : le
# compteur jamais visible en RUNNING était un bug produit, fix app parallèle). Les marqueurs
# mesurent les DEUX signaux du même écran, ET : le compteur, et la bannière de destination
# (`currentLegBanner`, my_strings currentLeg) remise à jour sur la jambe suivante. Après
# l'arrêt 1 : « 1/2 arrêt terminé » + « En route vers E2E Stop 2 Fann » (point 23). Après
# l'arrêt 2 : « 2/2 arrêts terminés » + « En route vers E2E Dest Point E », la bannière
# retombant sur la destination quand toutes les escales sont faites (point 24).
# Un compteur seul peut mentir (bannière figée = l'ancien bug) et la bannière seule ne prouve
# rien (elle s'affiche dès l'arrêt 1, stops vides) : chaque point exige les deux.
MARK_rider_stop1_done='+1/2 arrêts? terminés?&+En route vers E2E Stop 2|En route vers Fann'
MARK_rider_stop2_done='+2/2 arrêts? terminés?&+En route vers E2E Dest Point E|En route vers Point E'
MARK_rider_destination_arrived='+Vous êtes arrivé à destination|+Payer maintenant|Facture à payer|-En attente de la réponse du chauffeur'
MARK_rider_cash_methods='+Espèces|Cash|+Wave|Orange Money'
MARK_rider_cash_waiting='+En attente de la réponse du chauffeur|-Payer maintenant'
# Run 44441344957 : les captures 26/27/29/30 tombaient sur l'écran de composition (« Lieu de
# prise en charge », « Destination », « Confirmer le lieu »), où les deux alternatives « Lieu de
# prise en charge » et « Destination » suffisaient à faire PASSER 29 — faux PASS écarté par le
# négatif « Confirmer le lieu », l'affordance de composition.
MARK_rider_final_itinerary='+Course terminée|-Chargement|-Confirmer le lieu'
# Écran réel : « Avis » (+ « Course terminée », Arrêt 1 / Arrêt 2). « Notez » n'existe pas.
MARK_rider_rating='+Avis|Course terminée|-Payer maintenant|-Chargement'
# Accueil réel après la course (run 45004400552, capture 31) : « Partons pour une course » (letsTakeARide,
# location_pickup_widget:57) ET « Où allez-vous ? » (whereToGo) ; ni la notation, ni le dialogue de course,
# ni le sélecteur de composition (« Confirmer le lieu ») ne doivent être au premier plan.
MARK_rider_home_after='+Partons pour une course&+Où allez-vous&-Course terminée&-Terminez votre course&-Confirmer le lieu'
# L'offre est un toast AU-DESSUS du dashboard : la navbar « Accueil » reste visible par
# construction, elle n'est jamais un négatif. Les quatre libellés sont des ALTERNATIVES.
MARK_driver_offer_with_stops='+Accepter\s+la\s+course|REFUSER|Prix recommandé|Consignes du client'
MARK_driver_accepted_details='+Point de prise en charge|+Arrêt 1|-Accepter\s+la\s+course'
# B2 — pickup-only : « Point de prise en charge » appartient à l'écran accepté, pas ici.
MARK_driver_pickup_arrived='+Je suis arrivé|Prise en charge du client|-Accepter\s+la\s+course'
# Écran RUNNING réel : bouton « Confirmer l'arrêt » (confirmStop) + « Terminer la course ». Aucun bouton
# « Terminer l'arrêt » n'existe. L'écran ACTIVE (« Prise en charge du client ») montre déjà « Arrêt 1 » :
# seul le bouton d'arrêt distingue la course démarrée.
MARK_driver_running_stop1='+Arrêt 1|-Accepter\s+la\s+course|Prise en charge du client|+Confirmer l.arrêt'
# Progression d'arrêts (my_strings driver 82-85) : « Arrêt 2 »/« Arrivée » sont déjà
# affichés (course en cours) ou appartiennent à l'écran de fin — faux PASS, retirés.
MARK_driver_stop1_done='+1/2 arrêts? terminés?|-Accepter\s+la\s+course'
MARK_driver_stop2_done='+2/2 arrêts? terminés?|-Accepter\s+la\s+course'
MARK_driver_cash_request='+Reçois le paiement|+Confirmer le paiement|+Tarif de la course'
MARK_driver_cash_confirmed='+Tarif de la course|Distance|-Reçois\s+le\s+paiement|-Confirmer\s+le\s+paiement'
# L'app chauffeur n'a pas d'écran de résumé : après le cash elle revient à l'accueil (« En ligne »,
# Accueil / Activité / Courses). C'est cet écran réel qui est capturé en 43.
MARK_driver_summary='+En ligne|-Reçois\s+le\s+paiement|Confirmer\s+le\s+paiement|Terminer la course'
# Non-P0 captures (same table, same rules).
MARK_driver_home_online='+En ligne'
# S3 : la capture in-ride doit nier l'offre (-REFUSER) pour être distinguable de (a).
# « Arrêt 1 » seul vaut aussi sur l'écran ACTIVE : « Terminer la course » (RUNNING) est exigé.
MARK_driver_inride_stops='+Terminer la course|-Accepter\s+la\s+course|-REFUSER|-Prise en charge du client|+Arrêt 1'

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
trap 'release_watchdog; kill "${LOGCAT_PID:-}" "${SAMPLER_PID:-}" 2>/dev/null; pkill -P "$$" 2>/dev/null; true' EXIT
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
  snap_with_recovery "$name" "$pkg" "$tmo" "$spec" "" "$point" "$src"
  case $? in
    0) mark "$point" PASS "ui cap=$name.png marker ok ($src)$([ "$SNAP_REPLAYED" = 1 ] && echo " — rejoué après reprise émulateur, preuve d'écran")" ;;
    2) mark "$point" INCONCLUSIVE "emulator_crash: $EMU_LAST_CRASH_NOTE (cap=$name, point non compté comme échec de l'app)" ;;
    *) # L'écran RÉELLEMENT capturé, dans la raison même : le digest de fin de log le
       # publie alors qu'un extrait borné ne verrait jamais la ligne de capture.
       local txt=""
       if [ -s "$OUT/ui/$name.xml" ]; then
         txt=$("$PY" e2e/dump_diag.py "$OUT/ui/$name.xml" --texts 2>/dev/null \
                 | sed -E 's/(key|token|access_token)=[^&" ]+/\1=***/g' | cut -c1-140)
       fi
       mark "$point" FAIL "ui cap=$name.png marker absent ($spec)${txt:+ | écran=$txt}" ;;
  esac
}

# compose_ride_ui : compose la course DANS l'app Rider par taps (jamais par l'API), pour que
# `api pick` l'adopte avec ride_source=ui (B2 / ISC-F13-3 concluants). Libellés = my_strings.dart
# de Galass_Rider (whereToGo, addStop, confirmLocation, cashPayment/cash, findDriver). Seul
# chemin vers les états rider-destination-selected / rider-stop-picker / rider-multistop-summary :
# la composition les capture elle-même, aux moments où ils existent. Retour 0 = demande envoyée,
# 1 = échec tracé (le repli API reste marqué FAIL, jamais PASS).
ui_pick_place() {
  local query="$1" label="$2" typed
  [ "${PLACES_PROBED:-0}" = 1 ] || { places_diag_start; PLACES_PROBED=1; }
  # `adb input text` ne tape pas les accents : on tape la forme ASCII (« Médina » → « Medina »).
  typed=$(printf '%s' "$query" | python3 -c 'import sys,unicodedata;print(unicodedata.normalize("NFKD",sys.stdin.read()).encode("ascii","ignore").decode(),end="")')
  [ "$typed" = "$query" ] || log "places: requête tapée en ASCII ($typed) car « $query » a des accents"
  # Le focus n'est PAS acquis : sur l'écran de recherche le champ pickup est rempli et la
  # destination vide ; sans tap la saisie part ailleurs (champ destination resté vide au run 44192999654).
  tap_node_mode --empty-input "$RIDER_PKG" || log "places: aucun champ vide à focaliser"
  adb shell input text "$(printf '%s' "$typed" | sed 's/ /%s/g')" >/dev/null 2>&1
  sleep 2
  if ! places_field_check "$typed"; then
    log "places: saisie absente du champ, un seul nouvel essai (tap champ vide + saisie)"
    tap_node_mode --empty-input "$RIDER_PKG" || true
    adb shell input text "$(printf '%s' "$typed" | sed 's/ /%s/g')" >/dev/null 2>&1
    sleep 2
    places_field_check "$typed" || true
  fi
  # 20 s d'attente instrumentée : réseau/Places (HTTP, erreurs logcat) vs écran lisible ou non.
  places_wait_result "$label" 20 || true
  # Run 44882204874 : le 2e arrêt (Fann) n'a pas trouvé la 1re suggestion dans la fenêtre
  # d'attente ; le tap est rejoué sur des fenêtres fraîches avant de déclarer le résultat introuvable.
  local pick_rc=1
  for _ in 1 2 3; do
    if tap_prefix "$RIDER_PKG" "$label" 12; then
      pick_rc=0
      break
    fi
    sleep 2
  done
  if [ "$pick_rc" != 0 ]; then
    log "compose_ride_ui: résultat « $label » introuvable"
    ui_screen_diag "places: écran sans résultat"
    return 1
  fi
}
compose_ride_ui() {
  log "compose_ride_ui: début"
  bring_fg "$RIDER_PKG" || true
  tap_node_verified "$RIDER_PKG" "Où allez-vous ?" || { log "compose_ride_ui: le tap sur « Où allez-vous ? » n'a rien ouvert (repli API, FAIL)"; return 1; }
  # Lieux RÉELS de Dakar : Google Places ne connaît aucun nom inventé.
  ui_pick_place "Point E" "Point E," || return 1
  # 15 : destination seule retenue, avant tout arrêt.
  cap rider-destination-selected 15-rider-destination-selected "$RIDER_PKG" 25
  # Le picker reste ouvert après le choix : l'ajout d'arrêt n'apparaît qu'après la confirmation.
  tap_when "$RIDER_PKG" "Confirmer le lieu" || { log "compose_ride_ui: Confirmer le lieu absent"; return 1; }
  local stop n=0
  for stop in "Médina" "Fann"; do
    n=$((n + 1))
    tap_when "$RIDER_PKG" "Ajouter un arrêt" || { log "compose_ride_ui: Ajouter un arrêt absent"; return 1; }
    if [ "$n" -eq 1 ]; then
      # 16 : picker ouvert, AVANT toute saisie (hint « Adresse de l'arrêt », « Confirmer le lieu »).
      cap rider-stop-picker 16-rider-stop-picker "$RIDER_PKG" 20
    fi
    ui_pick_place "$stop" "$stop," || return 1
    tap_when "$RIDER_PKG" "Confirmer le lieu" "Confirmer" "Confirmer l'arrêt" >/dev/null 2>&1 || log "compose_ride_ui: confirmation de l'arrêt « $stop » absente"
  done
  # 17 : les deux arrêts sont posés, AVANT la demande.
  cap rider-multistop-summary 17-rider-multistop-summary "$RIDER_PKG" 25
  # Bridge-B3 : ordre des arrêts TEL QU'AFFICHÉ par l'app (écran de composition), source
  # indépendante du backend — ride/list ne porte pas les stops et une course composée par
  # l'UI n'a pas de ride_create. Sans cette source, le gate ne mesure que la relecture.
  COMPOSE_ORDER=$("$PY" e2e/compose_order.py "$OUT/ui/17-rider-multistop-summary.xml" \
                    --state "$OUT/state.json" 2>/dev/null || true)
  log "compose order (17): ${COMPOSE_ORDER:-WARN|indisponible}"
  # Les arrêts posés, l'app demande un SERVICE (« Sélectionnez un service ») : sans lui, ni mode de paiement
  # ni bouton de demande (run 44619326903). Le formulaire s'ouvre ensuite : mode de paiement, puis demande.
  tap_prefix "$RIDER_PKG" "Voiture" 20 || { log "compose_ride_ui: service Voiture absent"; return 1; }
  # Le formulaire s'ouvre sous la liste de services : le faire défiler dans la vue (deux arrêts allongent l'écran).
  sleep 3; adb shell input swipe 540 1900 540 700 400 >/dev/null 2>&1; sleep 2
  # Le formulaire affiche déjà « Paiement en espèces » (run 44647412466) : ne PAS rouvrir la feuille de paiement.
  # Driver foreground + « En ligne » confirmé AVANT le tap « Trouver un chauffeur » (close-7 timing).
  bring_fg "$DRIVER_PKG" || true
  if ! wait_ui "$DRIVER_PKG" 20 "$MARK_driver_home_online"; then
    log "compose_ride_ui: driver non En ligne avant Trouver un chauffeur"
    ui_screen_diag "compose: driver non En ligne"
  fi
  bring_fg "$RIDER_PKG" || true
  # Le titre du formulaire porte le même libellé que le bouton : viser le DERNIER nœud (le bouton).
  TAP_PICK=last tap_when "$RIDER_PKG" "Trouver un chauffeur" "Réserver une course" || { log "compose_ride_ui: bouton demande absent"; ui_screen_diag "compose: bouton demande absent"; return 1; }
  log "compose_ride_ui: demande envoyée par l'UI (driver En ligne confirmé avant tap)"
  return 0
}

api() { STEP="$1" "$PY" e2e/api_flow.py; }
# ui_action exige le dialogue de confirmation ; try_tap ne l'exige pas (navigation).
try_confirm() { local pkg="$1"; shift; local l; for l in "$@"; do ui_action "$pkg" "$l" && return 0; done; return 1; }
try_tap() { local pkg="$1"; shift; tap_when "$pkg" "$@"; }
ride_state() {
  STEP=ride_state "$PY" e2e/api_flow.py > "$OUT/last-ride-state.txt" 2>"$OUT/api-flow.err"
  smoke_emit_file "$OUT/api-flow.err"
  log "ride_state: $(tr '\n' ' ' < "$OUT/last-ride-state.txt")"
}

launch() { adb shell monkey -p "$1" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1; }
# wait_fg : un cold start de debug Flutter dépasse largement 6s. Attendre le premier
# plan évite un `wait_ui` qui rend rc=3 sur le launcher avant même la première frame.
wait_fg() {
  local pkg="$1" tmo="${2:-45}" t0=$SECONDS
  while [ $((SECONDS - t0)) -lt "$tmo" ]; do
    [ "$(fg_pkg)" = "$pkg" ] && return 0
    # dumpsys peut rester sur le launcher : le dump uiautomator tranche.
    if ui_xml && [ "$(xml_pkg)" = "$pkg" ]; then
      log "wait_fg: $pkg prouvé par le dump uiautomator (dumpsys fg=$(fg_pkg))"
      return 0
    fi
    # L14 (run 44680233906) : ce sondage à 2s a lui seul consommé ~13s avant de
    # prouver le passage au premier plan du chauffeur, sur une fenêtre d'offre
    # de ~30s — resserré à 1s pour laisser de la marge à la capture/accept qui suit.
    sleep 1
  done
  fg_diag
  log "wait_fg: $pkg jamais au premier plan (${tmo}s, fg=$(fg_pkg))"
  return 1
}
# Bascule explicite entre étapes (jamais depuis un wait).
bring_fg() {
  fg_is "$1" && return 0
  log "bring_fg $(fg_pkg) -> $1"
  # REORDER_TO_FRONT : ramène la tâche existante sans la recréer (monkey LAUNCHER renvoyait le Driver à
  # l'accueil). Repli monkey si le composant n'est pas résolu.
  local comp
  comp=$(adb shell cmd package resolve-activity --brief -c android.intent.category.LAUNCHER "$1" 2>/dev/null | tail -1 | tr -d '\r')
  if [[ "$comp" == */* ]] && adb shell am start -n "$comp" -f 0x20000 >/dev/null 2>&1; then :
  else adb shell monkey -p "$1" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1; fi
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
# Mesure: échantillon hôte/AVD toutes les 10 s (explique un plantage : CPU/RAM ou gms ?).
"$PY" e2e/emu_sampler.py "$OUT" "$LOG_FILE" 10 >/dev/null 2>&1 &
SAMPLER_PID=$!
# Stabilité émulateur : geler configupdater, puis attendre 60 s sans ANR avant tout test.
emu_quiet_prepare
if emu_wait_calm "$LOG_FILE" 60 240; then
  mark emulator-calm PASS "aucun ANR pendant 60 s avant les tests"
else
  mark emulator-calm WARN "émulateur jamais calme (ANR continus) avant les tests"
fi
emu_checkpoint

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
# Boîte « Let app always run in background? » du driver : exempter AVANT le lancement.
whitelist_battery "$DRIVER_PKG" "$RIDER_PKG"
# État de départ (step 33 / D-1): le driver doit être connecté (jeton injecté ci-dessus)
# et EN LIGNE côté backend avant que l'app driver démarre et avant toute course.
if api online >> "$OUT/run.log" 2>&1; then
  mark driver-online-api PASS "driver mis en ligne et confirmé par le backend"
else
  mark driver-online-api FAIL "driver non confirmé en ligne (backend) — voir run.log"
fi
launch "$RIDER_PKG"; wait_fg "$RIDER_PKG" 60 || true
cap rider-home-loaded 14-rider-home-loaded "$RIDER_PKG" 90
launch "$DRIVER_PKG"; wait_fg "$DRIVER_PKG" 60 || true
# Filet : si la boîte s'affiche quand même, la fermer par Allow (trouvé dans le dump).
dismiss_bg_dialog && log "boîte batterie fermée par Allow" || true
# Un driver hors ligne ne reçoit aucune offre : assertion dure (FAIL), sauf plantage d'émulateur.
snap_with_recovery 33-driver-home-online "$DRIVER_PKG" 60 "$MARK_driver_home_online" "" "driver-home-online" "grounded"
case $? in
  0) mark driver-home-online PASS "ui cap=33-driver-home-online.png (replay=$SNAP_REPLAYED)" ;;
  2) mark driver-home-online INCONCLUSIVE "emulator_crash: $EMU_LAST_CRASH_NOTE (33)" ;;
  *) mark driver-home-online FAIL "driver home sans « En ligne » (33) — aucune offre ne peut arriver" ;;
esac
# Baseline B7 prise AVANT toute composition de course : l'offre peut être poussée dès
# la création, donc une baseline juste avant `api pick` serait déjà polluée.
NOTIF_BEFORE=$(notif_probe "$DRIVER_PKG" "")
NOTIF_BEFORE_PKG=$(printf '%s' "$NOTIF_BEFORE" | json_field records_pkg)
log "notif baseline: $NOTIF_BEFORE"

# Artemis n'appelle plus d'API LLM quand ARTEMIS_DISABLE=1 :
# le CLI opencode pilote l'émulateur (adb + e2e/run.sh / api_flow.py / ui.sh).
if [ "${ARTEMIS_DISABLE:-0}" = "1" ]; then
  ARTEMIS_OK=0
  mark llm-vision SKIP "Artemis LLM désactivé (CLI opencode pilote l'émulateur)"
  mark artemis-smoke SKIP "Artemis LLM désactivé — UI via taps + API (e2e/ui.sh)"
else
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
fi

# -------------------------------------------------- rider compose (UI only)
deadline_check rider-compose  # budget mur : verdict partiel plutôt qu'un step qui traîne
api reset >> "$OUT/run.log" 2>&1
bring_fg "$RIDER_PKG" || true

# Les trois états rider-* ne sont plus joués par le LLM : la composition UI les produit
# par taps et les capture elle-même. Les points restent émis, en SKIP, avec le motif.
mark artemis-rider-destination SKIP "état produit par compose_ride_ui (taps UI) — plus d'appel Artemis"
mark artemis-rider-stop1 SKIP "état produit par compose_ride_ui (taps UI) — plus d'appel Artemis"
mark artemis-rider-stop2 SKIP "état produit par compose_ride_ui (taps UI) — plus d'appel Artemis"

# Composition INCONDITIONNELLE : aucun repli ne peut produire les états d'écran rider,
# et `ui_compose` garde le point ride-composed en FAIL si elle échoue.
ui_compose=0
if compose_ride_ui; then
  ui_compose=1
else
  log "compose_ride_ui: échec — api pick créera la course (ride_source=api) et ride-composed restera FAIL"
fi

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
# B8: a ride composed by the harness proves nothing about the Rider UI. Et si la
# composition UI a échoué (ou n'a pas été concluante), aucun repli ne rend le point PASS :
# `api pick` a alors créé la course côté API pour que la suite s'exécute.
if [ "${ui_compose:-0}" != "1" ] || [ "$RIDE_SOURCE" = "api" ]; then
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

# Close-7 timing: Driver foreground + « En ligne » confirmé AVANT « Trouver un chauffeur »
# (fait dans compose_ride_ui). Ici: UNE seule boucle de poll qui dump+tap « Accepter la course »,
# capture 34 = XML au moment exact du tap ; log t_offer_to_tap ; WARN si > 25 s.
# Exactement 2 `\bapi accept\b`.
# Run 44912533775 (close-7) : l'offre n'a jamais été rendue, pour deux raisons de harnais
# nommées — (a) la boucle rappelait `bring_fg` (monkey relaunch) à CHAQUE tour, ce qui relance
# l'app Driver et fait perdre l'overlay d'offre ; (b) elle lisait `status=0` comme « offre
# expirée » alors que `status=0` est l'état NORMAL d'une course fraîche sans chauffeur, et sa
# re-demande (« Trouver un chauffeur ») invalidait l'offre en cours (remark backend
# `offer_expired`, 40 tentatives d'acceptation API toutes mortes). Donc : UN bring_fg avant la
# boucle, aucun relaunch pendant le poll, aucune re-demande pilotée par `status=0`.
offer_t0=$SECONDS
offer_seen=0
t_offer_to_tap=""
offer_poll_screen=""
bring_fg "$DRIVER_PKG" || true
while [ $((SECONDS - offer_t0)) -lt 75 ]; do
  if ! ui_xml; then sleep 2; continue; fi
  clear_sys
  if _spec_ok "$MARK_driver_offer_with_stops"; then
    # Capture 34 = XML au moment exact du tap (même dump).
    cp "$UI_XML_PATH" "$OUT/ui/34-driver-offer-with-stops.xml" 2>/dev/null || true
    adb exec-out screencap -p > "$OUT/screens/34-driver-offer-with-stops.png" 2>/dev/null
    cap_fg_raw=$(fg_pkg 2>/dev/null || echo UNKNOWN)
    cap_fg="$cap_fg_raw"; cap_fg_src="dumpsys"
    [ "$(xml_pkg)" = "$DRIVER_PKG" ] && cap_fg="$DRIVER_PKG" && cap_fg_src="ui-dump"
    _write_sidecar "34-driver-offer-with-stops" "$DRIVER_PKG" "$MARK_driver_offer_with_stops" \
      "MATCH" "$((SECONDS - offer_t0))" "" "0" "driver-offer-with-stops" "grounded" "" "$cap_fg" "$cap_fg_src"
    mark driver-offer-with-stops PASS "ui cap=34-driver-offer-with-stops.png marker ok (grounded, at-tap)"
    t_offer_to_tap=$((SECONDS - offer_t0))
    log "t_offer_to_tap=${t_offer_to_tap}s"
    [ "$t_offer_to_tap" -gt 25 ] && mark driver-offer-timing WARN "t_offer_to_tap=${t_offer_to_tap}s > 25s"
    tap_exact "Accepter la course" || log "tap_exact Accepter la course: miss après capture"
    offer_seen=1
    break
  fi
  # Premier écran Driver vu pendant le poll : sert au diagnostic borné du timeout.
  if [ -z "$offer_poll_screen" ]; then
    offer_poll_screen=$(python3 e2e/dump_diag.py "$UI_XML_PATH" --texts 2>/dev/null | cut -c1-160)
  fi
  sleep 2
done
if [ "$offer_seen" != "1" ]; then
  log "t_offer_to_tap=TIMEOUT (offre non vue en $((SECONDS-offer_t0))s)"
  [ -n "$offer_poll_screen" ] && log "offre: premier écran driver pendant le poll: $offer_poll_screen"
  ui_screen_diag "offre: écran driver au timeout"
  cap driver-offer-with-stops 34-driver-offer-with-stops "$DRIVER_PKG" 5
fi
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

# Sonde PRE (diagnostic read-only) : l'état du chauffeur AVANT la fenêtre d'accept.
# Le step n'écrit rien et ne bascule pas online (online-status est un TOGGLE).
ASSIGN_PRE_RAW=$(api assign_pre 2>> "$OUT/run.log" || true)
ASSIGN_PRE_LINE=$(printf '%s\n' "$ASSIGN_PRE_RAW" | grep -a -F "assign_pre" | tail -n1)
[ -n "$ASSIGN_PRE_LINE" ] || ASSIGN_PRE_LINE="assign_pre bal=? online=?"
printf '%s\n' "$ASSIGN_PRE_LINE" > "$OUT/assign-pre.txt"

# S10: declines observed while the harness itself retries the accept are not evidence.
DECLINES_BEFORE=$(count_in "ride_unavailable" "$LOG_FILE" 2>/dev/null || echo 0)
ACCEPT_FALLBACK_LOG="$OUT/accept-fallback.log"
ACCEPT_FALLBACK_PID=""
if [ "$offer_seen" = "1" ]; then
  # Filet API : lancé après le tap, délai court pour laisser wait_ui confirmer le tap UI
  # mais assez tôt pour accepter avant expiration de l'offre si le tap UI n'a pas pris.
  (sleep 8; STEP=accept "$PY" e2e/api_flow.py) > "$ACCEPT_FALLBACK_LOG" 2>&1 &
  ACCEPT_FALLBACK_PID=$!
  if wait_ui "$DRIVER_PKG" 25 "$MARK_driver_accepted_details"; then
    UI_ACCEPT=1; log "acceptation par l'UI (écran suivant prouvé)"
    wait "$ACCEPT_FALLBACK_PID" 2>/dev/null; cat "$ACCEPT_FALLBACK_LOG" >> "$OUT/run.log" 2>/dev/null
  else
    mark driver-accepted-details FAIL "api fallback: accept"
    wait "$ACCEPT_FALLBACK_PID" 2>/dev/null; cat "$ACCEPT_FALLBACK_LOG" >> "$OUT/run.log" 2>/dev/null
    ride_state
    if ! grep -q "status=3\|status=4\|status=2" "$OUT/last-ride-state.txt" 2>/dev/null; then
      api accept >> "$OUT/run.log" 2>&1
    fi
  fi
else
  log "tap_exact « Accepter la course » manqué (offre non vue)"
  (STEP=accept "$PY" e2e/api_flow.py) > "$ACCEPT_FALLBACK_LOG" 2>&1 &
  ACCEPT_FALLBACK_PID=$!
  mark driver-accepted-details FAIL "api fallback: accept"
  wait "$ACCEPT_FALLBACK_PID" 2>/dev/null; cat "$ACCEPT_FALLBACK_LOG" >> "$OUT/run.log" 2>/dev/null
  ride_state
  if ! grep -q "status=3\|status=4\|status=2" "$OUT/last-ride-state.txt" 2>/dev/null; then
    api accept >> "$OUT/run.log" 2>&1
  fi
fi
sleep 2
ride_state
DECLINES_AFTER=$(count_in "ride_unavailable" "$LOG_FILE" 2>/dev/null || echo 0)
declines_pre=$(( DECLINES_AFTER - DECLINES_BEFORE ))
[ "$declines_pre" -lt 0 ] && declines_pre=0

# diag-driver-assign — MESURE, pas une porte : status==0 (aucun chauffeur assigné)
# est explicite dans le digest, avec la cause nommée par le remark backend.
ASSIGN_DIAG=$("$PY" e2e/diag_assign.py "$OUT" 2>> "$OUT/run.log" || true)
ASSIGN_DIAG_V=${ASSIGN_DIAG%%$'\t'*}
ASSIGN_DIAG_D=${ASSIGN_DIAG#*$'\t'}
case "$ASSIGN_DIAG_V" in
  PASS|FAIL|WARN) ;;
  *) ASSIGN_DIAG_V=WARN; ASSIGN_DIAG_D="diag illisible (${ASSIGN_DIAG_D:-aucune ligne})";;
esac
mark diag-driver-assign "$ASSIGN_DIAG_V" "$ASSIGN_DIAG_D"
ASSIGN_DRIVER=$("$PY" -c "import json,sys
st=json.load(open(sys.argv[1]))
vals=(st.get('ride_picked') or {}, st.get('ride_create') or {}, st.get('driver_final') or {}, st.get('rider_final') or {})
print(next((x.get('driver_id') for x in vals if x.get('driver_id') is not None), ''))" "$OUT/state.json" 2>/dev/null || true)
[ -n "$ASSIGN_DRIVER" ] || ASSIGN_DRIVER=inconnu

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
# 36 : écran de la fenêtre de confirmation ouverte par « Prise en charge du client » (le Driver n'a
# pas d'état « arrivé » : capturer avant le tap redonnait l'écran de 35, duplicate-state).
pickup_tapped=0
try_tap "$DRIVER_PKG" "Prise en charge du client" "Prise en charge" "Démarrer la course" && pickup_tapped=1
cap driver-pickup-arrived 36-driver-pickup-arrived "$DRIVER_PKG" 20

if [ "$pickup_tapped" = 1 ]; then
  # La fenêtre « Prise en charge du client » (Annuler / Confirmer) bloque tout l'écran
  # suivant (37, 35b, 42) : la confirmer et attendre qu'elle disparaisse AVANT de lire l'écran.
  dialog_confirm "$DRIVER_PKG" "Confirmer" "Annuler" 15 20; pickup_dlg=$?
  # Preuve backend juste après le tap : status=2 → le start UI n'a pas atteint le backend
  # (l'appel du Driver est en cause) ; status=3 → il l'a atteint et c'est l'app qui n'a pas
  # rafraîchi son écran. Le statut est capturé, puis re-signalé en fin de run (diag final)
  # pour survivre au tail borné des logs.
  log "prise en charge: état backend juste après « Confirmer » (2 = start UI non pris, 3 = pris)"
  ride_state
  PICKUP_START_STATUS=$(sed -n 's/^status=\([0-9][0-9]*\).*/\1/p' "$OUT/last-ride-state.txt" 2>/dev/null | head -n1)
  PICKUP_TAP_UI=$([ "${pickup_dlg:-9}" = 1 ] && echo confirmed || echo failed)
  log "diag pickup: tap_ui=$PICKUP_TAP_UI start_status=${PICKUP_START_STATUS:-inconnu} (2 = start UI jamais reçu par le backend, 3 = reçu)"
  mark pickup-backend-status WARN "tap_ui=$PICKUP_TAP_UI start_status=${PICKUP_START_STATUS:-inconnu}"
  if [ "$pickup_dlg" -ne 1 ] && wait_ui "$DRIVER_PKG" 25 "$MARK_driver_running_stop1"; then
    UI_START=1; log "start par l'UI chauffeur (écran suivant prouvé)"
    # Le marqueur peut être vu puis perdu (écran qui repart en ACTIVE) : la capture 37
    # dirait seulement « absent ». On journalise l'écran réel juste après le start borné.
    ui_screen_diag "driver: écran réel juste après le start (attendu 37)"
  else
    ui_screen_diag "driver: écran réel après la prise en charge UI (pas de bouton d'arrêt)"
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
# B2 (b) — seconde capture Driver distincte, en course, après le start. Prise sans attente elle
# redonnait l'écran de 37 octet pour octet (duplicate-state, run 45004400552) : on attend un écran
# réellement différent ; faute de quoi le point échoue avec la raison, jamais sur une copie.
bring_fg "$DRIVER_PKG" || true
if wait_screen_change "$OUT/ui/37-driver-running-stop1.xml" 30; then
  cap driver-inride-stops 35b-driver-inride "$DRIVER_PKG" 25
else
  mark driver-inride-stops FAIL "écran Driver identique à 37-driver-running-stop1 après 30s : aucun état distinct observé, pas de capture dupliquée"
fi
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
    # Garde dure : la jambe 1 est prouvée par l'API, pas par l'écran. ride_state vient
    # d'écrire last-ride-state.txt ; un arrêt déjà terminé interdit le point, quel que
    # soit ce que la capture a matché (report.py garde le pire verdict du point).
    if ! grep -q "stops_done=0" "$OUT/last-ride-state.txt" 2>/dev/null; then
      mark rider-stop1-active FAIL "état API incohérent : stops_done≠0 pendant la jambe 1 ($(tr '\n' ' ' < "$OUT/last-ride-state.txt" 2>/dev/null))"
    fi
  fi
  bring_fg "$DRIVER_PKG" || true
  next_marker="MARK_driver_stop${n}_done"
  ui_stop_ok=0
  # Bouton réel : « Confirmer l'arrêt » (écran RUNNING ; il n'existe pas de « Terminer l'arrêt »).
  # L'app peut rester sur l'écran ACTIVE un moment après le start : on attend le bouton et on
  # journalise le délai observé.
  t_btn=$SECONDS
  if wait_ui "$DRIVER_PKG" 45 '+Confirmer l.arrêt'; then
    log "bouton « Confirmer l'arrêt » présent après $((SECONDS - t_btn))s"
  else
    ui_screen_diag "driver: écran réel sans « Confirmer l'arrêt » (arrêt $n)"
  fi
  # Preuve = SERVEUR (stops_done>=n) après des taps UI sur le parent cliquable, jamais l'API de
  # complétion (run 44927908295 : aucun complete-stop reçu des taps, un seul POST = le repli).
  driver_stop_confirm_ui "$DRIVER_PKG" "$n"; stop_rc=$?
  if [ "$stop_rc" = "2" ]; then
    # Le Driver a quitté l'écran de course (accueil « En ligne » observé à l'arrêt 2) : y revenir par
    # la liste « Courses », puis un seul nouvel essai. Écran d'arrivée journalisé pour le prochain run.
    ui_screen_diag "driver: hors écran de course avant l'arrêt $n — retour par Courses"
    tap_when "$DRIVER_PKG" "Courses" >/dev/null 2>&1 || true
    ui_screen_diag "driver: après tap Courses (arrêt $n)"
    tap_when "$DRIVER_PKG" "E2E Rider" >/dev/null 2>&1 || true
    wait_ui "$DRIVER_PKG" 20 "+Confirmer l.arrêt" || true
    driver_stop_confirm_ui "$DRIVER_PKG" "$n"; stop_rc=$?
  fi
  [ "$stop_rc" = "0" ] && ui_stop_ok=1
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
  # Dialogue « Terminez votre course » : constat produit tracé (WARN), puis fermé par Back pour lire l'écran dessous.
  rider_dismiss_blocking_dialog "$RIDER_PKG"; dlg_rc=$?
  [ "$dlg_rc" = "3" ] || mark "bug-rider-stop${n}-run-dialog" WARN "dialogue « Terminez votre course » au-dessus de l'écran de course après l'arrêt $n (fermeture rc=$dlg_rc)"
  if [ "$i" -eq 0 ]; then
    # 23 : compteur « 1/2 arrêt terminé » rendu par l'app corrigée (marqueur ci-dessus).
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
  dialog_confirm "$DRIVER_PKG" "Confirmer" "Annuler" >/dev/null 2>&1 || true
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
# Écran réel : « Facture à payer CFA1440 », « Ajouter un pourboire », « Payer maintenant ».
# Les méthodes de paiement n'apparaissent qu'après ce tap : le faire avant d'attendre le 26.
# Après « Payer maintenant » : écran « Paiement » (Récapitulatif de la course, Mode de paiement,
# Pourboire, code promo, bouton « Payer »). Le sélecteur s'ouvre par l'icône cliquable de la ligne
# « Mode de paiement » ; on y choisit « Paiement en espèces » (libellé content-desc exact) PUIS
# on tape « Payer » après fermeture du sélecteur. 27 et 31 viennent après.
# diag-rider-pusher — MESURE, pas une porte. Run 44441344957 : l'app rider n'a jamais rendu la
# course (captures 26/27/29/30 = écran de composition, 35 s après backend-flow=PASS) et le tail
# CI est borné à 80 lignes, donc un simple `log` s'y perd. Ce résultat dit si l'événement pusher
# driver_assigned est arrivé dans le flux logcat, combien de lignes pusher existent au total, et
# quelle activité était au premier plan au moment de la mesure. Hors P0_DEFAULT et hors
# UI_POINTS : report.py l'affiche sans le compter comme warn/skip (test_report.py).
PUSHER_ASSIGNED=$(count_in_ci "driver_assigned" "$LOG_FILE" 2>/dev/null || echo 0)
PUSHER_TOTAL=$(grep -a -i -c "pusher" "$LOG_FILE" 2>/dev/null || true)
PUSHER_FG=$(adb shell dumpsys activity activities 2>/dev/null \
  | grep -m1 -E "mResumedActivity|topResumedActivity" \
  | grep -oE "[A-Za-z0-9_.]+/[A-Za-z0-9_.$]+" | head -1 | cut -d/ -f1)
[ -n "$PUSHER_FG" ] || PUSHER_FG=$(fg_pkg)
case "$PUSHER_ASSIGNED" in ''|*[!0-9]*) PUSHER_ASSIGNED=0 ;; esac
case "$PUSHER_TOTAL" in ''|*[!0-9]*) PUSHER_TOTAL=0 ;; esac
PUSHER_REASON="n=${PUSHER_ASSIGNED} pusher=${PUSHER_TOTAL} fg=${PUSHER_FG:-UNKNOWN}"
if [ "$PUSHER_ASSIGNED" -ge 1 ]; then
  mark diag-rider-pusher PASS "$PUSHER_REASON"
else
  mark diag-rider-pusher FAIL "$PUSHER_REASON"
fi
pay_ui=0
if try_tap "$RIDER_PKG" "Payer maintenant"; then
  if wait_ui "$RIDER_PKG" 20 '+Mode de paiement' \
     && tap_node_mode --row-control "$RIDER_PKG" "Mode de paiement"; then
    cap rider-cash-methods 26-rider-cash-methods "$RIDER_PKG" 15
    if tap_when "$RIDER_PKG" "Paiement en espèces" "Espèces" "Cash"; then
      wait_ui "$RIDER_PKG" 15 '+Payer' && tap_exact "Payer" && pay_ui=1
    else
      log "paiement: « Paiement en espèces » non trouvé dans le sélecteur"
    fi
  else
    ui_screen_diag "rider: écran « Paiement » absent ou sélecteur non ouvert"
    cap rider-cash-methods 26-rider-cash-methods "$RIDER_PKG" 5
  fi
else
  log "« Payer maintenant » non tapé"
  cap rider-cash-methods 26-rider-cash-methods "$RIDER_PKG" 5
fi

# --------------------------------------------------------------- cash flow
deadline_check cash  # budget mur : verdict partiel plutôt qu'un step qui traîne
if [ "$pay_ui" = 1 ]; then
  cap rider-cash-waiting 27-rider-cash-waiting "$RIDER_PKG" 25
  # 29 avant confirmation cash driver : le Pusher CASH_PAYMENT navigue ensuite le
  # rider vers Avis (review screen). Capturer ici garde « Course terminée » visible.
  # Même écran que 27 => paire revisited explicite dans report.py.
  cap rider-final-itinerary 29-rider-final-itinerary "$RIDER_PKG" 25
  if grep -q '^rider-cash-waiting|FAIL|' <<<"$(printf '%s\n' "${RESULTS[@]}")"; then
    api pay_rider >> "$OUT/run.log" 2>&1
  fi
else
  api pay_rider >> "$OUT/run.log" 2>&1
  bring_fg "$RIDER_PKG" || true
  cap rider-cash-waiting 27-rider-cash-waiting "$RIDER_PKG" 25
  cap rider-final-itinerary 29-rider-final-itinerary "$RIDER_PKG" 25
fi
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
  dialog_confirm "$DRIVER_PKG" "Confirmer" "Annuler" >/dev/null 2>&1 || true
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
# 43 : acceptPaymentRide ne pop pas l'écran — retour arrière explicite avant la synthèse.
adb shell input keyevent 4 >/dev/null 2>&1 || true
cap driver-summary 43-driver-summary "$DRIVER_PKG" 25
bring_fg "$RIDER_PKG" || true
# 29 a déjà été capturé avant la confirmation cash (écran 27, « Course terminée »).
# On ne recapture pas ici : le rider est maintenant sur Avis (Pusher CASH_PAYMENT).
if ! [ -s "$OUT/ui/29-rider-final-itinerary.xml" ]; then
  cap rider-final-itinerary 29-rider-final-itinerary "$RIDER_PKG" 35
fi
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
# S11: home-after = l'accueil OBSERVÉ, après le point 29. Plus de Back aveugle : sur l'accueil (racine
# Flutter) il quittait l'app (run 44927908295, capture 30 = accueil déjà affiché).
if ! printf '%s\n' "${RESULTS[@]}" | grep -q '^rider-final-itinerary|PASS|'; then
  mark rider-home-after FAIL "point 29 (rider-final-itinerary) non observé"
elif ! bring_fg "$RIDER_PKG"; then
  mark rider-home-after FAIL "Rider jamais au premier plan avant la sortie de la notation"
elif rider_leave_rating "$RIDER_PKG" && fg_is "$RIDER_PKG"; then
  cap rider-home-after 31-rider-home-after "$RIDER_PKG" 25
else
  mark rider-home-after FAIL "accueil Rider jamais observé après notation (ou app hors premier plan)"
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

# --------------------------------------------- B3 ordre des arrêts (composé vs relu)
# B3 : l'ordre composé dans l'app doit être l'ordre des ride_stops relu par GET detail.
# Seul lecteur possible en CI : l'archive de preuves est chiffrée (GPG), ni state.json
# ni captures ne sont consultables. FAIL (désaccord d'ordre) bascule le verdict ;
# WARN (course composée par l'API, ou preuve illisible) ne mesure pas l'app.
deadline_check b3-order
B3_OUT=$("$PY" e2e/b3_order.py "$OUT" 2>/dev/null || true)
B3_V=${B3_OUT%%|*}
case "$B3_V" in
  PASS|WARN|FAIL) mark bug-b3-stops-order "$B3_V" "${B3_OUT#*|}" ;;
  *) mark bug-b3-stops-order WARN "no evidence: analyse d'ordre indisponible" ;;
esac

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
kill "${SAMPLER_PID:-}" 2>/dev/null || true
# Émulateur instable ≠ échec de l'app : on le dit, le verdict reste FAIL mais étiqueté.
if emu_unstable_scan "$LOG_FILE" >> "$OUT/run.log" 2>&1; then
  [ "${EMU_CRASHES:-0}" -eq 0 ] && emu_crash_summary ""
  : > "$OUT/emulator-unstable.flag"
  mark emulator-stable WARN "ÉMULATEUR INSTABLE (ActivityManager has died / ANR in system) — échecs UI non imputables à l'app"
fi
flush_results
write_flags
"$PY" e2e/report.py "$OUT" --p0 "$P0_CSV"; report_rc=$?
# Diag final 2 : les RAISONS de CHAQUE FAIL, une ligne par point, pour qu'un extrait
# borné (80 lignes) dise *pourquoi* chaque point tombe. Source = results.txt (rapport
# agrégé de report.py, qui ajoute les FAIL « not attempted »/« no MATCH capture »),
# results-raw.txt en repli. Aucun verdict, aucun point P0 changé.
DIGEST_SRC="$OUT/results.txt"
[ -s "$DIGEST_SRC" ] || DIGEST_SRC="$OUT/results-raw.txt"
if [ -s "$DIGEST_SRC" ]; then
  log "digest FAIL: $(awk -F'|' '$2=="FAIL"{n++} END{printf "%d", n+0}' "$DIGEST_SRC") point(s) (src=$(basename "$DIGEST_SRC"))"
  # « brut= » = la ligne que cap/api a écrite AVANT l'overwrite de report.py (worst-wins
  # remplace la raison à rang égal) : sans elle on ne sait pas si cap a dit PASS ou
  # « marker absent » — c'est ce qui départage un vrai écran d'un sonde fausse.
  awk -F'|' 'FNR==NR{raw[$1]=$3; next}
             $2=="FAIL"{r=$3; gsub(/\|/,"/",r); if(length(r)>140)r=substr(r,1,140);
                        rt=($1 in raw)?raw[$1]:"-"; gsub(/\|/,"/",rt); if(length(rt)>110)rt=substr(rt,1,110);
                        printf "  FAIL %s = %s || brut=%s\n", $1, r, rt}' \
    "$OUT/results-raw.txt" "$DIGEST_SRC" | while IFS= read -r _dl; do log "$_dl"; done
fi
# Diag final 3 : pourquoi le premier plan retombe sur le launcher (classe B10). Le scan
# d'instabilité émulateur ne voit que system_server/ANR — un crash d'app y est invisible.
if [ -s "$OUT/logcat/full.log" ]; then
  CRF="$OUT/logcat/full.log"
  log "diag crash: FATAL=$(grep -ac 'FATAL EXCEPTION' "$CRF" || true) force_finish=$(grep -ac 'Force finishing activity' "$CRF" || true) galass_die=$(grep -acE 'Process sn\.galass\.(galassrider|galassdriver)[^ ]* (has died|died)' "$CRF" || true) am_crash=$(grep -ac 'am_crash' "$CRF" || true)"
  grep -a -m6 -E "FATAL EXCEPTION|Force finishing activity|Process: sn\.galass\." "$CRF" 2>/dev/null \
    | cut -c1-170 | while IFS= read -r _cl; do log "  crash:$_cl"; done
fi
# Diag final (dernière ligne du log de l'étape) : le statut pickup 37/38/39 doit rester
# lisible dans un extrait borné. Il ne change aucun verdict ni aucun point P0.
log "diag final pickup: tap_ui=${PICKUP_TAP_UI:-non-tapé} start_status=${PICKUP_START_STATUS:-inconnu} ui_start=$UI_START — 2 = start UI jamais reçu par le backend, 3 = reçu (bug de rafraîchissement Driver)"
# Re-signalé après le rapport (une ligne log, jamais un mark : le digest est déjà écrit).
log "diag-driver-assign: ${ASSIGN_DIAG_V:-inconnu} ${ASSIGN_DIAG_D:-aucun} asg=${ASSIGN_DRIVER:-inconnu}"
log "diag offer: seen=${offer_seen:-0} t_offer_to_tap=${t_offer_to_tap:-TIMEOUT} src=${RIDE_SOURCE:-unknown}"
exit $report_rc
