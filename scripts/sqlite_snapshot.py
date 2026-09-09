"""Online SQLite backup without loading application code or changing the source."""
import argparse
from pathlib import Path
import sqlite3

parser = argparse.ArgumentParser()
parser.add_argument("source")
parser.add_argument("destination")
args = parser.parse_args()
source, destination = Path(args.source).resolve(), Path(args.destination).resolve()
if destination.exists() or source == destination:
    raise SystemExit("Destination must be a new file")
with sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True) as src, sqlite3.connect(destination) as dst:
    src.backup(dst)
    assert dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
print("SQLite backup verified")
