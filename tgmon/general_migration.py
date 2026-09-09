"""Incremental general-platform migration. Source history is preserved."""
from __future__ import annotations

from .db import session_scope
from .models import AppSetting, ContentRelation, MonitorMessage, SchemaMigration


def migrate():
    with session_scope() as s:
        if s.get(SchemaMigration, 2):
            return
        from sqlalchemy.dialects.sqlite import insert
        for row in s.query(MonitorMessage).filter(
                MonitorMessage.dup_reason.in_(("phash", "simhash")),
                MonitorMessage.duplicate_of.isnot(None)):
            s.execute(insert(ContentRelation).values(message_id=row.id,
                related_id=row.duplicate_of, reason=row.dup_reason).on_conflict_do_nothing())
            row.duplicate_of, row.dup_reason = None, None
        from . import settings
        for key, value in {"MESSAGE_TTL_DAYS": "0", "MEDIA_TTL_DAYS": "7",
                           "VIDEO_TTL_DAYS": "7", "VIDEO_ARCHIVE_ENABLED": "true"}.items():
            row = s.get(AppSetting, key)
            if row is None:
                meta = settings.DEFAULTS[key]
                row = AppSetting(key=key, type=meta[1], group=meta[2], is_secret=False)
                s.add(row)
            row.value = value
        from .models import Channel, MessageMedia
        from .jobs import enqueue
        import re
        for asset, message in s.query(MessageMedia, MonitorMessage).join(
                MonitorMessage, MonitorMessage.id == MessageMedia.message_id).join(
                Channel, Channel.id == MonitorMessage.channel_id).filter(Channel.source_type == "telegram"):
            name = (asset.thumb_path or asset.video_path or "").rsplit("/", 1)[-1]
            match = re.match(r"^(\d+)(?:[_\-.]|$)", name)
            asset.source_tg_id = asset.source_tg_id or (match.group(1) if match else message.tg_message_id)
            if asset.kind == "video" and not asset.video_path:
                asset.video_status = "queued"
                enqueue("archive", f"archive:{asset.id}", {"channel_id": message.channel_id,
                    "media_id": asset.id, "tg_message_id": asset.source_tg_id}, session=s)
        s.add(SchemaMigration(version=2))
    settings.invalidate()
