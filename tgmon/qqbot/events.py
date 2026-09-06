"""Durable inbound claims and per-part delivery state."""
from __future__ import annotations

import hashlib
from dataclasses import asdict
from datetime import datetime, timedelta

from sqlalchemy.dialects.sqlite import insert

from ..db import session_scope
from ..models import QqEvent


def event_key(kind: str, target: str, msg_id: str) -> str:
    return hashlib.sha256(f"{kind}\0{target}\0{msg_id}".encode()).hexdigest()


def claim(kind: str, target: str, msg_id: str) -> tuple[str, bool, list[dict]]:
    if not msg_id:
        return "", True, []
    key = event_key(kind, target, msg_id)
    with session_scope() as s:
        inserted = s.execute(insert(QqEvent).values(event_key=key, status="processing",
            updated_at=datetime.utcnow()).on_conflict_do_nothing()).rowcount == 1
        row = s.get(QqEvent, key)
        if not inserted and row.status == "processing" and row.updated_at < datetime.utcnow() - timedelta(minutes=5):
            inserted = s.query(QqEvent).filter_by(event_key=key, status="processing").filter(
                QqEvent.updated_at < datetime.utcnow() - timedelta(minutes=5)).update(
                {"updated_at": datetime.utcnow()}, synchronize_session=False) == 1
        return key, inserted, list((row.result or {}).get("replies", [])) if row.status == "ready" else []


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


def begin_delivery(key: str, count: int = 0) -> bool:
    with session_scope() as s:
        s.execute(insert(QqEvent).values(event_key=key, status="ready",
            updated_at=datetime.utcnow()).on_conflict_do_nothing())
        claimed = s.query(QqEvent).filter_by(event_key=key, status="ready").update(
            {"status": "delivering", "updated_at": datetime.utcnow()}) == 1
        if claimed:
            row = s.get(QqEvent, key)
            parts = dict(row.parts or {})
            for index in range(1, count + 1):
                parts.setdefault(str(index), {"status": "pending"})
            row.parts = parts
        return claimed


def delivery_parts(key: str) -> dict:
    with session_scope() as s:
        row = s.get(QqEvent, key)
        return dict(row.parts or {}) if row else {}


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


def finish_part(key: str, index: int, status: str, error="") -> None:
    with session_scope() as s:
        row = s.get(QqEvent, key)
        parts = dict(row.parts or {})
        parts[str(index)] = {"status": status, "error": error[:1000],
                             "at": datetime.utcnow().isoformat()}
        row.parts = parts
        row.updated_at = datetime.utcnow()
