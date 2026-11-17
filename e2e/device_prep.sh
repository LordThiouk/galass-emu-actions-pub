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
