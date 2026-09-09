"""Versioned source content and one stable content fingerprint."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy.dialects.sqlite import insert

from .db import session_scope
from .models import ContentRevision, MessageMedia, MonitorMessage

RENDER_VERSION = "3"


def snapshot(s, mid: int) -> dict:
    row = s.get(MonitorMessage, mid)
    if row is None:
        return {}
    return {"id": mid, "raw": row.text_raw, "text": row.text_zh,
        "theme": getattr(row, "theme", None), "game": row.game_detected,
        "translation_status": row.translate_status, "render_version": RENDER_VERSION,
        "media": [{"id": m.id, "source": m.source_tg_id, "identity": m.source_identity,
                   "kind": m.kind, "sha256": m.sha256, "thumb": m.thumb_path,
                   "video": m.video_path, "status": m.status, "video_status": m.video_status}
                  for m in s.query(MessageMedia).filter_by(message_id=mid).filter(
                      MessageMedia.status != "superseded").order_by(MessageMedia.id)]}


def fingerprint(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), default=str).encode()).hexdigest()


def record(mid: int, *, session=None) -> str:
    def write(s):
        value = snapshot(s, mid)
        if not value:
            return ""
        digest = fingerprint(value)
        s.execute(insert(ContentRevision).values(message_id=mid, digest=digest, snapshot=value)
                  .on_conflict_do_nothing())
        return digest
    if session is not None:
        return write(session)
    with session_scope() as s:
        return write(s)


def merge_exact(s, mid: int) -> int | None:
    """Merge complete, byte-identical attachment sets and identical source text."""
    row = s.get(MonitorMessage, mid)
    assets = s.query(MessageMedia).filter_by(message_id=mid).filter(
        MessageMedia.status != "superseded").order_by(MessageMedia.id).all()
    if row is None or not assets or any(not asset.sha256 for asset in assets):
        return None
    signature = sorted((asset.kind, asset.sha256) for asset in assets)
    candidates = s.query(MonitorMessage).filter(MonitorMessage.id < mid,
        MonitorMessage.duplicate_of.is_(None), MonitorMessage.theme == row.theme,
        MonitorMessage.text_raw == row.text_raw).order_by(MonitorMessage.id).all()
    candidate_ids = [candidate.id for candidate in candidates]
    by_message = {}
    for asset in s.query(MessageMedia).filter(MessageMedia.message_id.in_(candidate_ids),
                                             MessageMedia.status != "superseded"):
        by_message.setdefault(asset.message_id, []).append((asset.kind, asset.sha256))
    for candidate in candidates:
        existing = by_message.get(candidate.id, [])
        if existing and all(sha for _, sha in existing) and sorted(existing) == signature:
            row.duplicate_of, row.dup_reason = candidate.id, "file_exact"
            return candidate.id
    return None
