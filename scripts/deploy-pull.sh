#!/usr/bin/env bash
# On the VPS: pull latest main and restart vpn-manager.
# Keeps .env, data/ DB, and Telethon sessions (gitignored).
set -euo pipefail
cd "$(dirname "$0")/.."
git fetch origin
git checkout -f main
git pull --ff-only origin main
if [[ -d .venv ]]; then
  .venv/bin/pip install -q -r requirements.txt
fi
systemctl restart vpn-manager
sleep 3
systemctl is-active vpn-manager
systemctl status vpn-manager --no-pager -l | head -20
