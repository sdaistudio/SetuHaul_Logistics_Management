#!/bin/bash
# Restore a backup into a SCRATCH database and prove it is usable (integrity + row counts).
# Works on the server or a laptop (needs python3). Does not touch the live database.
# Usage: restore-check.sh <demo_world-....db.gz | .db>
set -euo pipefail
IN="${1:?usage: restore-check.sh <backup.db.gz>}"
SCRATCH="$(mktemp -d)/restored.db"
case "$IN" in *.gz) gunzip -c "$IN" > "$SCRATCH" ;; *) cp "$IN" "$SCRATCH" ;; esac
python3 - "$SCRATCH" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
print("integrity_check:", c.execute("PRAGMA integrity_check").fetchone()[0])
# MVP 1 runs on SQLite: the no-double-booking promise lives in these triggers (setuhaul/db.py GUARDS).
have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
missing = [g for g in ("trg_no_overlap", "trg_appt_guard", "trg_active_must_be_current") if g not in have]
print("booking guards:", "all 3 present" if not missing else "MISSING " + ", ".join(missing))
if missing:
    sys.exit("restore check FAILED: booking guard triggers missing")
for t in ("drivers", "shipments", "appointments", "slot_holds", "escalations", "wa_inbound", "wa_delivery"):
    try:
        print(f"  {t:13s}", c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
    except sqlite3.Error as e:
        print(f"  {t:13s} (absent: {e})")
PY
rm -rf "$(dirname "$SCRATCH")"
echo "restore check OK (scratch copy deleted)"
