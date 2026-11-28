#!/usr/bin/env bash
# Après boot de l'émulateur : installe les APK, nettoie les courses test (>900000),
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

# ---------------------------------------------------------------- budget (P2)
# Le step "E2E multistop" du workflow a timeout-minutes: 100. Le watchdog du
# harnais (RUN_DEADLINE_MIN=90, fourni par le workflow) ne démarre qu'À
# l'INTÉRIEUR de e2e/run.sh : le temps déjà écoulé AVANT le harnais (boot de
# l'émulateur par l'action + prep_device) n'était pas décompté. Au-delà de
# 100 min, le step était coupé AVANT le verdict (run 43956819722 : 0
# results.txt, 0 report.json). Le budget EFFECTIF du harnais est donc borné
# par le temps restant de l'étape, et le temps déjà consommé est journalisé.
STEP_BUDGET_MIN="${E2E_STEP_BUDGET_MIN:-100}"          # timeout-minutes du step CI
PRE_SCRIPT_MIN="${E2E_PRE_SCRIPT_MIN:-15}"             # PLANCHER du temps pré-script
PRE_SCRIPT_ALLOW_S="${E2E_PRE_SCRIPT_ALLOW_S:-120}"    # marge post-boot de l'action
FLUSH_MARGIN_MIN="${E2E_FLUSH_MARGIN_MIN:-2}"          # flush report.py + verdict.txt
SAFETY_MARGIN_MIN="${E2E_SAFETY_MARGIN_MIN:-3}"       # slack final avant la coupure

# ------------------------------------------------- jambe de réparation (P3)
# Plafond GLOBAL de la jambe opencode (minutes). Sans plafond, la boucle
# `timeout 90m` sur 10 modèles pouvait tenir 900 min face à un step de 100.
REPAIR_BUDGET_MIN="${OC_REPAIR_BUDGET_MIN:-15}"
# Timeout par modèle (historique 90 min) : jamais dépassé par le budget global.
REPAIR_MODEL_MAX_S="${OC_REPAIR_MODEL_MAX_S:-5400}"
# Grace après SIGTERM avant le SIGKILL. Sans elle, un enfant qui IGNORE
# SIGTERM bloque le script POUR TOUJOURS : GNU timeout attend le délai
# indéfiniment (mesuré : budget 6 s → 90 s de blocage, script jamais rendu,
# derive_verdict jamais atteint). Le plafond doit être DUR.
REPAIR_KILL_GRACE_S="${OC_REPAIR_KILL_GRACE_S:-10}"

STEP_BUDGET_S=$(( STEP_BUDGET_MIN * 60 ))
FLUSH_S=$(( FLUSH_MARGIN_MIN * 60 ))
SAFETY_S=$(( SAFETY_MARGIN_MIN * 60 ))
if [ -n "${OC_REPAIR_BUDGET_S:-}" ]; then
  REPAIR_BUDGET_S="${OC_REPAIR_BUDGET_S}"
else
  REPAIR_BUDGET_S=$(( REPAIR_BUDGET_MIN * 60 ))
fi

# Budget du harnais fourni par le workflow : PLAFOND, jamais ignoré.
RUN_DEADLINE_CAP="${RUN_DEADLINE_MIN:-90}"

# Horloge : le temps se compte depuis le DÉBUT du script.
SCRIPT_START=$SECONDS
# Le temps PRÉ-SCRIPT est un PLANCHER (R1) : l'uptime de l'émulateur ne voit
# pas le setup de l'action reactivecircus/android-emulator-runner
# (téléchargement de l'image système ~1,4 Go + création de l'AVD), invisible
# depuis le script. Sans plancher, un step déjà consommé de 12 min gardait un
# budget de 90 min → le watchdog tirait son flush APRÈS la coupure de 100 min
# (run 43956819722 : 0 results.txt, 0 report.json).
PRE_SCRIPT_FLOOR_S=$(( PRE_SCRIPT_MIN * 60 ))
PRE_SCRIPT_UP_S=0         # uptime de l'émulateur MESURÉ (s)
PRE_SCRIPT_MEASURED_S=0   # uptime mesuré + marge post-boot de l'action
PRE_SCRIPT_S="$PRE_SCRIPT_FLOOR_S"
PRE_SCRIPT_SRC="plancher"
PREP_ELAPSED_S=0
EFFECTIVE_DEADLINE_MIN=0
STEP_REMAINING_S=0
OC_MODEL=""
OC_MODEL_OK=0

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
  apk/driver/app-debug.apk). C'est `bash e2e/run.sh` qui les installe sur
  l'émulateur (étape `apk-install`) : si tu pilots l'UI sans run.sh,
  installe-les toi-même (adb install -r -t -g apk/rider/app-debug.apk).
- Les secrets (BASE, tokens) sont déjà dans l'environnement. Ne les affiche
  jamais, ne les recopie dans aucun fichier, ne les envoie nulle part.
- Outils à utiliser : `adb`, `bash e2e/run.sh`, `python3 e2e/api_flow.py`,
  `bash e2e/ui.sh`.

À faire :
1. Vérifie `adb devices` (au moins un appareil en état `device`).
2. Exécute `bash e2e/run.sh` pour le scénario multi-stop (cleanup des
   courses du passager/chauffeur de test (RIDER_ID/DRIVER_ID > 900000), UI via ui.sh, API via
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

# Décide PASS/FAIL à partir de report.json.
#
# report.py émet `verdict` ∈ {PASS, PASS_WITH_WARNINGS, FAIL}. En CI,
# ARTEMIS_DISABLE=1 force les points NON-P0 (llm-vision, artemis-smoke) en
# SKIP : report.py rend alors PASS_WITH_WARNINGS même à 30/30 P0 PASS.
# Tester l'égalité exacte sur la chaîne "PASS" faisait donc sortir le step
# en ROUGE sur un run parfait. Le gate est la SUBSTANCE du rapport, pas le
# mot :
#   PASS <=> verdict != FAIL ET fail_count == 0 ET p0_total > 0
#            ET p0_pass == p0_total ET p0_nonpass vide
# R3 : un rapport qui porte `verdict: "FAIL"` est un FAIL, quelle que soit sa
# substance (rapport forgé/corrompu : des compteurs propres ne doivent pas
# produire un PASS). PASS / PASS_WITH_WARNINGS : la substance décide (règle
# inchangée, aucun seuil P0 modifié).
# Toute autre situation (échec, P0 non-PASS, rapport illisible/absent) est
# un FAIL. Sortie sur stdout : "STATUS<TAB>RESUME<TAB>RAISON".
read_report_decision() {
  python3 -c '
import json, sys

path = sys.argv[1]
UNKNOWN = "fails=? p0=?/? nonpass=?"


def refuse(reason):
    print("FAIL\t%s\t%s" % (UNKNOWN, reason))
    sys.exit(0)


try:
    with open(path, encoding="utf-8") as fh:
        report = json.load(fh)
except (OSError, ValueError) as exc:
    refuse("report illisible: %s" % exc)
if not isinstance(report, dict):
    refuse("report illisible: racine JSON non-objet")


def count(key):
    value = report.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


fails = count("fail_count")
p0_total = count("p0_total")
p0_pass = count("p0_pass")
nonpass = report.get("p0_nonpass") or []
if not isinstance(nonpass, list):
    nonpass = [nonpass]
nonpass = [str(item) for item in nonpass]

reasons = []
verdict = report.get("verdict")
if isinstance(verdict, str) and verdict.strip().upper() == "FAIL":
    reasons.append("rapport: verdict=FAIL")
if fails is None:
    reasons.append("fail_count absent/illisible")
elif fails != 0:
    reasons.append("fail_count=%d" % fails)
if p0_total is None:
    reasons.append("p0_total absent/illisible")
elif p0_total <= 0:
    reasons.append("p0_total=%d" % p0_total)
if p0_pass is None:
    reasons.append("p0_pass absent/illisible")
elif p0_total is not None and p0_pass != p0_total:
    reasons.append("p0_pass=%d != p0_total=%d" % (p0_pass, p0_total))
if nonpass:
    reasons.append("p0_nonpass=%s" % ",".join(nonpass))

resume = "verdict=%s fails=%s p0=%s/%s nonpass=%s" % (
    report.get("verdict") or "inconnu",
    fails if fails is not None else "?",
    p0_pass if p0_pass is not None else "?",
    p0_total if p0_total is not None else "?",
    ",".join(nonpass) or "none")
print("%s\t%s\t%s" % ("FAIL" if reasons else "PASS", resume,
                      "; ".join(reasons) or "substance OK"))
' "$1" 2>/dev/null || true
}

derive_verdict() {
  local status="" detail="" report="${OUT}/report.json"
  # Branche 1 (inchangée) : verdict.txt porte déjà PASS|FAIL en 1re ligne.
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
  # Branche 2 : report.json — la SUBSTANCE décide (cf. read_report_decision).
  if [ -f "$report" ]; then
    local decision="" rest="" resume="" reason=""
    decision=$(read_report_decision "$report")
    if [ -n "$decision" ]; then
      status=${decision%%$'\t'*}
      rest=${decision#*$'\t'}
      case "$rest" in
        *$'\t'*) resume=${rest%%$'\t'*}; reason=${rest#*$'\t'} ;;
        *) resume=$rest; reason="raison illisible" ;;
      esac
    else
      status="FAIL"
      resume="fails=? p0=?/? nonpass=?"
      reason="lecture de ${report} impossible (python3 indisponible ?)"
    fi
    detail="harness: ${resume} (${reason})"
    if [ "$status" = "PASS" ]; then
      write_verdict "PASS" "$detail"
      echo "$detail"
      return 0
    fi
    write_verdict "FAIL" "$detail"
    echo "$detail"
    return 1
  fi
  write_verdict "FAIL" "pas de verdict PASS (voir ${OC_LOG})"
  return 1
}

append_summary() {
  local f="${GITHUB_STEP_SUMMARY:-}"
  [ -n "$f" ] || return 0
  # R5 : la publication du résumé ne doit JAMAIS faire sortir le step en
  # ROUGE quand le verdict est PASS. Un GITHUB_STEP_SUMMARY non inscriptible
  # (droits, quota, montage) doit être JOURNALISÉ, pas fatal : la redirection
  # échoue AVEC le `||`, donc errexit ne s'enclenche pas.
  local body
  body=$(
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
  )
  printf '%s\n' "$body" 2>/dev/null >> "$f" \
    || log "append_summary: GITHUB_STEP_SUMMARY non inscriptible — résumé non publié (verdict inchangé)"
  return 0
}

update_step_remaining() {
  local s=$(( STEP_BUDGET_S - PRE_SCRIPT_S - (SECONDS - SCRIPT_START) - FLUSH_S - SAFETY_S ))
  if [ "$s" -lt 0 ]; then s=0; fi
  STEP_REMAINING_S="$s"
}

# Temps déjà écoulé AVANT ce script : l'action android-emulator-runner boot
# l'émulateur PUIS lance le script. On l'estime par l'uptime de l'émulateur
# (mesuré, + la marge post-boot de l'action).
#
# R1 : cette MESURE est une borne BASSE, pas la vérité. L'uptime ne voit pas
# le setup de l'action (image système ~1,4 Go + création de l'AVD) : il
# sous-estime le temps réellement consommé. PRE_SCRIPT_MIN est donc un
# PLANCHER, jamais un repli : le temps retenu vaut
#   max(uptime mesuré + marge, PRE_SCRIPT_MIN × 60).
# Les DEUX composantes (uptime mesuré ET plancher appliqué) sont journalisées
# pour que le budget soit auditable.
measure_pre_script() {
  local up="" secs=0
  up=$(adb shell cat /proc/uptime 2>/dev/null | tr -d '\r' | awk '{print $1}' || true)
  case "$up" in
    ''|*[!0-9.]*) ;;
    *)
      secs="${up%.*}"
      case "$secs" in ''|*[!0-9]*) secs=0 ;; esac
      PRE_SCRIPT_UP_S=$(( 10#$secs ))
      ;;
  esac
  PRE_SCRIPT_MEASURED_S=$(( PRE_SCRIPT_UP_S + PRE_SCRIPT_ALLOW_S ))
  if [ "$PRE_SCRIPT_MEASURED_S" -lt "$PRE_SCRIPT_FLOOR_S" ]; then
    PRE_SCRIPT_S="$PRE_SCRIPT_FLOOR_S"
    PRE_SCRIPT_SRC="plancher"
  else
    PRE_SCRIPT_S="$PRE_SCRIPT_MEASURED_S"
    PRE_SCRIPT_SRC="uptime"
  fi
  log "pré-script: uptime=${PRE_SCRIPT_UP_S}s +marge=${PRE_SCRIPT_ALLOW_S}s = mesuré ${PRE_SCRIPT_MEASURED_S}s ; plancher ${PRE_SCRIPT_FLOOR_S}s → retenu ${PRE_SCRIPT_S}s (${PRE_SCRIPT_SRC})"
}

# Budget EFFECTIF du harnais = min(budget du workflow, temps restant de
# l'étape après le temps déjà consommé). Le budget du workflow (90) reste
# pris en compte : c'est le plafond, jamais la valeur inconditionnelle.
compute_effective_budget() {
  local avail eff
  avail=$(( STEP_BUDGET_S - PRE_SCRIPT_S - PREP_ELAPSED_S - FLUSH_S - SAFETY_S ))
  eff=0
  if [ "$avail" -ge 60 ]; then
    eff=$(( avail / 60 ))
    if [ "$eff" -gt "$RUN_DEADLINE_CAP" ]; then eff="$RUN_DEADLINE_CAP"; fi
    if [ "$eff" -lt 1 ]; then eff=1; fi
  fi
  EFFECTIVE_DEADLINE_MIN="$eff"
  log "budget: step=${STEP_BUDGET_MIN}min pré-script=${PRE_SCRIPT_S}s (uptime=${PRE_SCRIPT_UP_S}s+marge=${PRE_SCRIPT_ALLOW_S}s=${PRE_SCRIPT_MEASURED_S}s, plancher=${PRE_SCRIPT_FLOOR_S}s, source=${PRE_SCRIPT_SRC}) prép=${PREP_ELAPSED_S}s flush=${FLUSH_S}s safety=${SAFETY_S}s → budget harnais=${eff}min (RUN_DEADLINE_MIN=${RUN_DEADLINE_CAP} non dépassé)"
  if [ "$eff" -gt 0 ]; then
    export RUN_DEADLINE_MIN="$eff"
  fi
}

prep_device() {
  # AEP (P2) : prep_device ne doit JAMAIS tuer le script sans verdict.
  # Le harnais (e2e/run.sh) est l'autorité : il rend un rapport (même
  # partiel) que derive_verdict transforme en verdict. Toute anomalie de
  # prep est donc journalisée, pas fatale.
  command -v adb >/dev/null || { log "adb introuvable — le harnais rendra le verdict"; return 0; }
  log "attente du boot émulateur"
  command timeout 300 adb wait-for-device || log "wait-for-device: rc=$? (le harnais tranchera)"
  measure_pre_script
  for _ in $(seq 1 90); do
    if [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]; then
      break
    fi
    sleep 5
  done
  if [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" != "1" ]; then
    log "émulateur non booté — le harnais rendra le verdict (emulator-boot FAIL)"
    return 0
  fi
  # L'installation des APK est FAITE par e2e/run.sh (étape `apk-install`,
  # lignes install -r -t -g des deux mêmes APK) : la réinstaller ici
  # consommait la marge du step sans rien prouver, et son échec tuait le
  # script SANS verdict. Le harnais marque `apk-install` PASS/FAIL.
  log "émulateur prêt (installation des APK à la charge de e2e/run.sh : apk-install)"
  # Cleanup courses stale des comptes de test (RIDER_ID/DRIVER_ID > 900000).
  if command -v python3 >/dev/null && [ -f e2e/api_flow.py ]; then
    log "cleanup courses stale (STEP=reset)"
    STEP=reset python3 e2e/api_flow.py >> "$OUT/run.log" 2>&1 || log "reset : poursuivi (pas de course stale ou API indisponible)"
  fi
}

run_opencode() {
  local chain models m oc_rc=1 deadline_s=0 remaining_s=0 per_model_s=0 budget_s=0
  command -v opencode >/dev/null || { log "CLI opencode introuvable (installer opencode-ai@${OPENCODE_VERSION:-1.19.4}) — jambe de réparation impossible"; return 1; }
  if [ "${OC_MODEL_OK:-0}" != "1" ]; then
    log "modèle invalide — jambe de réparation impossible"
    return 1
  fi
  write_prompt
  chain=$(build_chain "$OC_MODEL")
  log "chaîne de modèles: $chain"
  # P3 : plafond GLOBAL de la jambe de réparation. Il est borné par le
  # budget global ET par le temps restant de l'étape : quoi qu'il arrive,
  # le verdict part avant la coupure du step.
  update_step_remaining
  budget_s="$REPAIR_BUDGET_S"
  if [ "$STEP_REMAINING_S" -lt "$budget_s" ]; then budget_s="$STEP_REMAINING_S"; fi
  if [ "$budget_s" -lt 0 ]; then budget_s=0; fi
  deadline_s=$(( SECONDS + budget_s ))
  log "jambe de réparation: budget global=${REPAIR_BUDGET_S}s ($(( REPAIR_BUDGET_S / 60 ))min) effectif=${budget_s}s étape restante=${STEP_REMAINING_S}s"
  if [ "$budget_s" -le 0 ]; then
    log "jambe de réparation: budget épuisé — arrêt propre avant le premier modèle"
    return 0
  fi
  IFS=',' read -ra models <<< "$chain"
  for m in "${models[@]}"; do
    remaining_s=$(( deadline_s - SECONDS ))
    if [ "$remaining_s" -le 0 ]; then
      # Arrêt PROPRE entre deux modèles : on ne tue rien, on cesse de
      # lancer des modèles et on rend la main à derive_verdict.
      log "jambe de réparation: budget global épuisé — arrêt propre entre deux modèles"
      break
    fi
    per_model_s="$remaining_s"
    if [ "$per_model_s" -gt "$REPAIR_MODEL_MAX_S" ]; then per_model_s="$REPAIR_MODEL_MAX_S"; fi
    log "opencode run --model $m (timeout ${per_model_s}s + kill après ${REPAIR_KILL_GRACE_S}s, budget restant ${remaining_s}s)"
    OC_MODEL_USED="$m"
    : > "$OC_LOG"
    set +e
    # `-k` rend le plafond DUR : l'enfant est tué (SIGKILL) s'il ignore
    # SIGTERM. Sans `-k`, GNU timeout attend le délai INDÉFINIMENT quand
    # l'enfant ignore SIGTERM (mesuré : budget 6 s → 90 s de blocage, script
    # jamais rendu, derive_verdict jamais atteint).
    command timeout -k "${REPAIR_KILL_GRACE_S}s" "${per_model_s}s" opencode run --model "$m" --title "emu-e2e-multistop" "$(cat "$PROMPT_FILE")" >>"$OC_LOG" 2>&1
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

harness_has_verdict() {
  if [ -s "$VERDICT" ] && grep -qE '^(PASS|FAIL)$' "$VERDICT"; then
    return 0
  fi
  [ -f "${OUT}/report.json" ] || [ -f "${OUT}/results.txt" ]
}

cmd_run() {
  local rc=0 eff=0
  mkdir -p "$OUT/screens" "$OUT/ui" "$OUT/logcat" "$OUT/artemis"
  : >> "$OUT/run.log"
  # Un modèle refusé ne doit pas tuer le script sans verdict : le harnais
  # direct ne dépend pas du modèle, seule la jambe de réparation en dépend.
  if ! OC_MODEL=$(validate_model "${OC_MODEL:-${1:-$DEFAULT_MODEL}}" 2>&1); then
    log "modèle refusé : ${OC_MODEL} — le harnais direct tourne quand même"
    OC_MODEL=""
    OC_MODEL_OK=0
  else
    export OC_MODEL
    OC_MODEL_OK=1
  fi
  export ARTEMIS_DISABLE="${ARTEMIS_DISABLE:-1}"
  prep_device
  PREP_ELAPSED_S=$(( SECONDS - SCRIPT_START ))
  compute_effective_budget
  eff="$EFFECTIVE_DEADLINE_MIN"
  # Run 43956819722 : l'agent opencode n'est qu'un wrapper autour de
  # `bash e2e/run.sh`, harnais déterministe et autosuffisant (budget interne
  # `deadline_check` + RUN_DEADLINE_MIN). Lancer l'agent d'abord coûtait
  # 90 min × 9 modèles face à un step de 100 min : le secours placé après
  # n'était jamais atteint. On exécute donc le harnais direct en premier ; il
  # rend toujours la main en produisant results.txt/report.json, éventuellement
  # partiels. Aucune assertion n'est relâchée : derive_verdict lit toujours
  # report.json/verdict.txt que report.py produit.
  if [ "$eff" -gt 0 ]; then
    # Papa 10:20 : SCENARIO=lot → lot_flow.sh (P0_LOT) si présent. Sinon run.sh.
    HARNESS="e2e/run.sh"
    if [ "${SCENARIO:-multistop}" = "lot" ] && [ -f e2e/lot_flow.sh ]; then
      HARNESS="e2e/lot_flow.sh"
    elif [ "${SCENARIO:-multistop}" = "lot" ]; then
      log "SCENARIO=lot mais e2e/lot_flow.sh absent — repli run.sh"
    fi
    log "harnais déterministe direct (bash $HARNESS scenario=${SCENARIO:-multistop}) — budget ${eff}min"
    set +e
    bash "$HARNESS"
    rc=$?
    set -e
    log "harnais direct: rc=$rc"
    if [ "$rc" -eq 124 ]; then
      log "harnais: deadline interne atteinte (rc=124) — verdict partiel dérivé"
    fi
  else
    log "budget épuisé avant le harnais — pas de harnais, verdict dérivé des artefacts présents"
    rc=124
  fi
  update_step_remaining
  log "budget: budget effectif=${eff}min, temps déjà consommé=$(( PRE_SCRIPT_S + PREP_ELAPSED_S ))s (pré-script ${PRE_SCRIPT_S}s + prép ${PREP_ELAPSED_S}s), étape restante=${STEP_REMAINING_S}s"
  # Jambe de réparation : uniquement si le harnais direct n'a rendu ni verdict
  # ni résultats. Plafond global (P3) : elle ne peut pas brûler le step.
  if ! harness_has_verdict; then
    log "harnais sans verdict — jambe de réparation opencode"
    set +e
    # `|| true` est obligatoire et non décoratif : run_opencode() réactive
    # `set -e` à chaque tour de boucle, ce qui écrase le `set +e` ci-dessus et
    # fait sortir le script au retour non-zéro de la fonction (mesuré : rc=124,
    # rien après la boucle). Une liste `||` place l'appel dans un contexte où
    # errexit est ignoré — le rc d'opencode n'a jamais servi, le verdict vient
    # des artefacts.
    run_opencode || true
    set -e
  else
    log "harnais direct a rendu un verdict — agent opencode non appelé"
  fi
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
