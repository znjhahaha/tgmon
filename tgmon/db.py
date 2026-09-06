"""SQLite 引擎。admin 与 worker 两个进程同时读写，所以 WAL + busy_timeout 是必须的。"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .paths import DB_PATH, ensure_dirs

logger = logging.getLogger(__name__)

ensure_dirs()

def _json_dumps(obj) -> str:
    """JSON 列不转义中文。

    默认的 json.dumps 会把「卡芙卡」写成 \\u5361\\u8299\\u5361，于是
    `entities LIKE '%卡芙卡%'` 这种按实体过滤永远查不到东西。读取侧无所谓
    （json.loads 两种都认），所以只改写入。附带好处是 sqlite3 命令行里
    直接能看懂 JSON 列的内容。
    """
    import json
    return json.dumps(obj, ensure_ascii=False)


engine = create_engine(
    f"sqlite:///{DB_PATH}",
    future=True,
    json_serializer=_json_dumps,
    # FastAPI 的同步端点跑在线程池里，连接会跨线程
    connect_args={"check_same_thread": False, "timeout": 15},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    # WAL: 读不阻塞写，两个进程并发的前提
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    # 抢锁时最多等 15s 再报 database is locked
    cur.execute("PRAGMA busy_timeout=15000")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务边界。注意：别在里面做 AI 调用或网络 IO，会长时间占着写锁。"""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
