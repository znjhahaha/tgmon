"""Handlers for the independent persistent processing queues."""
from __future__ import annotations

import asyncio

from .. import jobs, media, pipeline, settings
from ..db import session_scope
from ..models import Channel, MessageMedia, MonitorMessage, SourceEvent


class RetryLater(RuntimeError):
    def __init__(self, message: str, seconds: float = 30):
        super().__init__(message)
        self.seconds = seconds


async def ingest(runner, payload):
    if runner.client is None or runner.status != "online":
        raise RetryLater("Telegram is offline")
    with session_scope() as s:
        event = s.get(SourceEvent, payload["event_id"])
        if event is None:
            return {}
        if event.grouped_id:
            candidates = s.query(SourceEvent).filter_by(channel_id=event.channel_id,
                grouped_id=event.grouped_id).order_by(SourceEvent.id).all()
            by_source = {row.source_id: row for row in candidates}
            events = list(by_source.values())
        else:
            events = [s.query(SourceEvent).filter_by(channel_id=event.channel_id,
                source_id=event.source_id).order_by(SourceEvent.id.desc()).first()]
        channel_id = event.channel_id
        event_ids = [row.id for row in events]
        messages = [jobs.decode_event(row) for row in events]
    mid = await pipeline.ingest(runner.client, channel_id, messages, deferred=True,
                               complete_snapshot=True)
    with session_scope() as s:
        if mid is None:
            anchor = s.query(MonitorMessage).filter_by(channel_id=channel_id)
            anchor = (anchor.filter_by(grouped_id=event.grouped_id) if event.grouped_id
                      else anchor.filter_by(tg_message_id=event.source_id)).first()
            mid = anchor.id if anchor else None
    if mid:
            s.query(SourceEvent).filter(SourceEvent.id.in_(event_ids)).update(
                {"message_id": mid}, synchronize_session=False)
            # One publication job per content event. The output queues have
            # stable message keys, so adding an attachment cannot resend a group.
            jobs.enqueue("publish", f"publish:{payload['event_id']}", {"message_id": mid}, session=s)
    from ..extension_runtime import emit
    await emit({"method": "event", "params": {"type": "content.received", "message_id": mid,
                                               "channel_id": channel_id, "event_ids": event_ids}})
    return {"message_id": mid}


async def process_media(runner, payload, *, archive_only=False):
    if runner.client is None or runner.status != "online":
        raise RetryLater("Telegram is offline")
    with session_scope() as s:
        row = s.get(MessageMedia, payload["media_id"])
        channel = s.get(Channel, payload["channel_id"])
        if row is None or channel is None or row.status == "superseded":
            return {}
        tg_id = int(channel.tg_id)
        source_id = int(payload["tg_message_id"])
        cap = channel.video_max_mb or 0
        row.status = "archiving" if archive_only else "downloading"
    entity = await runner.client.get_entity(tg_id)
    message = await runner.client.get_messages(entity, ids=source_id)
    if message is None:
        with session_scope() as s:
            row = s.get(MessageMedia, payload["media_id"])
            row.status, row.error = "unavailable", "Source message is unavailable"
        return {"status": "unavailable"}
    identity = media.describe(message)
    if identity is None or (row.source_identity and identity.source_identity != row.source_identity):
        await asyncio.to_thread(jobs.save_events, payload["channel_id"], [message])
        return {"status": "source_changed"}
    if archive_only:
        out = media.describe(message)
        out.thumb_path, out.thumb_bytes = row.thumb_path, row.thumb_bytes
        out.phash, out.dhash = row.phash, row.dhash
        await media._archive_video(runner.client, message, payload["channel_id"], out, cap)
    else:
        out = await media.process(runner.client, message, payload["channel_id"],
                                  keep_video=False, video_max_mb=cap)
    if out is None:
        raise RetryLater("Source attachment is unavailable")
    if out.video_status in ("failed", "waiting_space") or out.status in ("failed", "waiting_space"):
        with session_scope() as s:
            row = s.get(MessageMedia, payload["media_id"])
            row.status, row.error = out.video_status or out.status, out.error
            row.original_path, row.original_bytes, row.sha256 = out.original_path, out.original_bytes, out.sha256
            row.thumb_path, row.thumb_bytes = out.thumb_path, out.thumb_bytes
        raise RetryLater(out.error or "Media processing failed",
                         300 if "waiting_space" in (out.video_status, out.status) else 60)
    with session_scope() as s:
        row = s.get(MessageMedia, payload["media_id"])
        if row is None or row.status == "superseded":
            return {}
        for field in ("thumb_path", "thumb_bytes", "width", "height", "duration",
                      "orig_bytes", "mime", "phash", "dhash", "video_path", "video_bytes",
                      "video_status", "has_spoiler", "original_path", "original_bytes", "sha256"):
            setattr(row, field, getattr(out, field))
        row.status, row.error = out.status, out.error
        if out.kind == "video" and not archive_only:
            row.video_status = "queued"
            jobs.enqueue("archive", f"archive:{row.id}", payload, session=s)
        s.flush()
        from ..content import record, merge_exact
        merge_exact(s, row.message_id)
        record(row.message_id, session=s)
    return {"status": out.status}


async def archive(runner, payload):
    return await process_media(runner, payload, archive_only=True)


async def translate(runner, payload):
    from .tasks import _retranslate
    result = await _retranslate(runner, {**payload, "use_cache": True})
    if result.get("status") in ("failed", "budget"):
        raise RetryLater(result.get("error") or "Translation failed", 60)
    return result


async def publish(runner, payload):
    mid = payload["message_id"]
    with session_scope() as s:
        message = s.get(MonitorMessage, mid)
        if message is None:
            return {}
        if message.translate_status == "pending":
            raise RetryLater("Translation is pending", 2)
        media_rows = s.query(MessageMedia).filter_by(message_id=mid).filter(
            MessageMedia.status != "superseded").all()
        if any(row.status in ("queued", "downloading") for row in media_rows):
            # Wait for images to be complete. Video source archives do not
            # delay a usable preview indefinitely.
            from datetime import datetime
            if (datetime.utcnow() - message.created_at).total_seconds() < 120:
                raise RetryLater("Media previews are pending", 2)
    from .. import outputs, qqbot
    await outputs.push_message(mid)
    await qqbot.push_message(mid)
    return {"message_id": mid}


HANDLERS = {"ingest": ingest, "media": process_media, "archive": archive,
            "translate": translate, "publish": publish}
from ..source_adapters import ingest as ingest_source
HANDLERS["source"] = ingest_source
