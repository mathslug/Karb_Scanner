#!/usr/bin/env bash
# Ephemeral GPU scan: provision the LLM backend, run the screening scan
# against it, and always tear it down afterwards. The trap makes teardown run
# even if the scan crashes; the midday cron sweep is the backstop if this
# whole script dies.
#
# SLONK_LLM_ORCH picks the backend orchestrator (default: gpu_droplet.py,
# the Ollama GPU droplet; alternative: dedicated.py, DO dedicated inference).
# The orchestrator's `create` prints LLM_BASE_URL/LLM_API_KEY/LLM_MODEL
# exports which we eval and pass through to scan.py.
# Usage: deploy/run_scan_gpu.sh
set -uo pipefail
cd /opt/slonk-arb
set -a; source /var/lib/slonk-arb/.env; set +a
export SLONK_DB=/var/lib/slonk-arb/slonk_arb.db
export UV_PYTHON_INSTALL_DIR=/opt/uv-python
export PYTHONUNBUFFERED=1

ORCH="${SLONK_LLM_ORCH:-gpu_droplet.py}"

cleanup() { /usr/local/bin/uv run "$ORCH" destroy; }
trap cleanup EXIT

env_exports=$(/usr/local/bin/uv run "$ORCH" create)
if [ $? -ne 0 ] || [ -z "$env_exports" ]; then
    echo "GPU LLM backend failed to provision; skipping today's scan."
    exit 1
fi
eval "$env_exports"

/usr/local/bin/uv run scan.py --from-db --filter "tennis,hockey,golf" --min-volume 200 \
    --model "$LLM_MODEL" \
    --db /var/lib/slonk-arb/slonk_arb.db --log-file /var/log/slonk-arb/scan.log
