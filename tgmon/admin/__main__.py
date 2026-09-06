"""admin 进程入口：python -m tgmon.admin"""
from __future__ import annotations

import os

import uvicorn

from ..util import setup_logging


def main() -> None:
    setup_logging("admin")
    port = int(os.getenv("TGMON_ADMIN_PORT") or 8000)
    # 只在容器网络内监听，对外由 Caddy 反代（带 TLS）
    uvicorn.run("tgmon.admin.app:app", host="0.0.0.0", port=port,
                log_config=None, access_log=False, proxy_headers=True,
                forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
