#!/usr/bin/env bash
# Optional standalone admin web. Prefer the panel started inside vpn-manager
# (scripts/vpn-manager-toggle.sh) when ADMIN_WEB_PASSWORD is set.
set -euo pipefail
ROOT="/media/amazon/Others/PythonProjects/vpn-manager"
cd "$ROOT"
# shellcheck disable=SC1091
source .venv/bin/activate
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec python -m app.web.main
