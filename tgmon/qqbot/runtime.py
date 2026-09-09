"""Durable QQ callback processing and request-bound background cards."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from .. import jobs, settings
from ..conversation_scope import bot_identity
from . import events

logger = logging.getLogger(__name__)


class PendingReply(RuntimeError):
    pass


def recover_conversation(payload: dict) -> str:
    from ..db import session_scope
    from ..models import QqEvent
    event = payload["event"]
    data = event.get("d") or {}
    private = str(event.get("t") or event.get("type") or "") == "C2C_MESSAGE_CREATE"
    target = (data.get("author") or {}).get("user_openid", "") if private else data.get("group_openid", "")
    key = events.event_key("c2c" if private else "group", str(target), str(data.get("id") or ""),
                           str(payload.get("app_id") or payload.get("bot_id") or ""))
    with session_scope() as s:
        row = s.get(QqEvent, key)
        if row and row.status == "processing":
            # The durable job lease is exclusive; a crashed generation has no
            # acknowledged send and can be generated again immediately.
            s.delete(row)
        elif row and row.status == "delivering":
            parts = {name: dict(part) for name, part in (row.parts or {}).items()}
            for part in parts.values():
                if part.get("status") == "sending":
                    part.update(status="unknown", error="发送进程中断，结果待核实")
            row.parts = parts
            row.status = "review" if any(p.get("status") == "unknown" for p in parts.values()) else "ready"
    return key


def deadline_for(data: dict) -> datetime:
    now = datetime.utcnow()
    try:
        stamp = datetime.fromisoformat(str(data.get("timestamp") or "").replace("Z", "+00:00"))
        if stamp.tzinfo:
            stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None)
        stamp = min(stamp, now)
    except ValueError:
        stamp = now
    return stamp + timedelta(seconds=int(settings.get("QQ_REPLY_WINDOW_SECONDS") or 300) - 10)


async def receive(event: dict, bot: dict | None, source: str) -> None:
    kind = str(event.get("t") or event.get("type") or "")
    if kind not in ("GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"):
        from .. import qqbot
        await qqbot.handle_callback_event(event, bot=bot)
        return
    data = event.get("d") or {}
    private = kind == "C2C_MESSAGE_CREATE"
    target = str((data.get("author") or {}).get("user_openid") or "") if private else str(data.get("group_openid") or "")
    mid = str(data.get("id") or "")
    if not mid or not target:
        return
    key = events.event_key("c2c" if private else "group", target, mid, bot_identity(bot))
    await asyncio.to_thread(jobs.enqueue, "conversation", f"qq:{key}",
        {"event": event, "bot_id": (bot or {}).get("id"),
         "app_id": (bot or {}).get("app_id"), "source": source,
         "deadline": deadline_for(data).isoformat()},
        scope_key=f"qq:{bot_identity(bot)}:{target}")


async def run_queue(queue: str) -> None:
    from . import client
    from ..admin.routes.qqbot_cb import _handle_and_reply
    while True:
        try:
            job = await asyncio.to_thread(jobs.claim, queue)
        except Exception:
            logger.exception("QQ queue claim failed")
            await asyncio.sleep(1)
            continue
        if not job:
            await asyncio.sleep(0.25)
            continue
        heartbeat = asyncio.create_task(jobs.heartbeat(job))
        try:
            payload = job["payload"]
            deadline = datetime.fromisoformat(payload["deadline"])
            if datetime.utcnow() >= deadline:
                await asyncio.to_thread(jobs.finish, job, {"status": "expired"})
                continue
            bot = client.resolve_bot(bot_id=payload.get("bot_id"), app_id=payload.get("app_id"))
            if queue == "conversation":
                key = await asyncio.to_thread(recover_conversation, payload) if job["attempts"] > 1 else None
                await _handle_and_reply(payload["event"], bot, payload["source"], deadline=deadline)
                if key is None:
                    data = payload["event"].get("d") or {}
                    kind = payload["event"].get("t") or payload["event"].get("type")
                    private = kind == "C2C_MESSAGE_CREATE"
                    target = (data.get("author") or {}).get("user_openid", "") if private else data.get("group_openid", "")
                    key = events.event_key("c2c" if private else "group", str(target), str(data.get("id") or ""), bot_identity(bot))
            else:
                key = await send_cards(payload, bot, deadline)
            parts = events.delivery_parts(key)
            if any(part.get("status") in ("unknown", "sending") for part in parts.values()):
                await asyncio.to_thread(jobs.finish, job, review=True, error="发送结果待核实")
                continue
            if any(part.get("status") in ("failed", "pending") for part in parts.values()):
                raise RuntimeError("被动回复未完成")
            await asyncio.to_thread(jobs.finish, job)
        except PendingReply as exc:
            await asyncio.to_thread(jobs.finish, job, error=str(exc), retry_after=2)
        except Exception as exc:
            logger.exception("QQ %s job failed", queue)
            await asyncio.to_thread(jobs.finish, job, error=str(exc),
                                     retry_after=5 if job["attempts"] < 3 else None)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)


async def send_cards(payload, bot, deadline):
    from ..sharing import bundle_cards, create_bundle
    from .commands import Reply
    from .sending import send_parts
    parent = events.event_key("c2c" if payload["private"] else "group", payload["target"],
                              payload["msg_id"], bot_identity(bot))
    parts = events.delivery_parts(parent)
    if parts.get("1", {}).get("status") != "done":
        raise PendingReply("原请求的首条回复尚未确认")
    key = events.event_key("c2c" if payload["private"] else "group", payload["target"],
                           f"{payload['msg_id']}:cards", bot_identity(bot))
    prior = events.delivery_parts(key)
    sid = payload["sid"]
    events.set_reply_deadline(key, deadline)
    from ..db import session_scope
    from ..models import QqEvent
    with session_scope() as s:
        sid = (s.get(QqEvent, key).result or {}).get("bundle_id") or sid
    if not any(part.get("status") in ("done", "unknown", "sending") for part in prior.values()):
        sid = await asyncio.to_thread(create_bundle, payload["mids"])
        with session_scope() as s:
            s.get(QqEvent, key).result = {"bundle_id": sid}
    paths, _, _ = await asyncio.to_thread(bundle_cards, sid)
    replies = [Reply(kind="image", thumb_path=path, bundle_id=sid,
                     story_ids=payload["mids"]) for path in paths[:4]]
    await send_parts(payload["target"], replies, key=key, private=payload["private"],
                     msg_id=payload["msg_id"], bot=bot, seq_start=2)
    return key
