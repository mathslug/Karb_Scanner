#!/usr/bin/env bash
# Create and provision a new slonk-arb droplet from scratch.
#
# Requires: doctl (authenticated), SSH key on DO account.
#
# .env is created by GitHub Actions deploy (ANTHROPIC_KEY + SLONK_ADMIN_PASS).
# The script prompts you to push to main to trigger the deploy.
#
# Usage:
#   bash deploy/rebuild.sh [--db path/to/backup.db]
#
set -euo pipefail

DOMAIN=karb.mathslug.com
SSH_USER=almalinux
SENTINEL=/var/lib/slonk-arb/.cloud-init-done
DO_PROJECT=SlonkN
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Parse args ────────────────────────────────────────────────────────────
DB_FILE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --db) DB_FILE="$2"; shift 2 ;;
        *)    echo "Unknown arg: $1"; exit 1 ;;
    esac
done

if [[ -n "$DB_FILE" && ! -f "$DB_FILE" ]]; then
    echo "ERROR: DB file not found: $DB_FILE"
    exit 1
fi

# ── Phase 1: Create droplet ──────────────────────────────────────────────
echo "==> Creating droplet..."
DROPLET_INFO=$(doctl compute droplet create slonk-arb \
    --image almalinux-10-x64 \
    --size s-1vcpu-512mb-10gb \
    --region nyc1 \
    --ssh-keys "$(doctl compute ssh-key list --format ID --no-header | head -1)" \
    --user-data-file "$SCRIPT_DIR/cloud-init.yml" \
    --wait \
    --format ID,PublicIPv4 \
    --no-header 2>&1)

DROPLET_ID=$(echo "$DROPLET_INFO" | awk '{print $1}')
IP=$(echo "$DROPLET_INFO" | awk '{print $2}')

if [[ -z "$IP" || "$IP" == "Error"* ]]; then
    echo "ERROR: Failed to create droplet"
    echo "$DROPLET_INFO"
    exit 1
fi

echo "    Droplet $DROPLET_ID created at $IP"

PROJECT_ID=$(doctl projects list --format ID,Name --no-header 2>/dev/null | grep "$DO_PROJECT" | awk '{print $1}')
if [[ -n "$PROJECT_ID" ]]; then
    doctl projects resources assign "$PROJECT_ID" --resource="do:droplet:$DROPLET_ID" >/dev/null 2>&1
    echo "    Assigned to project $DO_PROJECT"
fi

# ── Phase 2: Wait for cloud-init ─────────────────────────────────────────
echo ""
echo "==> Waiting for cloud-init to finish on $IP..."
while true; do
    if ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new \
        "$SSH_USER@$IP" "test -f $SENTINEL" 2>/dev/null; then
        echo "    Cloud-init complete."
        break
    fi
    echo "    Not ready yet (retrying in 15s)"
    sleep 15
done

# ── Phase 3: Push DB (optional) ──────────────────────────────────────────
if [[ -n "$DB_FILE" ]]; then
    echo ""
    echo "==> Pushing DB: $DB_FILE"
    scp "$DB_FILE" "$SSH_USER@$IP:~/slonk_arb.db"
    ssh "$SSH_USER@$IP" "
        sudo cp ~/slonk_arb.db /var/lib/slonk-arb/slonk_arb.db
        sudo chown slonk:slonk /var/lib/slonk-arb/slonk_arb.db
        rm ~/slonk_arb.db
    "
    echo "    Done."
fi

# ── Phase 4: DNS ────────────────────────────────────────────────────────
echo ""
echo "==> Checking DNS for $DOMAIN -> $IP"
RESOLVED=$(dig +short "$DOMAIN" 2>/dev/null | tail -1)
if [[ "$RESOLVED" != "$IP" ]]; then
    echo "    DNS currently resolves to: ${RESOLVED:-<nothing>}"
    echo "    Update the A record for $DOMAIN to $IP in Namecheap, then press Enter."
    read -r
    echo "    Waiting for DNS propagation..."
    DNS_WAIT=15
    while true; do
        RESOLVED=$(dig +short "$DOMAIN" 2>/dev/null | tail -1)
        if [[ "$RESOLVED" == "$IP" ]]; then
            echo "    DNS resolved!"
            break
        fi
        echo "    Still resolving to: ${RESOLVED:-<nothing>} (retrying in ${DNS_WAIT}s)"
        sleep "$DNS_WAIT"
        DNS_WAIT=$(( DNS_WAIT * 2 > 120 ? 120 : DNS_WAIT * 2 ))
    done
else
    echo "    DNS already correct."
fi

# ── Phase 5: SSL ─────────────────────────────────────────────────────────
echo ""
echo "==> Setting up SSL with certbot"
ssh "$SSH_USER@$IP" "
    sudo certbot --nginx -d $DOMAIN --non-interactive --agree-tos --register-unsafely-without-email --redirect
"
echo "    Done."

# ── Phase 6: Clean up old droplets ────────────────────────────────────────
echo ""
if [[ -n "$PROJECT_ID" ]]; then
    PROJECT_DROPLET_IDS=$(doctl projects resources list "$PROJECT_ID" --format URN --no-header 2>/dev/null \
        | grep '^do:droplet:' | sed 's/do:droplet://' | grep -v "$DROPLET_ID" || true)
    if [[ -n "$PROJECT_DROPLET_IDS" ]]; then
        echo "==> Old droplets in $DO_PROJECT project:"
        for OLD_ID in $PROJECT_DROPLET_IDS; do
            doctl compute droplet get "$OLD_ID" --format ID,Name,PublicIPv4 --no-header 2>/dev/null
        done
        echo "    Destroy them? [y/N]"
        read -r CONFIRM
        if [[ "$CONFIRM" =~ ^[Yy]$ ]]; then
            for OLD_ID in $PROJECT_DROPLET_IDS; do
                doctl compute droplet delete "$OLD_ID" --force
                echo "    Destroyed $OLD_ID"
            done
        fi
    fi
fi

echo ""
echo "==> Done! App accessible at https://$DOMAIN/"
echo "    Push to main to deploy secrets (LLM access + admin auth)."
