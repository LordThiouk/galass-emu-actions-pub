# Shared sanitized smoke log (sourced by run.sh and ui.sh).
# Raw lines stay in $OUT/run.log (encrypted evidence); every copy that reaches
# stderr or e2e/smoke.log is masked first by e2e/sanitize.py.
SMOKE_LOG="${SMOKE_LOG:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/smoke.log}"
SMOKE_SANITIZER="${SMOKE_SANITIZER:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/sanitize.py}"
export SMOKE_LOG SMOKE_SANITIZER

smoke_emit() {
  printf '%s\n' "$*" \
    | "${PY:-python3}" "$SMOKE_SANITIZER" --append "$SMOKE_LOG" --stderr >/dev/null
}

# smoke_emit_file FILE : publie le stderr d'une étape (cp. api-flow.err) via le
# sanitizer, ignore les lignes vides, puis tronque le fichier. Couvre les sorties
# anticipées (watchdog, deadline) puisque run.sh l'appelle aussi dans flush_results.
smoke_emit_file() {
  local file="${1:-}"
  [ -n "$file" ] || return 0
  if [ -s "$file" ]; then
    grep -v '^[[:space:]]*$' "$file" \
      | "${PY:-python3}" "$SMOKE_SANITIZER" --append "$SMOKE_LOG" --stderr >/dev/null || true
  fi
  : > "$file"
}
export -f smoke_emit smoke_emit_file
