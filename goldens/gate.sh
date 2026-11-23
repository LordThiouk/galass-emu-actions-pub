#!/usr/bin/env bash
# Porte CI golden-diff — compare les écrans d'une branche aux goldens officiels
# de sa branche de base, sans seuil de tolérance.
#
#   goldens/gate.sh <app_dir> [--name Galass_Rider] [--goldens test/goldens/rider]
#                   [--base main] [--accept] [--out board]
#
# Règles :
#   * la référence est TOUJOURS celle de la branche de base (`origin/<base>`),
#     jamais la copie de la branche évaluée — une PR ne peut pas affaiblir sa
#     propre référence ; tout PNG golden modifié/supprimé/ajouté par la branche
#     est signalé, et un golden absent de la base compte comme nouvelle
#     référence (donc échec sans acceptation explicite) ;
#   * le harnais de comparaison (`*.dart` du dossier golden) est lui aussi relu
#     depuis la branche de base : une branche ne peut pas neutraliser sa propre
#     porte en réécrivant le test qui compare ;
#   * aucun seuil : `flutter test` compare en égalité stricte des pixels ;
#   * sortie : code 0 = VERT, 1 = ROUGE, 2 = échec de compilation/exécution ;
#     planche avant/après dans `$OUT/index.html` dans tous les cas.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${1:?usage: gate.sh <app_dir> [options]}"; shift
APP_NAME="Galass_Rider"
GOLDEN_SUBDIR="test/goldens/rider"
BASE_REF="origin/main"
ACCEPT="false"
OUT="board"

while [ $# -gt 0 ]; do
  case "$1" in
    --name) APP_NAME="$2"; shift 2 ;;
    --goldens) GOLDEN_SUBDIR="$2"; shift 2 ;;
    --base) BASE_REF="$2"; shift 2 ;;
    --accept) ACCEPT="true"; shift ;;
    --out) OUT="$2"; shift 2 ;;
    *) echo "option inconnue: $1" >&2; exit 2 ;;
  esac
done

cd "$APP_DIR" || { echo "app_dir introuvable: $APP_DIR" >&2; exit 2; }
mkdir -p "$OUT"
LOG="$OUT/flutter-golden.log"
REF="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo detached)"
SHA="$(git rev-parse --short HEAD 2>/dev/null || echo inconnu)"
BASE_SHA="$(git rev-parse --short "$BASE_REF" 2>/dev/null || echo inconnu)"

echo "== golden-diff $APP_NAME =="
echo "branche évaluée: $REF @ $SHA"
echo "référence      : $BASE_REF @ $BASE_SHA"

if ! git rev-parse --verify --quiet "$BASE_REF" >/dev/null; then
  echo "GOLDEN-GATE: ERREUR — $BASE_REF absent (fetch manquant)" | tee "$OUT/verdict.txt"
  exit 2
fi

# 1. Référence de vérité : les PNG de la branche de base.
BASE_LIST="$(git ls-tree -r --name-only "$BASE_REF" -- "$GOLDEN_SUBDIR" | grep '\.png$' || true)"
if [ -z "$BASE_LIST" ]; then
  echo "GOLDEN-GATE: ERREUR — aucun golden sur $BASE_REF dans $GOLDEN_SUBDIR" | tee "$OUT/verdict.txt"
  exit 2
fi

TAMPERED="$(git diff --name-status "$BASE_REF" -- "$GOLDEN_SUBDIR" | grep '\.png' || true)"
if [ -n "$TAMPERED" ]; then
  echo "!! goldens modifiés par la branche (référence restaurée depuis $BASE_REF) :"
  echo "$TAMPERED" | sed 's/^/   /'
fi

for f in $BASE_LIST; do
  mkdir -p "$(dirname "$f")"
  git show "$BASE_REF:$f" > "$f"
done
# Golden ajouté par la branche sans équivalent en base → nouvelle référence.
while IFS= read -r f; do
  [ -z "$f" ] && continue
  printf '%s\n' "$BASE_LIST" | grep -qxF "$f" || {
    echo "!! golden absent de $BASE_REF: $f (nouvelle référence)"
    rm -f "$f"
  }
done <<< "$(find "$GOLDEN_SUBDIR" -name '*.png' 2>/dev/null || true)"

# 1bis. Le harnais de comparaison lui-même est relu depuis la branche de base.
#       Sans ça une branche pourrait affaiblir sa propre porte (remplacer
#       `matchesGoldenFile` par une écriture, changer la surface, retirer un
#       test) et passer verte sans qu'aucune capture ne soit comparée : la
#       référence PNG serait intacte et la comparaison vide.
BASE_DART="$(git ls-tree -r --name-only "$BASE_REF" -- "$GOLDEN_SUBDIR" | grep '\.dart$' || true)"
if [ -z "$BASE_DART" ]; then
  echo "GOLDEN-GATE: ERREUR — aucun harnais (.dart) sur $BASE_REF dans $GOLDEN_SUBDIR" | tee "$OUT/verdict.txt"
  exit 2
fi
HARNESS_DIFF="$(git diff --name-status "$BASE_REF" -- "$GOLDEN_SUBDIR" | grep '\.dart' || true)"
if [ -n "$HARNESS_DIFF" ]; then
  echo "!! harnais golden modifié par la branche (version de $BASE_REF restaurée) :"
  echo "$HARNESS_DIFF" | sed 's/^/   /'
fi
for f in $BASE_DART; do
  mkdir -p "$(dirname "$f")"
  git show "$BASE_REF:$f" > "$f"
done
# Fichier de test/fixture ajouté par la branche : hors référence, ignoré.
while IFS= read -r f; do
  [ -z "$f" ] && continue
  printf '%s\n' "$BASE_DART" | grep -qxF "$f" || {
    echo "!! harnais absent de $BASE_REF: $f (ignoré)"
    rm -f "$f"
  }
done <<< "$(find "$GOLDEN_SUBDIR" -name '*.dart' 2>/dev/null || true)"

rm -rf "$GOLDEN_SUBDIR/failures"

# 2. Comparaison stricte.
set +e
flutter test "$GOLDEN_SUBDIR" --reporter expanded >"$LOG" 2>&1
TEST_EXIT=$?
set -e
grep -E '^(✅|[0-9]+:[0-9]+ \+)' "$LOG" | tail -20 || true

DIFFS="$(grep -c 'Pixel test failed' "$LOG" || true)"
MISSING="$(grep -c 'Golden file does not exist' "$LOG" || true)"
GAPS=$((DIFFS + MISSING))

# 3. Planche avant/après (toujours produite, même verte).
REF_MUTATED="false"
if [ -n "$TAMPERED$HARNESS_DIFF" ]; then REF_MUTATED="true"; fi
if [ "$GAPS" -gt 0 ] || [ "$REF_MUTATED" = "true" ]; then
  VERDICT_SRC="ROUGE"
else
  VERDICT_SRC="VERT"
fi
printf '%s\n' "$VERDICT_SRC" > "$OUT/verdict-src.txt"
python3 "$SCRIPT_DIR/board.py" \
  --failures "$GOLDEN_SUBDIR/failures" \
  --goldens "$GOLDEN_SUBDIR" \
  --out "$OUT" \
  --app "$APP_NAME" \
  --ref "$REF@$SHA" \
  --base "$BASE_REF@$BASE_SHA" \
  --log "$LOG" || true

if [ "$TEST_EXIT" -ne 0 ] && [ "$GAPS" -eq 0 ] && [ "$REF_MUTATED" = "false" ]; then
  echo "GOLDEN-GATE: ERREUR — flutter test a échoué hors comparaison golden (compilation?)" | tee "$OUT/verdict.txt"
  tail -40 "$LOG"
  exit 2
fi

# La référence et le harnais ne changent JAMAIS par la branche : les toucher est
# une tentative d'acceptation implicite, donc un échec même si la comparaison
# (faite contre la référence restaurée) est verte.
if [ "$REF_MUTATED" = "true" ]; then
  {
    echo "GOLDEN-GATE: ROUGE — référence/harnais golden modifiés par la branche :"
    [ -n "$TAMPERED" ] && echo "$TAMPERED" | sed 's/^/   /'
    [ -n "$HARNESS_DIFF" ] && echo "$HARNESS_DIFF" | sed 's/^/   /'
    echo "La comparaison a tourné contre la version de $BASE_REF (planche: $OUT/index.html)."
    echo "Une nouvelle référence se commite sur la branche de base après revue — jamais ici."
  } | tee "$OUT/verdict.txt"
  exit 1
fi

if [ "$GAPS" -gt 0 ]; then
  if [ "$ACCEPT" = "true" ]; then
    mkdir -p "$OUT/accepted-goldens"
    for img in "$GOLDEN_SUBDIR"/failures/*_testImage.png; do
      [ -e "$img" ] || continue
      base="$(basename "$img" _testImage.png)"
      cp "$img" "$OUT/accepted-goldens/$base.png"
    done
    echo "GOLDEN-GATE: ACCEPTE — $GAPS nouvelle(s) référence(s) exportée(s) dans $OUT/accepted-goldens (à committer explicitement)" | tee "$OUT/verdict.txt"
    echo "RAPPEL: la référence ne change que par un commit de test/goldens/ revu — jamais par un run." | tee -a "$OUT/verdict.txt"
    exit 0
  fi
  echo "GOLDEN-GATE: ROUGE — $GAPS écart(s) vs $BASE_REF (planche: $OUT/index.html)" | tee "$OUT/verdict.txt"
  exit 1
fi

echo "GOLDEN-GATE: VERT — aucun écart vs $BASE_REF" | tee "$OUT/verdict.txt"
exit 0
