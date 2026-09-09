"""Start an isolated, loopback-only admin preview without external accounts."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    root = Path(args.directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for key, value in {"DB": root / "preview.sqlite3", "SESSIONS": root / "sessions",
                       "MEDIA": root / "media", "LOGS": root / "logs",
                       "SECRET_FILE": root / "secret.key", "EXTENSIONS": root / "extensions"}.items():
        os.environ[f"TGMON_{key}"] = str(value)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tgmon.bootstrap import init_all
    from tgmon import settings
    from tgmon.crypto import hash_password
    from tgmon.db import session_scope
    from tgmon.models import AdminUser, Channel, MonitorMessage
    init_all()
    settings.set_many({"QQ_ENABLED": False, "WEBHOOK_ENABLED": False,
                       "MCP_ENABLED": False, "EMBEDDING_ENABLED": False,
                       "BASE_URL": f"http://127.0.0.1:{args.port}"})
    with session_scope() as s:
        if not s.query(AdminUser).filter_by(username="preview").first():
            s.add(AdminUser(username="preview", password_hash=hash_password("preview-local-2026"), role="admin"))
        if not s.query(Channel).first():
            channel = Channel(tg_id="preview", title="通用资讯示例", theme="generic", enabled=True)
            s.add(channel)
            s.flush()
            s.add(MonitorMessage(channel_id=channel.id, tg_message_id="1", theme="generic",
                text_raw="Sample source 42", text_zh="示例资讯 42", translate_status="ok"))
    import uvicorn
    uvicorn.run("tgmon.admin.app:app", host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
