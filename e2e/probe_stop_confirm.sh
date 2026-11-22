#!/usr/bin/env bash
# Sonde « Confirmer l'arrêt » du Driver (multistop), hors e2e complet. Réutilise ui.sh / device_prep.sh /
# api_flow.py SANS les modifier. Étapes : fermer les courses ouvertes du rider (annulation, rien de supprimé) ;
# course multistop créée, acceptée et démarrée par l'API (le Driver n'est pas ouvert) ; ping Driver posé par l'API
# au stop 1 ; Driver ouvert seul sur l'émulateur ; tap « Confirmer l'arrêt » ; capture de la requête
# complete-stop et de la réponse serveur (lignes complete-stop / LastPing / ping_age / http= / remark=).
#   Essai A : ping API seul (aucun GPS émulateur posé) — le ping propre du Driver peut l'écraser.
#   Essai B : GPS émulateur + ping API sur le stop, attente d'un ping du Driver, puis tap (stop_prime).
# Les secrets restent dans l'environnement : jamais affichés.
set -o pipefail
set +x
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export OUT="${OUT:-e2e-out}"
mkdir -p "$OUT/ui" "$OUT/diag" "$OUT/screens"
PY="${PY:-python3}"
RIDER_PKG=sn.galass.galassrider; DRIVER_PKG=sn.galass.galassdriver
APK_DRIVER="${APK_DRIVER:-apk/driver/app-debug.apk}"
export RIDER_PKG DRIVER_PKG
. e2e/ui.sh
. e2e/device_prep.sh
eval "$(sed -n '/^write_prefs() {/,/^}$/p' e2e/run.sh)"
[ "$(type -t write_prefs)" = function ] || { echo "write_prefs introuvable dans run.sh"; exit 2; }

log() { printf '[probe %s] %s\n' "$(date -u +%H:%M:%S)" "$*" | tee -a "$OUT/probe.log"; }
api() { STEP="$1" "$PY" e2e/api_flow.py; }
ride_state() {
  STEP=ride_state "$PY" e2e/api_flow.py > "$OUT/last-ride-state.txt" 2>"$OUT/api-flow.err"
  log "ride_state: $(tr '\n' ' ' < "$OUT/last-ride-state.txt")"
}
STOP_LAT=(14.71 14.702); STOP_LNG=(-17.462 -17.455)

"$PY" e2e/base_guard.py "$BASE" || exit 2
adb emu geo fix -17.4677 14.7167 >/dev/null 2>&1 || true
ADB_TMO=600 adb install -r -t -g "$APK_DRIVER" >/dev/null 2>&1 || { log "install Driver: ÉCHEC"; exit 3; }
write_prefs "$DRIVER_PKG" "${DRIVER_TOKEN:-}" "${DRIVER_ID:-23}" "E2E Driver" "e2e.driver@galass.sn" "+221700000023"
whitelist_battery "$DRIVER_PKG" || true

log "1/ fermeture des courses ouvertes du rider"
"$PY" e2e/probe_close_rides.py 2>&1 | tee -a "$OUT/probe.log"
log "1/ course multistop créée, acceptée et démarrée par l'API (Driver fermé)"
api create 2>&1 | tee -a "$OUT/probe.log"
api accept 2>&1 | tee -a "$OUT/probe.log"
ride_state
api start 2>&1 | tee -a "$OUT/probe.log"
ride_state
grep -q "status=3" "$OUT/last-ride-state.txt" || { log "course non démarrée (status!=3) : sonde impossible"; exit 4; }

log "1/ ping Driver posé par l'API au stop 1 (moins de 500 m, âge < 15 min)"
STOP_INDEX=0 api loc_stop 2>&1 | tee -a "$OUT/probe.log"

log "2/ Driver ouvert seul, essai A : ping API seul, aucun GPS émulateur posé"
launch() { adb shell monkey -p "$1" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1; }
launch "$DRIVER_PKG"
# Démarrage à froid avec une course en cours : attendre l'écran, puis ouvrir la course si le bouton n'y est pas.
for _ in 1 2 3 4 5 6 7 8; do
  sleep 8
  ui_xml && [ "$(xml_pkg)" = "$DRIVER_PKG" ] && break
done
if ! { ui_xml && grep -q "Confirmer l'arrêt" "$UI_XML_PATH"; }; then
  log "bouton absent à l'ouverture : textes visibles = $(python3 e2e/dump_diag.py "$UI_XML_PATH" --texts 2>/dev/null | cut -c1-240)"
  tap_when "$DRIVER_PKG" "E2E Rider" "Course en cours" "En cours" >/dev/null 2>&1 || log "aucune carte de course à ouvrir"
  sleep 6
fi
STOP_LAT_SAVE=("${STOP_LAT[@]}"); STOP_LNG_SAVE=("${STOP_LNG[@]}")
STOP_LAT=(); STOP_LNG=()          # stop_prime ne fait rien : essai A = ping API seul
OFFSET_A=$SECONDS
driver_stop_confirm_ui "$DRIVER_PKG" 1; rc_a=$?
log "essai A rc=$rc_a"
if [ "$rc_a" -ne 0 ]; then
  log "2/ essai B : GPS émulateur + ping API sur le stop, attente d'un ping du Driver, puis tap"
  STOP_LAT=("${STOP_LAT_SAVE[@]}"); STOP_LNG=("${STOP_LNG_SAVE[@]}")
  driver_stop_confirm_ui "$DRIVER_PKG" 1; rc_b=$?
  log "essai B rc=$rc_b"
fi
ride_state

# Résultat : seulement les lignes décisives (aucun jeton : app_logcat_since les masque).
{
  echo "== lignes décisives (journal de la sonde)"
  grep -a -E 'complete-stop|LastPing|ping_age|http=|remark=|outcome=' "$OUT/run.log" 2>/dev/null | sed -E 's/^\[ui [0-9:]+\] //' | sort -u
  echo "== état serveur final"; cat "$OUT/last-ride-state.txt" 2>/dev/null
} > "$OUT/probe-result.txt"
cat "$OUT/probe-result.txt"
