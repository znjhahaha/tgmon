"""Read-only deployment inventory. Run inside the existing admin container.

Only whitelisted non-secret settings and source hashes are printed.
"""
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

from tgmon import settings

root = Path("/app")
db = sqlite3.connect(f"file:{os.getenv('TGMON_DB', '/app/db/tgmon.db')}?mode=ro", uri=True)
tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
counts = {}
for table in ("monitor_message", "message_media", "glossary_entry", "knowledge_page",
              "retrieval_index", "qq_group", "ai_provider"):
    counts[table] = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] if table in tables else None
report = {
    "python": sys.version.split()[0],
    "encryption_key_in_env": bool(os.getenv("TGMON_SECRET_KEY")),
    "settings": {key: settings.get(key) for key in (
        "BASE_URL", "QQ_ENABLED", "QQ_AI_ENABLED", "TRANSLATE_ENABLED",
        "RETRIEVAL_ENABLED", "EMBEDDING_ENABLED", "QQ_BRIDGE_LAST_SEEN")},
    "counts": counts,
    "files": {},
}
for relative in ("tgmon/retrieval.py", "tgmon/models.py", "tgmon/settings.py", "tgmon/card.py",
                 "tgmon/kb/wiki.py", "tgmon/qqbot/commands.py", "tgmon/qqbot/media.py"):
    path = root / relative
    report["files"][relative] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
db.close()
print(json.dumps(report, ensure_ascii=True))
