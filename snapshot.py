#!/usr/bin/env python3
"""Write a consistent snapshot of the live database to /data/.backup.db.

Run inside the container by the off-box backup (rpi/backup/pull-backups.sh),
so it sees the same file the app has open.

VACUUM INTO, never a file copy. The database runs in WAL mode, so recent
writes live in slonk_arb.db-wal rather than in slonk_arb.db — copying the main
file alone yields a valid database that is silently missing everything since
the last checkpoint. VACUUM INTO takes a transactionally consistent snapshot of
a live database and folds the WAL in, with no downtime and no possibility of a
torn read.

The connection is read-write on purpose. VACUUM INTO cannot modify the source,
and a read-only connection to a WAL database needs the -shm segment to already
exist — which makes the backup depend on whether some other process happens to
have the database open at the time.
"""

import os
import sqlite3

SRC = os.environ.get("SLONK_DB", "/data/slonk_arb.db")
DEST = "/data/.backup.db"

if os.path.exists(DEST):
    os.unlink(DEST)

con = sqlite3.connect(SRC)
try:
    con.execute("VACUUM INTO ?", (DEST,))
finally:
    con.close()

print(f"{DEST}: {os.path.getsize(DEST)} bytes")
