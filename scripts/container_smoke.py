"""Offline container smoke check of the migrated application and media tools."""
import json
import os
from pathlib import Path
import subprocess
import sys

root = Path("/validation")
os.environ["TGMON_DB"] = str(root / "migrated.sqlite3")
os.environ["TGMON_MEDIA"] = str(root / "media")
os.environ["TGMON_SESSIONS"] = str(root / "sessions")
os.environ["TGMON_LOGS"] = str(root / "logs")
os.environ["TGMON_SECRET_FILE"] = str(root / "validation.key")
sys.path.insert(0, "/app")

from tgmon.admin.app import app
from tgmon import settings
from tgmon.admin.deps import require_admin, require_login
from tgmon.db import session_scope
from tgmon.models import Base
from fastapi.testclient import TestClient

settings.set_many({"QQ_ENABLED": False, "MCP_ENABLED": False, "PLUGIN_ENABLED": False,
                   "WEBHOOK_ENABLED": False, "EMBEDDING_ENABLED": False})
app.dependency_overrides[require_admin] = lambda: "validation"
app.dependency_overrides[require_login] = lambda: "validation"
checked = {}
with TestClient(app) as client:
    for path in ("/login", "/messages", "/plugins", "/qqbot", "/providers", "/kb?tab=memory"):
        response = client.get(path)
        assert response.status_code == 200, (path, response.status_code, response.text[:300])
        checked[path] = response.status_code
subprocess.run([sys.executable, "-m", "pip", "check"], check=True)
subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=128x128:d=0.1",
                "-c:v", "libx264", "-y", str(root / "smoke.mp4")], check=True)
from tgmon.media import _playable
assert _playable(root / "smoke.mp4")
print(json.dumps({"routes": checked, "ffmpeg": "ok", "dependencies": "ok"}))
