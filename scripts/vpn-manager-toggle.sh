#!/usr/bin/env bash
# Desktop launcher: start vpn-manager if stopped, restart if already running.
set -euo pipefail

ROOT="/media/amazon/Others/PythonProjects/vpn-manager"
LOG="/tmp/vpn-manager-desktop.log"
TITLE="VPN Manager"

cd "$ROOT"

notify() {
  local msg="$1"
  if command -v notify-send >/dev/null 2>&1; then
    notify-send -a "$TITLE" "$TITLE" "$msg" || true
  fi
  printf '%s\n' "$(date '+%F %T') $msg" >>"$LOG"
}

pids="$(pgrep -x vpn-manager || true)"
if [[ -n "$pids" ]]; then
  notify "در حال ری‌استارت…"
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null || true
  sleep 1
  # Force leftover processes
  pids="$(pgrep -x vpn-manager || true)"
  if [[ -n "$pids" ]]; then
    # shellcheck disable=SC2086
    kill -9 $pids 2>/dev/null || true
    sleep 1
  fi
  nohup ./start.sh >>"$LOG" 2>&1 &
  sleep 8
  if pgrep -x vpn-manager >/dev/null; then
    notify "ری‌استارت شد و در حال اجراست."
  else
    notify "ری‌استارت ناموفق — لاگ: $LOG"
    exit 1
  fi
else
  notify "در حال راه‌اندازی…"
  nohup ./start.sh >>"$LOG" 2>&1 &
  sleep 8
  if pgrep -x vpn-manager >/dev/null; then
    notify "راه‌اندازی شد و در حال اجراست."
  else
    notify "راه‌اندازی ناموفق — لاگ: $LOG"
    exit 1
  fi
fi
