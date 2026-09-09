"""运行时路径。容器内由 compose 注入环境变量，本地跑用相对目录兜底。"""
from __future__ import annotations

import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

DB_PATH = Path(os.getenv("TGMON_DB") or _ROOT / "db" / "tgmon.db")
SESSIONS_DIR = Path(os.getenv("TGMON_SESSIONS") or _ROOT / "sessions")
MEDIA_DIR = Path(os.getenv("TGMON_MEDIA") or _ROOT / "media")
LOGS_DIR = Path(os.getenv("TGMON_LOGS") or _ROOT / "logs")
SECRET_KEY_FILE = Path(os.getenv("TGMON_SECRET_FILE") or _ROOT / "secret.key")

# 归档视频的存放地。默认在 MEDIA_DIR 下的 private/ —— Caddy 对这个前缀直接
# 404（见 Caddyfile），admin 走鉴权路由发文件。video_path 一律相对 VIDEO_DIR
VIDEO_DIR = Path(os.getenv("TGMON_VIDEO") or MEDIA_DIR / "private")

# user session 的文件名。worker 独占，admin 只检查存在性与删除
USER_SESSION = SESSIONS_DIR / "user.session"
EXTENSIONS_DIR = Path(os.getenv("TGMON_EXTENSIONS") or DB_PATH.parent / "extensions")


def ensure_dirs() -> None:
    for d in (DB_PATH.parent, SESSIONS_DIR, MEDIA_DIR, VIDEO_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)
