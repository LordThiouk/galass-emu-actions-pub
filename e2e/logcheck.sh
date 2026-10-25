#!/usr/bin/env bash
# Deterministic logcat evidence scan (B9).
#
# "Absence of galassapp.com in the log" is only evidence when the log itself is
# evidence. An empty log, a rotated buffer, or a grep error used to yield 0 hits
# and a vacuous PASS. Callers get a machine-readable line and a distinct status:
#
#   0  log is real and clean            (prod_hits=0)
#   1  no evidence (missing/too small)  (error=missing-file|no-evidence)
#   2  scan error (grep rc=2)
#   3  forbidden prod host found        (prod_hits=N)
#
# usage: log_scan <logfile> <staging_host> [byte_floor]
set -uo pipefail

log_scan() {
  local file="$1" host="${2:-}" floor="${3:-200}"
  local bytes prod_rc host_rc prod_hits host_hits
  if [ ! -f "$file" ]; then
    printf 'error=missing-file\n'
    return 1
  fi
  bytes=$(wc -c < "$file" 2>/dev/null || echo 0)
  [ -z "$bytes" ] && bytes=0
  printf 'bytes=%s\n' "$bytes"
  if [ "$bytes" -lt "$floor" ]; then
    printf 'error=no-evidence\n'
    return 1
  fi

  prod_hits=$(grep -a -F -c "galassapp.com" "$file" 2>/dev/null)
  prod_rc=$?
  if [ "$prod_rc" -gt 1 ]; then
    printf 'error=grep-rc=%s\n' "$prod_rc"
    return 2
  fi
  printf 'prod_hits=%s\n' "${prod_hits:-0}"

  if [ -n "$host" ]; then
    host_hits=$(grep -a -F -c "$host" "$file" 2>/dev/null)
    host_rc=$?
    if [ "$host_rc" -gt 1 ]; then
      printf 'error=grep-host-rc=%s\n' "$host_rc"
      return 2
    fi
    printf 'host_hits=%s\n' "${host_hits:-0}"
  fi

  [ "${prod_hits:-0}" -gt 0 ] && return 3
  return 0
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  log_scan "$@"
  exit $?
fi
