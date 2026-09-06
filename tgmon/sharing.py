"""Immutable story bundles and cached cards, usable without the admin app."""
from __future__ import annotations

import hashlib
import secrets
import json
from datetime import datetime, timedelta
from pathlib import Path

from . import card, settings
from .crypto import decrypt, encrypt
from .db import session_scope
from .message_query import by_ids
from .models import ShareToken
from .paths import MEDIA_DIR
from .util import to_local


def create_bundle(ids: list[int], *, created_by="qqbot", hours=48, summary="") -> int:
    records = by_ids(ids)
    if not records:
        raise ValueError("没有可分享的消息")
    game_order = list(dict.fromkeys(r["game"] for r in records))
    records.sort(key=lambda r: game_order.index(r["game"]))
    items = []
    for row in records:
        item = dict(row)
        item["published_at"] = row["published_at"].isoformat() if row["published_at"] else ""
        local = to_local(row["published_at"]) if row["published_at"] else None
        item.update(channel=row["title"], date_str=local.strftime("%m-%d %H:%M") if local else "",
                    thumb_paths=row["photos"], raw=row["raw"])
        items.append(item)
    token = secrets.token_urlsafe(12)
    with session_scope() as s:
        row = ShareToken(token_hash=hashlib.sha256(token.encode()).hexdigest(), token_enc=encrypt(token),
                         message_id=records[0]["id"], message_ids=[r["id"] for r in records],
                         snapshot_items=items, created_by=created_by, summary=summary or None,
                         expires_at=datetime.utcnow() + timedelta(hours=hours))
        s.add(row)
        s.flush()
        return row.id


def bundle_url(row: ShareToken) -> str:
    base = str(settings.get("BASE_URL") or "").rstrip("/")
    if not base or not row.token_enc:
        raise ValueError("分享站点地址尚未配置")
    return f"{base}/s/{decrypt(row.token_enc)}"


def bundle_cards(sid: int, *, media_dir: Path | None = None) -> tuple[list[str], str, list[dict]]:
    root = media_dir or MEDIA_DIR
    with session_scope() as s:
        row = s.get(ShareToken, sid)
        if row is None or row.revoked or row.expires_at <= datetime.utcnow():
            raise ValueError("分享链接已失效")
        items = list(row.snapshot_items or [])
        if not items:
            raise ValueError("分享快照不存在")
        url = bundle_url(row)
        summary = row.summary or ""
    fingerprint = hashlib.sha256(json.dumps([items, summary, url], ensure_ascii=False,
                                            sort_keys=True).encode()).hexdigest()[:20]
    directory = root / "qq-cards" / f"{sid}-{fingerprint}"
    directory.mkdir(parents=True, exist_ok=True)
    existing = sorted(directory.glob("page-*.jpg"))
    marker = directory / "complete.json"
    complete = None
    if marker.exists():
        try:
            complete = json.loads(marker.read_text(encoding="ascii"))
        except (OSError, TypeError, ValueError):
            complete = None
    if not complete or len(existing) != complete.get("pages"):
        for old_page in existing:
            old_page.unlink(missing_ok=True)
        data = [{**item, "raw": ""} for item in items]
        pages = card.render_long_cards(data, summary=summary, url=url, media_dir=root)
        for i, page in enumerate(pages, 1):
            path = directory / f"page-{i:03d}.jpg"
            temporary = directory / f"page-{i:03d}.{secrets.token_hex(4)}.tmp"
            temporary.write_bytes(page)
            temporary.replace(path)
        existing = sorted(directory.glob("page-*.jpg"))
        temporary = directory / f"complete-{secrets.token_hex(4)}.tmp"
        temporary.write_text(json.dumps({"pages": len(pages)}), encoding="ascii")
        temporary.replace(marker)
    return [p.relative_to(root).as_posix() for p in existing], url, items


def protected_message_ids(s) -> set[int]:
    rows = s.query(ShareToken).filter(ShareToken.revoked.is_(False),
                                     ShareToken.expires_at > datetime.utcnow()).all()
    ids = {int(mid) for row in rows for item in (row.snapshot_items or [])
           for mid in item.get("source_ids", [item["id"]])}
    for row in rows:
        ids.update(row.message_ids or [row.message_id])
    return ids


def snapshot_views(items: list[dict]) -> list[dict]:
    return [{**item, "text_raw": item.get("raw", ""), "text_zh": item.get("text", ""),
             "game_detected": item.get("game", ""), "version_tag": item.get("version", ""),
             "published_at": datetime.fromisoformat(item["published_at"]) if item.get("published_at") else None,
             "topics": [], "raw_segments": None, "zh_segments": None,
             "spoiler_aligned": False, "entities": [], "media": item.get("media", [])}
            for item in items]
