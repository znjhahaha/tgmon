"""Create a consistent SQLite backup without stopping the running service."""
import json
import os
import sqlite3
import sys
from pathlib import Path

destination = Path(sys.argv[1])
if destination.exists():
    raise SystemExit("Backup target already exists; refusing to overwrite it.")
destination.parent.mkdir(parents=True, exist_ok=True)
source = sqlite3.connect(f"file:{os.getenv('TGMON_DB', '/app/db/tgmon.db')}?mode=ro", uri=True)
backup = sqlite3.connect(destination)
source.backup(backup)
integrity = backup.execute("PRAGMA integrity_check").fetchone()[0]
backup.close()
source.close()
assert integrity == "ok", integrity
os.chmod(destination, 0o600)
print(json.dumps({"backup": str(destination), "bytes": destination.stat().st_size,
                  "integrity": integrity}))
