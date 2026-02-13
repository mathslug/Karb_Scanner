#!/usr/bin/env bash
# Wrapper for cron jobs — loads env vars and runs a command via uv.
# Usage: deploy/run.sh scan.py --from-db --db /var/lib/slonk-arb/slonk_arb.db
set -euo pipefail
# Support both old (kalshi-arb) and new (slonk-arb) directory layouts
if [ -d /opt/slonk-arb ]; then
    cd /opt/slonk-arb
    set -a; source /var/lib/slonk-arb/.env; set +a
    export SLONK_DB=/var/lib/slonk-arb/slonk_arb.db
else
    cd /opt/kalshi-arb
    set -a; source /var/lib/kalshi-arb/.env; set +a
    export SLONK_DB=/var/lib/kalshi-arb/kalshi_arb.db
fi
export UV_PYTHON_INSTALL_DIR=/opt/uv-python
exec /usr/local/bin/uv run "$@"
