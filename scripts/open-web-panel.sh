#!/usr/bin/env bash
# Open the VPN Manager web panel on this PC via SSH tunnel (no domain needed).
# Double-click the Desktop "VPN Panel" launcher, or run this script from a terminal.
set -euo pipefail

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/vpn-manager"
CONFIG_FILE="$CONFIG_DIR/panel-tunnel.env"
DEFAULT_LOCAL_PORT=8787
PID_FILE="${XDG_RUNTIME_DIR:-/tmp}/vpn-manager-panel-tunnel.pid"
LOG_FILE="${XDG_RUNTIME_DIR:-/tmp}/vpn-manager-panel-tunnel.log"

notify() {
  local title="$1" body="$2"
  if command -v notify-send >/dev/null 2>&1; then
    notify-send -a "VPN Panel" "$title" "$body" || true
  fi
  printf '%s: %s\n' "$title" "$body"
}

ensure_config() {
  mkdir -p "$CONFIG_DIR"
  if [[ -f "$CONFIG_FILE" ]]; then
    return 0
  fi
  cat >"$CONFIG_FILE" <<'EOF'
# SSH tunnel settings for the admin web panel (localhost-only on the VPS).
VPS_HOST=185.230.143.223
VPS_USER=root
LOCAL_PORT=8787
REMOTE_PORT=8787

# Prefer an SSH private key (leave empty to use sshpass + password).
SSH_KEY=

# Only used when SSH_KEY is empty. Keep this file private (chmod 600).
SSHPASS=
EOF
  chmod 600 "$CONFIG_FILE"
  notify "VPN Panel" "Config created at $CONFIG_FILE — set SSHPASS or SSH_KEY, then run again."
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$CONFIG_FILE" >/dev/null 2>&1 || true
  fi
  exit 1
}

load_config() {
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
  VPS_HOST="${VPS_HOST:-185.230.143.223}"
  VPS_USER="${VPS_USER:-root}"
  LOCAL_PORT="${LOCAL_PORT:-$DEFAULT_LOCAL_PORT}"
  REMOTE_PORT="${REMOTE_PORT:-8787}"
  SSH_KEY="${SSH_KEY:-}"
  SSHPASS="${SSHPASS:-}"
}

tunnel_running() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi
  return 1
}

stop_tunnel() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "${pid:-}" ]]; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
  fi
  # Also drop any leftover forward on this port.
  pkill -f "ssh .*${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}.*${VPS_HOST}" 2>/dev/null || true
}

start_tunnel() {
  local ssh_base=(
    ssh
    -o StrictHostKeyChecking=accept-new
    -o ExitOnForwardFailure=yes
    -o ServerAliveInterval=30
    -o ServerAliveCountMax=3
    -N
    -L "${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}"
    "${VPS_USER}@${VPS_HOST}"
  )
  if [[ -n "$SSH_KEY" ]]; then
    ssh_base+=(-i "$SSH_KEY" -o IdentitiesOnly=yes)
    "${ssh_base[@]}" >>"$LOG_FILE" 2>&1 &
  elif [[ -n "$SSHPASS" ]]; then
    if ! command -v sshpass >/dev/null 2>&1; then
      notify "VPN Panel" "sshpass is missing. Install it or set SSH_KEY in $CONFIG_FILE"
      exit 1
    fi
    export SSHPASS
    sshpass -e "${ssh_base[@]}" >>"$LOG_FILE" 2>&1 &
  else
    notify "VPN Panel" "Set SSHPASS or SSH_KEY in $CONFIG_FILE"
    exit 1
  fi
  echo $! >"$PID_FILE"
  # Wait until the local port accepts connections.
  local i
  for i in $(seq 1 25); do
    if (echo >/dev/tcp/127.0.0.1/"$LOCAL_PORT") >/dev/null 2>&1; then
      return 0
    fi
    if ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      notify "VPN Panel" "Tunnel failed — see $LOG_FILE"
      rm -f "$PID_FILE"
      exit 1
    fi
    sleep 0.2
  done
  notify "VPN Panel" "Tunnel started but port $LOCAL_PORT not ready yet — opening browser anyway."
}

open_browser() {
  local url="http://127.0.0.1:${LOCAL_PORT}/"
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1 || true
  elif command -v gio >/dev/null 2>&1; then
    gio open "$url" >/dev/null 2>&1 || true
  else
    notify "VPN Panel" "Open $url in your browser"
  fi
}

main() {
  local action="${1:-open}"
  ensure_config
  load_config
  case "$action" in
    stop)
      stop_tunnel
      notify "VPN Panel" "Tunnel stopped."
      ;;
    status)
      if tunnel_running; then
        notify "VPN Panel" "Tunnel is running (pid $(cat "$PID_FILE"))."
      else
        notify "VPN Panel" "Tunnel is not running."
      fi
      ;;
    open|*)
      if ! tunnel_running; then
        stop_tunnel
        start_tunnel
        notify "VPN Panel" "Tunnel ready — opening http://127.0.0.1:${LOCAL_PORT}/"
      else
        notify "VPN Panel" "Tunnel already up — opening panel."
      fi
      open_browser
      ;;
  esac
}

main "$@"
