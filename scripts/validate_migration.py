"""Validate upgrades against an SQLite backup, never the source database."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys


def counts(path):
    with sqlite3.connect(path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {name: db.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                for name in ("monitor_message", "message_media", "share_token", "qq_group", "qq_bot") if name in tables}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("destination")
    args = parser.parse_args()
    source, destination = Path(args.source).resolve(), Path(args.destination).resolve()
    if source == destination or destination.exists():
        raise SystemExit("Destination must be a new database copy")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)
    before = counts(destination)
    os.environ["TGMON_DB"] = str(destination)
    os.environ["TGMON_SECRET_FILE"] = str(destination.parent / "validation.key")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tgmon.bootstrap import init_all
    init_all()
    after = counts(destination)
    assert before == {k: after[k] for k in before}, (before, after)
    init_all()
    assert counts(destination) == after
    with sqlite3.connect(destination) as db:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        assert integrity == "ok"
        versions = [row[0] for row in db.execute("SELECT version FROM schema_migration ORDER BY version")]
        pending = db.execute("SELECT count(*) FROM processing_job WHERE status='pending'").fetchone()[0]
    print(json.dumps({"database": str(destination), "counts": after, "versions": versions,
                      "integrity": integrity, "idempotent": True, "pending_jobs": pending}, ensure_ascii=False))


if __name__ == "__main__":
    main()
