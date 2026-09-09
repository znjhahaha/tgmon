"""SQLite-backed queues with atomic leases and explicit ambiguous-send state."""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, update, or_
from sqlalchemy.dialects.sqlite import insert

from .db import session_scope
from .models import ProcessingJob, SourceEvent


# Several worker queues claim jobs from the same SQLite database. Serialize
# the short claim/recovery transaction inside one worker process so competing
# lease updates never hold RESERVED locks against one another.
_claim_lock = threading.Lock()


def enqueue(queue: str, key: str, payload: dict, *, delay: float = 0, session=None,
            scope_key: str | None = None) -> int:
    def write(s):
        now = datetime.utcnow()
        s.execute(insert(ProcessingJob).values(queue=queue, key=key, payload=payload,
            scope_key=scope_key,
            status="pending", attempts=0, available_at=now + timedelta(seconds=delay),
            created_at=now, updated_at=now).on_conflict_do_nothing(index_elements=["key"]))
        return s.execute(select(ProcessingJob.id).where(ProcessingJob.key == key)).scalar_one()
    if session is not None:
        return write(session)
    with session_scope() as s:
        return write(s)


def claim(queue: str, *, lease_seconds: int = 120) -> dict | None:
    with _claim_lock:
        now, owner = datetime.utcnow(), uuid.uuid4().hex
        with session_scope() as s:
            recover(s, now)
            from sqlalchemy import exists
            from sqlalchemy.orm import aliased
            prior = aliased(ProcessingJob)
            candidate = select(ProcessingJob.id).where(
                ProcessingJob.queue == queue, ProcessingJob.status.in_(("pending", "retry")),
                ProcessingJob.available_at <= now,
                or_(ProcessingJob.scope_key.is_(None), ~exists(select(prior.id).where(
                    prior.scope_key == ProcessingJob.scope_key, prior.queue == queue,
                    prior.id < ProcessingJob.id, prior.status.in_(("pending", "retry", "running")))))
                ).order_by(
                ProcessingJob.available_at, ProcessingJob.id).limit(1).scalar_subquery()
            row = s.execute(update(ProcessingJob).where(ProcessingJob.id == candidate).values(
                status="running", owner=owner, lease_until=now + timedelta(seconds=lease_seconds),
                attempts=ProcessingJob.attempts + 1, updated_at=now).returning(
                ProcessingJob.id, ProcessingJob.queue, ProcessingJob.payload,
                ProcessingJob.attempts)).first()
            return {"id": row.id, "queue": row.queue, "payload": row.payload,
                    "attempts": row.attempts, "owner": owner} if row else None


def recover(s, now: datetime) -> int:
    return s.execute(update(ProcessingJob).where(ProcessingJob.status == "running",
        ProcessingJob.lease_until < now).values(status="retry", owner=None,
        error="Worker lease expired", available_at=now, updated_at=now)).rowcount


def renew(job: dict, lease_seconds: int = 120) -> bool:
    with session_scope() as s:
        return s.execute(update(ProcessingJob).where(
            ProcessingJob.id == job["id"], ProcessingJob.owner == job["owner"],
            ProcessingJob.status == "running").values(
            lease_until=datetime.utcnow() + timedelta(seconds=lease_seconds),
            updated_at=datetime.utcnow())).rowcount == 1


def finish(job: dict, result: dict | None = None, *, error: str = "",
           retry_after: float | None = None, review: bool = False) -> bool:
    now = datetime.utcnow()
    status = "review" if review else "retry" if retry_after is not None else "failed" if error else "done"
    with session_scope() as s:
        return s.execute(update(ProcessingJob).where(
            ProcessingJob.id == job["id"], ProcessingJob.owner == job["owner"],
            ProcessingJob.status == "running").values(status=status, result=result,
            error=error[:2000] or None, available_at=now + timedelta(seconds=retry_after or 0),
            lease_until=None, owner=None, updated_at=now)).rowcount == 1


async def heartbeat(job: dict) -> None:
    while True:
        await asyncio.sleep(30)
        if not await asyncio.to_thread(renew, job):
            return


def save_events(channel_id: int, messages: list, *, delay: float = 0) -> list[int]:
    """Persist Telegram's TL payload before acknowledging an incoming batch."""
    ids = []
    with session_scope() as s:
        for message in messages:
            source_id = str(message.id)
            media = getattr(message, "photo", None) or getattr(message, "document", None)
            snapshot = {"id": source_id, "text": getattr(message, "message", "") or "",
                        "grouped_id": str(getattr(message, "grouped_id", "") or ""),
                        "media_id": str(getattr(media, "id", "") or ""),
                        "date": str(getattr(message, "date", "") or ""),
                        "edited": str(getattr(message, "edit_date", "") or ""),
                        "entities": [x.to_dict() for x in getattr(message, "entities", []) or []]}
            revision = hashlib.sha256(json.dumps(snapshot, sort_keys=True,
                ensure_ascii=False, default=str).encode()).hexdigest()
            try:
                raw = bytes(message)
            except TypeError:
                raw = None
            s.execute(insert(SourceEvent).values(channel_id=channel_id, source_id=source_id,
                revision=revision, grouped_id=snapshot["grouped_id"] or None, raw=raw,
                snapshot=snapshot, received_at=datetime.utcnow()).on_conflict_do_nothing())
            eid = s.execute(select(SourceEvent.id).where(SourceEvent.channel_id == channel_id,
                SourceEvent.source_id == source_id, SourceEvent.revision == revision)).scalar_one()
            ids.append(eid)
            enqueue("ingest", f"telegram:{eid}", {"event_id": eid},
                    delay=delay, session=s, scope_key=f"telegram:{channel_id}")
    return ids


def decode_event(event: SourceEvent):
    if event.raw:
        from telethon.extensions import BinaryReader
        with BinaryReader(event.raw) as reader:
            return reader.tgread_object()
    from types import SimpleNamespace
    data = event.snapshot
    return SimpleNamespace(id=int(event.source_id), message=data["text"],
        grouped_id=data["grouped_id"] or None, media=None, entities=[], date=None,
        edit_date=data.get("edited") or None)
