#!/usr/bin/env bash
#
# pull_prod.sh — decompress the newest Pi backup of the production database
# into the working tree, so `uv run app.py` and ad-hoc queries see real data.
#
#     bash scripts/pull_prod.sh
#
# Snapshots come from ~/src/rpi/backup/pull-backups.sh, which runs daily.
# Logs are in the Pi's journal, not files:
#     ssh mypi-remote 'journalctl --user-unit "karb-*" --since "2 days ago"'
set -euo pipefail

SNAPSHOT="${KARB_SNAPSHOT:-${HOME}/src/rpi/backups/latest/karb/slonk_arb.db.gz}"
LOCAL_DB="${SLONK_DB:-slonk_arb.db}"
STALE_HOURS="${KARB_STALE_HOURS:-36}"

if [ ! -f "$SNAPSHOT" ]; then
    echo "No snapshot at $SNAPSHOT" >&2
    echo "Refresh with: ~/src/rpi/backup/pull-backups.sh" >&2
    exit 1
fi

age_h=$(( ( $(date +%s) - $(stat -f %m "$SNAPSHOT" 2>/dev/null || stat -c %Y "$SNAPSHOT") ) / 3600 ))
echo "==> Snapshot: $SNAPSHOT (${age_h}h old)"
if [ "$age_h" -gt "$STALE_HOURS" ]; then
    echo "    STALE (>${STALE_HOURS}h). Refresh with: ~/src/rpi/backup/pull-backups.sh" >&2
fi

if [ -f "$LOCAL_DB" ]; then
    BACKUP_DIR="db_backups/$(date +%Y%m%d_%H%M%S)"
    mkdir -p "$BACKUP_DIR"
    mv "$LOCAL_DB" "$BACKUP_DIR/"
    echo "==> Moved existing $LOCAL_DB to $BACKUP_DIR/"
fi

echo "==> Decompressing..."
# Temp file so an interrupted run leaves no truncated database at $LOCAL_DB.
gunzip -c "$SNAPSHOT" > "${LOCAL_DB}.partial"
mv "${LOCAL_DB}.partial" "$LOCAL_DB"

echo "==> Done."
ls -lh "$LOCAL_DB"
