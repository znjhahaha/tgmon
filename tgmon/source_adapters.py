"""Persist plugin source pages through the existing content and push pipeline."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy.dialects.sqlite import insert

from . import jobs, pipeline
from .contracts import Event
from .db import session_scope
from .models import Channel, SourceCursor, SourceEvent


def channel_for(plugin: str, config: dict) -> int:
    from .themes import get_theme
    source_id = str(config["id"])
    with session_scope() as s:
        row = s.query(Channel).filter_by(source_type="plugin", tg_id=f"{plugin}:{source_id}").first()
        if row is None:
            row = Channel(source_type="plugin", tg_id=f"{plugin}:{source_id}",
                          title=str(config.get("title") or source_id)[:300],
                          theme=get_theme(config.get("theme") or "generic").key,
                          enabled=True, translate=bool(config.get("translate", True)))
            s.add(row)
            s.flush()
        return row.id


def checkpoint(channel_id: int):
    with session_scope() as s:
        row = s.get(SourceCursor, channel_id)
        return row.state if row else None


def save_page(channel_id: int, page: dict) -> int:
    if not isinstance(page, dict) or not isinstance(page.get("events"), list):
        raise ValueError("source page must contain an events list")
    if len(page["events"]) > 100:
        raise ValueError("source page exceeds 100 events")
    events = [Event.model_validate(value) for value in page["events"]]
    with session_scope() as s:
        for event in events:
            snapshot = event.model_dump(mode="json")
            revision = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
            s.execute(insert(SourceEvent).values(channel_id=channel_id, source_id=event.source_id,
                revision=revision, snapshot=snapshot).on_conflict_do_nothing())
            row = s.query(SourceEvent).filter_by(channel_id=channel_id,
                source_id=event.source_id, revision=revision).one()
            jobs.enqueue("source", f"source:{row.id}", {"event_id": row.id},
                         scope_key=f"source:{channel_id}", session=s)
        cursor = s.get(SourceCursor, channel_id)
        if cursor is None:
            cursor = SourceCursor(channel_id=channel_id)
            s.add(cursor)
        cursor.state, cursor.complete, cursor.updated_at = page.get("cursor"), True, datetime.utcnow()
    return len(events)


async def ingest(runner, payload):
    with session_scope() as s:
        event = s.get(SourceEvent, payload["event_id"])
        if event is None:
            return {}
        event = s.query(SourceEvent).filter_by(channel_id=event.channel_id,
            source_id=event.source_id).order_by(SourceEvent.id.desc()).first()
        value, channel_id, eid = Event.model_validate(event.snapshot), event.channel_id, event.id
    message = SimpleNamespace(id=value.source_id, message=value.text,
        source_url=value.deeplink, date=value.published_at, grouped_id=None, media=None,
        entities=[], edit_date=None)
    mid = await pipeline.ingest(None, channel_id, [message], deferred=True, complete_snapshot=True)
    with session_scope() as s:
        if mid is None:
            from .models import MonitorMessage
            row = s.query(MonitorMessage).filter_by(channel_id=channel_id, tg_message_id=value.source_id).first()
            mid = row.id if row else None
        if mid:
            s.get(SourceEvent, eid).message_id = mid
            jobs.enqueue("publish", f"publish:{eid}", {"message_id": mid}, session=s)
    return {"message_id": mid}
