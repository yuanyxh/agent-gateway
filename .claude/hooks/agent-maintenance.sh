#!/usr/bin/env bash
set -euo pipefail

root="."
if [ "${1:-}" = "--root" ]; then
  root="${2:?missing --root value}"
fi

profile="$root/.agent/profile.yaml"
report="$root/.agent/agent-health.md"
state="$root/.agent/.maintenance-state"

days=7
if [ -f "$profile" ]; then
  parsed="$(awk -F: '/^[[:space:]]*maintenance_stale_days:[[:space:]]*[0-9]+/{gsub(/[^0-9]/,"",$2); print $2; exit}' "$profile" 2>/dev/null || true)"
  [ -n "$parsed" ] && days="$parsed"
fi

if [ -f "$state" ] && find "$state" -mtime -1 -print -quit 2>/dev/null | grep -q .; then
  exit 0
fi

if [ ! -f "$report" ]; then
  printf '[agent-maintenance] Run steward: health report is missing.\n'
  exit 0
fi

if find "$report" -mtime +"$days" -print -quit 2>/dev/null | grep -q .; then
  printf '[agent-maintenance] Run steward: health report is older than %s days.\n' "$days"
fi

exit 0
