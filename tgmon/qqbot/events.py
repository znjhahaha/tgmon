"""Durable inbound claims and per-part delivery state."""
from __future__ import annotations

import hashlib
import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta

from sqlalchemy.dialects.sqlite import insert

from ..db import session_scope
from ..models import QqEvent


_conversation_locks: dict[str, asyncio.Lock] = {}


def conversation_lock(kind: str, target: str, bot_id: str = "") -> asyncio.Lock:
    key = f"{bot_id}\0{kind}\0{target}"
    lock = _conversation_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _conversation_locks[key] = lock
    return lock


def event_key(kind: str, target: str, msg_id: str, bot_id: str = "") -> str:
    prefix = f"{bot_id}\0" if bot_id else ""
    return hashlib.sha256(f"{prefix}{kind}\0{target}\0{msg_id}".encode()).hexdigest()


def claim(kind: str, target: str, msg_id: str, bot_id: str = "") -> tuple[str, bool, list[dict]]:
    if not msg_id:
        return "", True, []
    key = event_key(kind, target, msg_id, bot_id)
    with session_scope() as s:
        inserted = s.execute(insert(QqEvent).values(event_key=key, status="processing",
            updated_at=datetime.utcnow()).on_conflict_do_nothing()).rowcount == 1
        row = s.get(QqEvent, key)
        if not inserted and row.status == "processing" and row.updated_at < datetime.utcnow() - timedelta(minutes=5):
            inserted = s.query(QqEvent).filter_by(event_key=key, status="processing").filter(
                QqEvent.updated_at < datetime.utcnow() - timedelta(minutes=5)).update(
                {"updated_at": datetime.utcnow()}, synchronize_session=False) == 1
        return key, inserted, list((row.result or {}).get("replies", [])) if row.status in ("ready", "delivered") else []


def finish(key: str, replies) -> None:
    if not key:
        return
    with session_scope() as s:
        row = s.get(QqEvent, key)
        if row:
            row.result = {"replies": [asdict(r) for r in replies]}
            row.status = "ready"
            row.updated_at = datetime.utcnow()


def begin_part(key: str, index: int) -> bool:
    with session_scope() as s:
        s.execute(insert(QqEvent).values(event_key=key, status="ready",
            updated_at=datetime.utcnow()).on_conflict_do_nothing())
        row = s.get(QqEvent, key)
        parts = dict(row.parts or {})
        if parts.get(str(index), {}).get("status") in ("done", "sending", "unknown"):
            return False
        parts[str(index)] = {"status": "sending", "at": datetime.utcnow().isoformat()}
        row.parts = parts
        return True


def begin_delivery(key: str, count: int = 0, seq_start: int = 1) -> bool:
    with session_scope() as s:
        s.execute(insert(QqEvent).values(event_key=key, status="ready",
            updated_at=datetime.utcnow()).on_conflict_do_nothing())
        claimed = s.query(QqEvent).filter_by(event_key=key, status="ready").update(
            {"status": "delivering", "updated_at": datetime.utcnow()}) == 1
        if claimed:
            row = s.get(QqEvent, key)
            parts = dict(row.parts or {})
            # seq_start>1 = 追加投递（如长图补发接着首条链接的 seq），
            # parts 下标与实际 msg_seq 对齐，pending 标记才准
            for index in range(seq_start, seq_start + count):
                parts.setdefault(str(index), {"status": "pending"})
            row.parts = parts
        return claimed


def delivery_parts(key: str) -> dict:
    with session_scope() as s:
        row = s.get(QqEvent, key)
        return dict(row.parts or {}) if row else {}


def set_reply_deadline(key: str, deadline: datetime) -> None:
    with session_scope() as s:
        row = s.get(QqEvent, key)
        if row is None:
            row = QqEvent(event_key=key, status="ready")
            s.add(row)
        if row.reply_deadline is None or row.reply_deadline > deadline:
            row.reply_deadline = deadline


def reply_expired(key: str) -> bool:
    with session_scope() as s:
        row = s.get(QqEvent, key)
        return bool(row and row.reply_deadline and datetime.utcnow() >= row.reply_deadline)


def recover_stale() -> int:
    with session_scope() as s:
        rows = s.query(QqEvent).filter(QqEvent.status == "delivering",
            QqEvent.updated_at < datetime.utcnow() - timedelta(minutes=5)).all()
        for row in rows:
            parts = {k: dict(v) for k, v in (row.parts or {}).items()}
            for part in parts.values():
                if part.get("status") == "sending":
                    part.update(status="unknown", error="发送进程中断，结果待核实")
            row.parts = parts
            row.status = "review" if any(p.get("status") == "unknown" for p in parts.values()) else "ready"
        return len(rows)


def end_delivery(key: str) -> dict:
    with session_scope() as s:
        row = s.get(QqEvent, key)
        parts = dict(row.parts or {})
        states = [p.get("status") for p in parts.values()]
        row.status = ("review" if any(x in ("sending", "unknown") for x in states)
                      else "delivered" if states and all(x == "done" for x in states)
                      else "ready")
        row.updated_at = datetime.utcnow()
        return parts


def finish_part(key: str, index: int, status: str, error="", remote_id="") -> None:
    with session_scope() as s:
        row = s.get(QqEvent, key)
        parts = dict(row.parts or {})
        parts[str(index)] = {"status": status, "error": error[:1000],
                             "remote_id": remote_id,
                             "at": datetime.utcnow().isoformat()}
        row.parts = parts
        row.updated_at = datetime.utcnow()
