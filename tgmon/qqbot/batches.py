"""Persistent, per-game short-window story batching."""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta

from sqlalchemy.dialects.sqlite import insert

from .. import settings
from ..db import session_scope
from ..glossary import normalize_game
from ..message_query import by_ids
from ..models import (MonitorMessage, QqBot, QqBotCapability, QqDelivery,
                      QqGroup, QqPending)
from ..sharing import bundle_cards, create_bundle


def enqueue(message_id: int, force=False) -> int:
    if not settings.get("QQ_ENABLED") and not force:
        return 0
    records = by_ids([message_id])
    if not records:
        return 0
    record = records[0]
    with session_scope() as s:
        message = s.get(MonitorMessage, message_id)
        if not (record.get("text") or record.get("photos") or record.get("has_media")):
            return 0
        if message.duplicate_of and not force:
            return 0
        channels = settings.get("QQ_CHANNEL_IDS") or []
        if channels and message.channel_id not in channels and not force:
            return 0
        # 多机器人扇出：只给「归属机器人可用」的群排队。明确归属且机器人
        # 启用 → 推；无归属（迁移前存量）→ 仅在恰好 ≤1 个启用机器人时推
        #（单机器人旧部署零变化；多机器人时无从判断归属，跳过）
        enabled_rows = s.query(QqBot).filter(QqBot.enabled.is_(True)).all()
        caps = {c.bot_id: c for c in s.query(QqBotCapability).all()}
        bots_enabled = {b.id for b in enabled_rows
                        if caps.get(b.id) is None or caps[b.id].proactive_enabled}
        groups = [g for g in s.query(QqGroup).filter_by(enabled=True).all()
                  if g.bot_id in bots_enabled
                  or (g.bot_id is None and len(enabled_rows) <= 1
                      and (not enabled_rows or bool(bots_enabled)))]
        count = 0
        for group in groups:
            if group.themes and record.get("theme", "gaming") not in group.themes:
                continue
            if group.games and record["game"] not in [normalize_game(g) for g in group.games]:
                continue
            if force:
                old = s.query(QqPending).filter_by(group_openid=group.group_openid, message_id=record["id"]).first()
                if old:
                    s.delete(old)
                    s.flush()
            count += s.execute(insert(QqPending).values(group_openid=group.group_openid,
                message_id=record["id"], game=record["game"], status="waiting",
                created_at=datetime.utcnow(), ready_at=datetime.utcnow() + timedelta(
                    seconds=0 if force else int(settings.get("QQ_BATCH_WINDOW_SECONDS") or 60)))
                .on_conflict_do_nothing()).rowcount
        return count


async def flush_pending(now: datetime | None = None) -> int:
    if not settings.get("QQ_ENABLED"):
        return 0
    now = now or datetime.utcnow()
    done = 0
    for _ in range(10):
        with session_scope() as s:
            first = s.query(QqPending).filter(QqPending.status == "waiting",
                QqPending.ready_at <= now).order_by(QqPending.ready_at, QqPending.id).first()
            if not first:
                break
            rows = s.query(QqPending).filter_by(group_openid=first.group_openid,
                game=first.game, status="waiting").filter(QqPending.created_at <= first.ready_at).order_by(
                QqPending.id).limit(5).all()
            ids, mids, target = [r.id for r in rows], [r.message_id for r in rows], first.group_openid
            count = s.query(QqPending).filter(QqPending.id.in_(ids), QqPending.status == "waiting").update(
                {"status": "building"}, synchronize_session=False)
            if count != len(ids):
                raise RuntimeError("发送批次被其他任务领取，稍后重试")
        try:
            sid = await asyncio.to_thread(create_bundle, mids)
            key = hashlib.sha256(f"{target}:{ids}:{sid}".encode()).hexdigest()
            with session_scope() as s:
                group = s.query(QqGroup).filter_by(group_openid=target).first()
                delivery = QqDelivery(group_openid=target, message_id=mids[0], bundle_id=sid,
                                      bot_id=group.bot_id if group else None,
                                      source="source_message", trigger="subscription",
                                      dedup_key=key, status="pending", parts={})
                s.add(delivery)
                s.flush()
                did = delivery.id
                s.query(QqPending).filter(QqPending.id.in_(ids)).update(
                    {"status": "queued", "delivery_id": did}, synchronize_session=False)
            await deliver(did)
            done += 1
        except Exception:
            with session_scope() as s:
                s.query(QqPending).filter(QqPending.id.in_(ids), QqPending.status == "building").update(
                    {"status": "waiting", "ready_at": now + timedelta(seconds=60)}, synchronize_session=False)
            raise
    return done


async def deliver(did: int) -> None:
    from .commands import Reply
    from .sending import send_parts
    with session_scope() as s:
        row = s.get(QqDelivery, did)
        if row is None:
            return
        if row.status in ("done", "review", "blocked", "failed"):
            return
        group = s.query(QqGroup).filter_by(group_openid=row.group_openid, enabled=True).first()
        if not group:
            row.status, row.error = "failed", "群已停用"
            s.query(QqPending).filter_by(delivery_id=did).update({"status": "failed"})
            return
        sid, target, key = row.bundle_id, row.group_openid, row.dedup_key
        owner = row.bot_id or group.bot_id
    from ..sharing import bundle_url
    from . import client as qq_client
    try:
        bot = qq_client.resolve_bot(bot_id=owner) if owner else qq_client.resolve_bot()
    except qq_client.QqApiError as e:
        with session_scope() as s:
            row = s.get(QqDelivery, did)
            row.status, row.error, row.next_retry_at = "failed", f"机器人不可用: {e.message}", None
            s.query(QqPending).filter_by(delivery_id=did).update({"status": "failed"})
        return
    if not bot.get("proactive_enabled", True):
        with session_scope() as s:
            row = s.get(QqDelivery, did)
            row.status, row.error, row.next_retry_at = "blocked", "主动推送权限已停用", None
        return
    from . import events
    prior = events.delivery_parts(key)
    if not any(part.get("status") in ("done", "unknown", "sending") for part in prior.values()):
        with session_scope() as s:
            mids = [r.message_id for r in s.query(QqPending).filter_by(delivery_id=did)]
        if mids:
            sid = await asyncio.to_thread(create_bundle, mids)
            with session_scope() as s:
                s.get(QqDelivery, did).bundle_id = sid
    try:
        paths, url, items = await asyncio.to_thread(bundle_cards, sid)
    except Exception as exc:
        with session_scope() as s:
            from ..models import ShareToken
            share = s.get(ShareToken, sid)
            url, items = bundle_url(share), list(share.snapshot_items or [])
            s.get(QqDelivery, did).error = f"长图降级为链接: {type(exc).__name__}"
        paths = []
    replies = [Reply(kind="image", thumb_path=p, text=url if i == 0 else "", bundle_id=sid)
               for i, p in enumerate(paths[:4])] or [Reply(text=url, bundle_id=sid)]
    if len(paths) > 4:
        replies.append(Reply(text=url))
    parts = await send_parts(target, replies, key=key, bot=bot)
    if not parts:
        return
    states = [p.get("status") for p in parts.values()]
    permission_error = next((p.get("error") for p in parts.values()
                             if "40034105" in (p.get("error") or "")), "")
    if permission_error:
        # This is account capability, not a transient batch failure. Stop
        # creating new proactive attempts for this bot while preserving the
        # current delivery record for operators to inspect.
        qq_client.disable_proactive(bot.get("id"), permission_error)
    if any(x == "pending" for x in states):
        return
    with session_scope() as s:
        row = s.get(QqDelivery, did)
        row.parts = parts
        row.attempts = (row.attempts or 0) + 1
        row.error = "\n".join(p["error"] for p in parts.values() if p.get("error")) or row.error
        if all(x == "done" for x in states):
            row.status, row.delivered_at, row.next_retry_at = "done", datetime.utcnow(), None
            s.query(QqPending).filter_by(delivery_id=did).update({"status": "done"})
            s.query(MonitorMessage).filter(MonitorMessage.id.in_([x["id"] for x in items])).update(
                {"pushed_at": datetime.utcnow()}, synchronize_session=False)
        elif any(x in ("unknown", "sending") for x in states):
            row.status, row.next_retry_at = "review", None
        elif row.attempts >= 5 or any(code in (row.error or "") for code in ("40034105", "40034101", "40054003")):
            row.status, row.next_retry_at = "failed", None
        else:
            row.status = "retry"
            row.next_retry_at = datetime.utcnow() + timedelta(seconds=min(3600, 30 * 4 ** (row.attempts - 1)))
    if all(x == "done" for x in states):
        from .commands import mark_read
        mark_read(target, [x["id"] for x in items])


async def retry_batches() -> None:
    if not settings.get("QQ_ENABLED"):
        return
    from .events import recover_stale
    recover_stale()
    with session_scope() as s:
        s.query(QqPending).filter(QqPending.status == "building",
            QqPending.ready_at < datetime.utcnow() - timedelta(minutes=5)).update({"status": "waiting"})
        ids = [row.id for row in s.query(QqDelivery).filter(QqDelivery.bundle_id.isnot(None),
            QqDelivery.status.in_(["pending", "retry"])).filter(
            (QqDelivery.next_retry_at.is_(None)) | (QqDelivery.next_retry_at <= datetime.utcnow())).limit(10)]
    for did in ids:
        await deliver(did)
