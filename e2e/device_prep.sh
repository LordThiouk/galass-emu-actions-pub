# Préparation de l'appareil + stabilité de l'émulateur (harnais seulement : jamais de
# changement dans les apps Rider/Driver, leur auto-online reste tel quel — point D-1).
# Sourcé par run.sh après ui.sh (utilise adb borné, _ulog, ui_xml, xml_pkg, tap_exact).

# Motifs qui prouvent que c'est l'ÉMULATEUR qui est instable, pas l'app testée.
EMU_UNSTABLE_RE='ActivityManager has died|ANR in system|Process system_server has died|system_server has died'

_dp_log() { if declare -F _ulog >/dev/null; then _ulog "$*"; else printf '%s\n' "$*"; fi; }

# whitelist_battery pkg... : l'app driver ouvre « Let app always run in background? »
# (RequestIgnoreBatteryOptimizations) et recouvre son écran. On l'exempte AVANT le lancement.
whitelist_battery() {
  local pkg out ok
  for pkg in "$@"; do
    out=$(adb shell dumpsys deviceidle whitelist +"$pkg" 2>&1 | tr -d '\r' | head -2 | tr '\n' ' ')
    _dp_log "whitelist_battery $pkg: deviceidle -> ${out:-rc=$?}"
    for op in RUN_ANY_IN_BACKGROUND RUN_IN_BACKGROUND; do
      adb shell cmd appops set "$pkg" "$op" allow >/dev/null 2>&1
      _dp_log "whitelist_battery $pkg: appops $op allow rc=$?"
    done
    if adb shell dumpsys deviceidle whitelist 2>/dev/null | grep -q "$pkg"; then ok=oui; else ok=non; fi
    _dp_log "whitelist_battery $pkg: présent dans la whitelist deviceidle = $ok"
  done
}

# dismiss_bg_dialog : si la boîte « Let app always run in background? » est au premier plan,
# tape Allow/Autoriser (nœud trouvé dans le dump uiautomator). 0 = fermée, 1 = rien à fermer
# ou échec. Tracé dans les deux cas.
dismiss_bg_dialog() {
  ui_xml || return 1
  [ "$(xml_pkg)" = "com.android.settings" ] || return 1
  grep -qiE 'always run in background|toujours.*arrière-plan|RequestIgnoreBattery' "$UI_XML_PATH" 2>/dev/null \
    || grep -qiE 'text="(Allow|ALLOW|Autoriser|AUTORISER)"' "$UI_XML_PATH" 2>/dev/null || return 1
  _dp_log "dismiss_bg_dialog: boîte batterie détectée"
  local lab
  for lab in Allow ALLOW Autoriser AUTORISER; do
    if tap_exact "$lab" >/dev/null 2>&1; then
      _dp_log "dismiss_bg_dialog: bouton « $lab » tapé"
      sleep 1
      return 0
    fi
  done
  _dp_log "dismiss_bg_dialog: bouton Allow introuvable dans le dump"
  return 1
}

# emu_unstable_scan logfile : 0 si le logcat contient un signe d'émulateur instable.
emu_unstable_scan() {
  local f="$1"
  [ -s "$f" ] || return 1
  grep -E "$EMU_UNSTABLE_RE" "$f" 2>/dev/null | head -3 | cut -c1-200 | while IFS= read -r l; do
    _dp_log "emu_unstable: $l"
  done
  grep -qE "$EMU_UNSTABLE_RE" "$f" 2>/dev/null
}

# emu_quiet_prepare : gèle ce qui charge gms en arrière-plan (autorisé ou non : tracé).
emu_quiet_prepare() {
  local pkg rc
  for pkg in com.google.android.configupdater; do
    adb shell pm disable-user --user 0 "$pkg" >/dev/null 2>&1; rc=$?
    _dp_log "emu_quiet_prepare: pm disable-user $pkg rc=$rc"
  done
  adb shell settings put global auto_time 0 >/dev/null 2>&1 || true
  adb shell settings put global package_verifier_enable 0 >/dev/null 2>&1 || true
  adb shell settings put global verifier_verify_adb_installs 0 >/dev/null 2>&1 || true
}

# emu_wait_calm logfile quiet_sec max_sec : sys.boot_completed=1 puis AUCUN « ANR in »
# pendant quiet_sec consécutives. 0 = calme, 1 = jamais calme dans max_sec.
emu_wait_calm() {
  local f="$1" quiet="${2:-60}" max="${3:-240}" t0=$SECONDS last_anr cur
  last_anr=$(grep -c "ANR in" "$f" 2>/dev/null || echo 0)
  local calm_since=$SECONDS
  while [ $((SECONDS - t0)) -lt "$max" ]; do
    if [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" != "1" ]; then
      calm_since=$SECONDS
    fi
    cur=$(grep -c "ANR in" "$f" 2>/dev/null || echo 0)
    if [ "$cur" != "$last_anr" ]; then
      _dp_log "emu_wait_calm: nouvel ANR ($last_anr -> $cur), minuteur remis à zéro"
      last_anr=$cur; calm_since=$SECONDS
    fi
    if [ $((SECONDS - calm_since)) -ge "$quiet" ]; then
      _dp_log "emu_wait_calm: calme depuis ${quiet}s (attente $((SECONDS - t0))s)"
      return 0
    fi
    sleep 5
  done
  _dp_log "emu_wait_calm: jamais ${quiet}s sans ANR en ${max}s"
  return 1
}

# ---------------------------------------------------------------- reprise après plantage
# Un plantage d'émulateur (system_server mort, UI qui redémarre, dumps illisibles) n'est
# JAMAIS un FAIL de l'app : on attend le retour du système, on refait le calme, on relance
# l'app, on rejoue le point UNE fois. Pas de preuve d'écran = pas de PASS ; sinon
# INCONCLUSIVE emulator_crash (le verdict reste FAIL pour un point P0, rien n'est affaibli).
EMU_CRASHES=0
EMU_LOG_OFF=0
EMU_SS_PID=""
EMU_LAST_CRASH_NOTE=""
SNAP_REPLAYED=0
_dp_here() { dirname "${BASH_SOURCE[0]}"; }

# emu_checkpoint : nouvelle base de comparaison (journal, pid system_server, dumps).
emu_checkpoint() {
  EMU_LOG_OFF=$(wc -l < "${LOG_FILE:-/dev/null}" 2>/dev/null | tr -d ' ' || echo 0)
  [ -n "$EMU_LOG_OFF" ] || EMU_LOG_OFF=0
  EMU_DUMP_FAILS=0
  EMU_DUMP_FAILS_MAX=0
  EMU_SS_PID=$(adb shell pidof system_server 2>/dev/null | tr -d '\r' | awk '{print $1}')
}

# emu_crash_reason : imprime la raison et rend 0 s'il y a une preuve de plantage depuis le
# dernier checkpoint ; rend 1 sinon.
emu_crash_reason() {
  local boot pid
  if [ -s "${LOG_FILE:-}" ] && tail -n +$((EMU_LOG_OFF + 1)) "$LOG_FILE" 2>/dev/null \
       | grep -aqE "$EMU_UNSTABLE_RE"; then
    echo "journal: ActivityManager/system_server mort ou ANR in system"; return 0
  fi
  if [ "${EMU_DUMP_FAILS_MAX:-0}" -ge 2 ]; then
    echo "dumps uiautomator indisponibles x${EMU_DUMP_FAILS_MAX} d'affilée"; return 0
  fi
  boot=$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')
  if [ "$boot" != "1" ]; then
    echo "sys.boot_completed='${boot}' (interface en redémarrage)"; return 0
  fi
  pid=$(adb shell pidof system_server 2>/dev/null | tr -d '\r' | awk '{print $1}')
  if [ -n "$pid" ] && [ -n "$EMU_SS_PID" ] && [ "$pid" != "$EMU_SS_PID" ]; then
    echo "system_server redémarré (pid $EMU_SS_PID -> $pid)"; return 0
  fi
  return 1
}

emu_crash_summary() {
  python3 "$(_dp_here)/crash_summary.py" "$OUT" "${1:-}" 60 2>&1 | while IFS= read -r l; do _dp_log "$l"; done
}

emu_apply_settings() {
  adb shell settings put global window_animation_scale 0 >/dev/null 2>&1
  adb shell settings put global transition_animation_scale 0 >/dev/null 2>&1
  adb shell settings put global animator_duration_scale 0 >/dev/null 2>&1
  adb shell svc power stayon true >/dev/null 2>&1
  adb shell settings put global hide_error_dialogs 1 >/dev/null 2>&1
}

# emu_recover pkg reason : 0 = système revenu et app relancée, 1 = jamais revenu.
emu_recover() {
  local pkg="$1" reason="$2" t0=$SECONDS boot=0
  EMU_CRASHES=$((EMU_CRASHES + 1))
  EMU_LAST_CRASH_NOTE="$reason"
  _dp_log "CRASH #$EMU_CRASHES à $(date -u +%H:%M:%SZ) — émulateur: $reason (app=$pkg)"
  emu_crash_summary "$(date +%s)"
  ADB_TMO="${EMU_RECOVER_ADB_TMO:-120}" adb wait-for-device >/dev/null 2>&1
  while [ $((SECONDS - t0)) -lt "${EMU_RECOVER_BOOT_MAX:-180}" ]; do
    if [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]; then boot=1; break; fi
    sleep 3
  done
  if [ "$boot" != "1" ]; then
    _dp_log "REPRISE ÉCHEC à $(date -u +%H:%M:%SZ) — sys.boot_completed jamais revenu en $((SECONDS - t0))s"
    return 1
  fi
  _dp_log "REPRISE #$EMU_CRASHES à $(date -u +%H:%M:%SZ) — sys.boot_completed=1 après $((SECONDS - t0))s"
  # logcat meurt parfois avec system_server : le relancer pour ne pas perdre l'évidence.
  if [ -n "${LOGCAT_PID:-}" ] && ! kill -0 "$LOGCAT_PID" 2>/dev/null && [ -n "${LOG_FILE:-}" ]; then
    command adb logcat -v threadtime >> "$LOG_FILE" 2>&1 &
    LOGCAT_PID=$!
    _dp_log "REPRISE: logcat relancé (pid $LOGCAT_PID)"
  fi
  emu_apply_settings
  whitelist_battery "${DRIVER_PKG:-sn.galass.galassdriver}" "${RIDER_PKG:-sn.galass.galassrider}"
  emu_wait_calm "${LOG_FILE:-/dev/null}" "${EMU_RECOVER_QUIET:-60}" "${EMU_RECOVER_CALM_MAX:-180}" \
    || _dp_log "REPRISE: calme non atteint, on poursuit"
  launch "$pkg"
  wait_fg "$pkg" 60 || true
  emu_checkpoint
  _dp_log "REPRISE terminée à $(date -u +%H:%M:%SZ) — app $pkg relancée (total $((SECONDS - t0))s)"
  return 0
}

# snap_with_recovery <args de snap> : 0 MATCH prouvé par snap (au 1er essai ou rejoué),
# 1 échec sans preuve de plantage (vrai échec), 2 plantage d'émulateur (INCONCLUSIVE).
# Le premier argument de snap est le nom, le deuxième le paquet.
snap_with_recovery() {
  local pkg="$2" reason
  SNAP_REPLAYED=0
  if snap "$@"; then emu_checkpoint; return 0; fi
  reason=$(emu_crash_reason) || return 1
  if ! emu_recover "$pkg" "$reason"; then
    EMU_LAST_CRASH_NOTE="$reason; reprise impossible"
    return 2
  fi
  _dp_log "REJEU unique du point ($1) après la reprise"
  if snap "$@"; then
    SNAP_REPLAYED=1
    emu_checkpoint
    return 0
  fi
  if reason=$(emu_crash_reason); then
    EMU_LAST_CRASH_NOTE="replanté pendant le rejeu: $reason"
    _dp_log "CRASH pendant le rejeu de $1 : $reason"
  else
    EMU_LAST_CRASH_NOTE="écran du point non retrouvé après la reprise (état perdu au redémarrage)"
  fi
  emu_checkpoint
  return 2
}

# ---------------------------------------------------------------- diagnostic recherche Places
PLACES_LOG_OFF=0
PLACES_NET_OK=unknown
_mask_secrets() { sed -E 's/(key|token|access_token)=[^&" ]+/\1=***/g'; }

# places_diag_start : base du journal + sonde réseau hôte (HTTP) et AVD (TCP 443) vers
# maps.googleapis.com (l'app Rider y fait place/autocomplete). Sans clé : aucune donnée secrète.
places_diag_start() {
  local code body st rc
  PLACES_LOG_OFF=$(wc -l < "${LOG_FILE:-/dev/null}" 2>/dev/null | tr -d ' ' || echo 0)
  code=$(command timeout 12 curl -m 8 -s -o "${OUT:-.}/.places-probe.json" -w '%{http_code}' \
         'https://maps.googleapis.com/maps/api/place/autocomplete/json?input=E2E' 2>/dev/null || echo 000)
  st=$(grep -oE '"status" *: *"[A-Z_]+"' "${OUT:-.}/.places-probe.json" 2>/dev/null | head -1 | grep -oE '[A-Z_]{3,}"' | tr -d '"')
  rm -f "${OUT:-.}/.places-probe.json"
  adb shell "echo | toybox nc -w 3 maps.googleapis.com 443" >/dev/null 2>&1; rc=$?
  _dp_log "places: sonde hôte HTTP=${code:-000} status_api=${st:-n/a} ; sonde AVD tcp/443 rc=$rc"
  if [ "${code:-000}" != "000" ] && [ "$rc" -eq 0 ]; then PLACES_NET_OK=oui; else PLACES_NET_OK=non; fi
}

# places_field_check query : relit l'écran, journalise le texte COMPLET de chaque champ (focus
# indiqué) et rend 0 si l'un d'eux contient la requête tapée (sans accents ni casse).
places_field_check() {
  local q="$1" d rc=1
  ui_xml || { _dp_log "places saisie: dump illisible"; return 2; }
  d=$(python3 "$(_dp_here)/dump_diag.py" "$UI_XML_PATH" 2>/dev/null | _mask_secrets)
  python3 "$(_dp_here)/dump_diag.py" --contains "$UI_XML_PATH" "$q" && rc=0
  _dp_log "places saisie: requête='${q}' trouvée_dans_un_champ=$([ $rc = 0 ] && echo oui || echo NON) ${d}"
  return $rc
}

# places_wait_result label max : sonde toutes les 5 s pendant max s, SANS taper. Journalise
# si le dump est lisible, si des requêtes réseau apparaissent au logcat, et conclut.
places_wait_result() {
  local label="$1" max="${2:-20}" t0=$SECONDS lines reqs errs dumps_ok=0 dumps_ko=0 vis=non pkg fresh
  while [ $((SECONDS - t0)) -lt "$max" ]; do
    if ui_xml; then
      dumps_ok=$((dumps_ok + 1)); fresh=1; pkg=$(xml_pkg)
      grep -qF "$label" "$UI_XML_PATH" 2>/dev/null && vis=oui || vis=non
    else
      dumps_ko=$((dumps_ko + 1)); fresh=0; pkg=?; vis=non
    fi
    lines=$(tail -n +$((PLACES_LOG_OFF + 1)) "${LOG_FILE:-/dev/null}" 2>/dev/null \
            | grep -aiE 'maps\.googleapis|place/autocomplete|OVER_QUERY_LIMIT|REQUEST_DENIED|UnknownHost|Failed host lookup|SocketException|HttpException' || true)
    reqs=$(printf '%s' "$lines" | grep -aciE 'maps\.googleapis|place/autocomplete' || true)
    errs=$(printf '%s' "$lines" | grep -aciE 'OVER_QUERY_LIMIT|REQUEST_DENIED|UnknownHost|Failed host lookup|SocketException|HttpException' || true)
    _dp_log "places t=$((SECONDS - t0))s: dump=$([ $dumps_ko -gt 0 ] && [ "$pkg" = "?" ] && echo indisponible || echo lisible) pkg=${pkg} résultat_visible=$vis requêtes_logcat=${reqs:-0} erreurs_réseau=${errs:-0}"
    if [ "$fresh" = 1 ] && { [ "$dumps_ok" -eq 1 ] || [ "$vis" = "oui" ]; }; then
      _dp_log "places champ: $(python3 "$(_dp_here)/dump_diag.py" "$UI_XML_PATH" 2>/dev/null | _mask_secrets)"
    fi
    [ "$vis" = "oui" ] && break
    sleep 5
  done
  [ "$vis" = "oui" ] || { ui_xml && _dp_log "places champ (fin): $(python3 "$(_dp_here)/dump_diag.py" "$UI_XML_PATH" 2>/dev/null | _mask_secrets)"; }
  printf '%s' "${lines:-}" | head -3 | cut -c1-160 | _mask_secrets | while IFS= read -r l; do _dp_log "places logcat: $l"; done
  if [ "$vis" = "oui" ]; then
    _dp_log "places: CONCLUSION résultat « $label » visible après $((SECONDS - t0))s"
  elif [ "$PLACES_NET_OK" = "non" ]; then
    _dp_log "places: CONCLUSION panne réseau ou Places (sonde réseau KO), pas un émulateur lent"
  elif [ "$dumps_ok" -eq 0 ] || [ "$dumps_ko" -gt "$dumps_ok" ]; then
    _dp_log "places: CONCLUSION émulateur lent/instable (écran illisible $dumps_ko fois sur $((dumps_ok + dumps_ko)))"
  else
    _dp_log "places: CONCLUSION réseau OK et écran lisible, résultat absent (recherche vide ou lente, pas l'émulateur)"
  fi
  [ "$vis" = "oui" ]
}
