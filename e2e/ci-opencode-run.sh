#!/usr/bin/env bash
# Après boot de l'émulateur : installe les APK, nettoie les courses 106/23,
# puis lance le CLI opencode (modèles opencode/*-free, sans clé API) pour
# piloter adb et le harnais e2e multi-stop (run.sh / api_flow.py / ui.sh).
# Les secrets restent dans l'environnement : jamais affichés, jamais recopiés.
set -euo pipefail
set +x

OUT="${OUT:-e2e-out}"
VERDICT="${OUT}/verdict.txt"
PROMPT_FILE="${OUT}/opencode-prompt.md"
OC_LOG="${OUT}/opencode.log"
DEFAULT_MODEL="opencode/deepseek-v4.1-flash-free"
DEFAULT_CHAIN="opencode/mimo-v2.6-flash-free,opencode/glm-5.3-flash-free,opencode/minimax-m3.1-flash-free,opencode/longcat-2.5-free,opencode/iceman-free,opencode/flint-40b-free,opencode/nemotron-3.5-lightning-free,opencode/beam-free,opencode/inkling-2-small-free"
APK_RIDER="${APK_RIDER:-apk/rider/app-debug.apk}"
APK_DRIVER="${APK_DRIVER:-apk/driver/app-debug.apk}"

fail() { echo "$1" >&2; exit 1; }

log() {
  mkdir -p "$OUT"
  printf '[ci-oc %s] %s\n' "$(date -u +%H:%M:%S)" "$*" >> "$OUT/run.log"
  printf '%s\n' "$*"
}

normalize_model() {
  local m="$1"
  m="${m#"${m%%[![:space:]]*}"}"
  m="${m%"${m##*[![:space:]]}"}"
  case "$m" in
    opencode/*) printf '%s\n' "$m" ;;
    *) printf 'opencode/%s\n' "$m" ;;
  esac
}

validate_model() {
  local raw="$1" m
  [ -n "$raw" ] || fail "modèle vide (uniquement opencode/*-free)"
  case "$raw" in
    *freellm*|*FreeLLM*|*[Ff]ree[Ll][Ll][Mm]*)
      fail "modèle refusé (pas de FreeLLM) : $raw"
      ;;
  esac
  m=$(normalize_model "$raw")
  [[ "$m" =~ ^opencode/[a-z0-9][a-z0-9.-]*-free$ ]] \
    || fail "modèle refusé (uniquement opencode/*-free) : $raw"
  printf '%s\n' "$m"
}

cmd_validate_model() {
  local m
  m=$(validate_model "${1:-}")
  echo "model=$m"
}

build_chain() {
  local main="$1"
  local chain="$main"
  local m norm
  local raw="${OC_MODEL_CHAIN:-$DEFAULT_CHAIN}"
  IFS=',' read -ra extras <<< "$raw"
  for m in "${extras[@]}"; do
    [ -n "${m// /}" ] || continue
    norm=$(validate_model "$m")
    [ "$norm" != "$main" ] || continue
    case ",$chain," in
      *",$norm,"*) continue ;;
    esac
    chain="$chain,$norm"
  done
  printf '%s\n' "$chain"
}

write_prompt() {
  cat > "$PROMPT_FILE" <<'PROMPT'
# Mission e2e multi-stop (staging Galass)

Tu es le client/terminal opencode. Tu pilotes le ou les émulateurs Android
déjà démarrés, uniquement via adb, et tu exécutes le scénario multi-stop
du harnais existant. Aucune clé API, aucun appel HTTP direct à un modèle,
pas de FreeLLM.

Contexte :
- Les APK Rider et Driver sont déjà construits (apk/rider/app-debug.apk,
  apk/driver/app-debug.apk) et doivent déjà être installés sur l'émulateur.
- Les secrets (BASE, tokens) sont déjà dans l'environnement. Ne les affiche
  jamais, ne les recopie dans aucun fichier, ne les envoie nulle part.
- Outils à utiliser : `adb`, `bash e2e/run.sh`, `python3 e2e/api_flow.py`,
  `bash e2e/ui.sh`.

À faire :
1. Vérifie `adb devices` (au moins un appareil en état `device`).
2. Exécute `bash e2e/run.sh` pour le scénario multi-stop (cleanup des
   courses du passager 106 et du chauffeur 23, UI via ui.sh, API via
   api_flow.py, assertions, rapport).
3. Si une étape bloque, tu peux relancer une étape ciblée
   (`STEP=... python3 e2e/api_flow.py` ou des commandes adb / ui.sh)
   pour débloquer, sans jamais viser la prod, sans git push, sans secret
   affiché.
4. À la fin, écris le verdict dans `e2e-out/verdict.txt` :
   - première ligne exactement `PASS` ou `FAIL`
   - puis un court résumé (points P0 en échec, raison).
   Le harnais écrit aussi `e2e-out/results.txt` et `e2e-out/report.json`.
5. Termine avec le code 0 seulement si le verdict est PASS, sinon 1.

N'installe pas de modèle, n'appelle pas d'API LLM, n'utilise pas Artemis
pour un appel modèle. Tu es déjà le client opencode.
PROMPT
}

write_verdict() {
  local status="$1" rest="$2"
  mkdir -p "$OUT"
  printf '%s\n%s\n' "$status" "$rest" > "$VERDICT"
}

derive_verdict() {
  local status="" detail="" report="${OUT}/report.json"
  if [ -s "$VERDICT" ] && grep -qE '^(PASS|FAIL)$' "$VERDICT"; then
    status=$(head -n1 "$VERDICT")
    detail=$(tail -n +2 "$VERDICT" | head -n 20)
    if [ "$status" = "PASS" ]; then
      echo "$detail"
      return 0
    fi
    echo "$detail"
    return 1
  fi
  if [ -f "$report" ]; then
    status=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("verdict",""))' "$report" 2>/dev/null || true)
    detail=$(python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); print("fails=%s p0=%s/%s nonpass=%s" % (r.get("fail_count"), r.get("p0_pass"), r.get("p0_total"), ",".join(r.get("p0_nonpass") or []) or "none"))' "$report" 2>/dev/null || true)
    if [ "$status" = "PASS" ]; then
      write_verdict "PASS" "${detail:-harness PASS}"
      return 0
    fi
    write_verdict "FAIL" "harness: ${status:-inconnu} ${detail}"
    return 1
  fi
  write_verdict "FAIL" "pas de verdict PASS (voir ${OC_LOG})"
  return 1
}

append_summary() {
  local f="${GITHUB_STEP_SUMMARY:-}"
  [ -n "$f" ] || return 0
  {
    echo "## Verdict e2e multi-stop"
    echo
    if [ -f "$VERDICT" ]; then
      head -n 20 "$VERDICT"
    else
      echo "FAIL"
      echo "verdict.txt absent"
    fi
    echo
    if [ -n "${OC_MODEL_USED:-}" ]; then
      echo "modèle: $OC_MODEL_USED"
    fi
  } >> "$f"
}

prep_device() {
  local r1=1 r2=1
  command -v adb >/dev/null || fail "adb introuvable"
  log "attente du boot émulateur"
  command timeout 300 adb wait-for-device
  for _ in $(seq 1 90); do
    [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ] && break
    sleep 5
  done
  [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ] \
    || fail "émulateur non booté"
  log "installation des APK Rider + Driver"
  command timeout 600 adb install -r -t -g "$APK_RIDER" >/dev/null && r1=0 || r1=1
  command timeout 600 adb install -r -t -g "$APK_DRIVER" >/dev/null && r2=0 || r2=1
  [ "$r1" -eq 0 ] && [ "$r2" -eq 0 ] || fail "installation APK échouée (rider=$r1 driver=$r2)"
  log "APK Rider + Driver installés"
  # Cleanup courses stale du passager 106 / chauffeur 23 (sortie dans run.log seulement).
  if command -v python3 >/dev/null && [ -f e2e/api_flow.py ]; then
    log "cleanup courses stale (STEP=reset)"
    STEP=reset python3 e2e/api_flow.py >> "$OUT/run.log" 2>&1 || log "reset : poursuivi (pas de course stale ou API indisponible)"
  fi
}

run_opencode() {
  local chain models m oc_rc=1
  command -v opencode >/dev/null || fail "CLI opencode introuvable (installer opencode-ai@${OPENCODE_VERSION:-1.19.4})"
  write_prompt
  chain=$(build_chain "$OC_MODEL")
  log "chaîne de modèles: $chain"
  IFS=',' read -ra models <<< "$chain"
  for m in "${models[@]}"; do
    log "opencode run --model $m"
    OC_MODEL_USED="$m"
    : > "$OC_LOG"
    set +e
    command timeout 90m opencode run --model "$m" --title "emu-e2e-multistop" "$(cat "$PROMPT_FILE")" >>"$OC_LOG" 2>&1
    oc_rc=$?
    set -e
    # Si le harnais ou opencode a déjà tranché, on ne relance pas le e2e
    # sur un autre modèle (l'état staging aurait déjà changé).
    if [ -s "$VERDICT" ] && grep -qE '^(PASS|FAIL)$' "$VERDICT"; then
      return "$oc_rc"
    fi
    if [ -f "${OUT}/report.json" ] || [ -f "${OUT}/results.txt" ]; then
      return "$oc_rc"
    fi
    log "modèle $m sans verdict (rc=$oc_rc), essai suivant"
  done
  return "$oc_rc"
}

cmd_run() {
  mkdir -p "$OUT/screens" "$OUT/ui" "$OUT/logcat" "$OUT/artemis"
  : >> "$OUT/run.log"
  OC_MODEL=$(validate_model "${OC_MODEL:-${1:-$DEFAULT_MODEL}}")
  export OC_MODEL
  export ARTEMIS_DISABLE="${ARTEMIS_DISABLE:-1}"
  prep_device
  set +e
  run_opencode
  set -e
  if derive_verdict; then
    log "verdict=PASS modèle=${OC_MODEL_USED:-$OC_MODEL}"
    append_summary
    exit 0
  fi
  log "verdict=FAIL modèle=${OC_MODEL_USED:-$OC_MODEL}"
  append_summary
  exit 1
}

cmd="${1:-run}"
case "$cmd" in
  run) shift || true; cmd_run "$@" ;;
  validate-model) shift || true; cmd_validate_model "${1:-}" ;;
  *) fail "commande inconnue : $cmd" ;;
esac
