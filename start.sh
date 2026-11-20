#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi
# Process title is set to "vpn-manager" inside app.main (setproctitle).
exec .venv/bin/python -m app.main
