"""出站：webhook 主动推送（HMAC 签名 + 退避重试）与统一的消息序列化。

序列化格式同时给 JSON API、webhook、RSS 用，保证三个出口看到的是同一份内容。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta

import httpx

from . import settings
from .crypto import decrypt
from .db import session_scope
from .kb.annotate import entity_list
from .models import Channel, MessageMedia, MonitorMessage, Webhook, WebhookDelivery
from .util import log_event

logger = logging.getLogger(__name__)

RETRY_BACKOFF = [30, 120, 600, 3600]  # 秒


def base_url() -> str:
    return (str(settings.get("BASE_URL") or "").rstrip("/") or "")


def media_url(rel: str | None) -> str | None:
    if not rel:
        return None
    return f"{base_url()}/media/{rel}"


def serialize(message_id: int, include_original: bool = True) -> dict | None:
    with session_scope() as s:
        m = s.get(MonitorMessage, message_id)
        if m is None:
            return None
        ch = s.get(Channel, m.channel_id)
        media = (s.query(MessageMedia)
                 .filter(MessageMedia.message_id == m.id)
                 .order_by(MessageMedia.id.asc()).all())
        out = {
            "id": m.id,
            "channel": {
                "id": ch.id if ch else None,
                "title": ch.title if ch else "",
                "username": ch.username if ch else None,
                "game": ch.game if ch else None,
            },
            "tg_message_id": m.tg_message_id,
            "deeplink": m.deeplink,
            "published_at": m.published_at.isoformat() + "Z" if m.published_at else None,
            "text_zh": m.text_zh,
            "translate_status": m.translate_status,
            "glossary_hits": m.glossary_hits or [],
            "glossary_miss": m.glossary_miss or [],
            # 机器人按实体订阅要靠这个字段，不带上就得再打一次详情接口
            "entities": entity_list(m.entities),
            # 按消息判定的游戏。和 channel.game 不是一回事 —— 多游戏频道的
            # channel.game 是空的，分流只能靠这个
            "game_detected": m.game_detected,
            "topics": m.topics or [],
            "version_tag": m.version_tag,
            # 剧透：消费方自己决定怎么处理。ranges 是相对 text_raw 的
            # Python 字符区间，走模型翻译的消息只有 has_spoiler 没有 ranges
            "has_spoiler": bool(m.has_spoiler),
            "spoiler_ranges": m.spoiler_ranges or [],
            "duplicate_of": m.duplicate_of,
            "dup_reason": m.dup_reason,
            "has_media": m.has_media,
            "media": [
                {
                    "kind": x.kind,
                    "thumb": media_url(x.thumb_path),
                    "width": x.width,
                    "height": x.height,
                    "duration": x.duration,
                    "orig_bytes": x.orig_bytes,
                    "mime": x.mime,
                }
                for x in media
            ],
        }
        if include_original:
            out["text_raw"] = m.text_raw
        return out


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _matching_webhooks(channel_id: int, is_dup: bool, force: bool) -> list[dict]:
    with session_scope() as s:
        rows = s.query(Webhook).filter(Webhook.enabled.is_(True)).all()
        out = []
        for w in rows:
            ids = w.channel_ids or []
            if ids and channel_id not in ids:
                continue
            if is_dup and not w.include_duplicates and not force:
                continue
            out.append({"id": w.id, "url": w.url,
                        "secret": decrypt(w.secret) or "",
                        "max_retry": w.max_retry})
        return out


async def _deliver(hook: dict, payload: dict, delivery_id: int) -> bool:
    body = json.dumps({"event": "message", "data": payload},
                      ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8",
               "User-Agent": "tgmon/0.1"}
    if hook["secret"]:
        headers["X-Tgmon-Signature"] = "sha256=" + _sign(hook["secret"], body)
    ok, status, err = False, None, None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(hook["url"], content=body, headers=headers)
            status = resp.status_code
            ok = 200 <= status < 300
            if not ok:
                err = f"HTTP {status}: {resp.text[:300]}"
    except Exception as e:
        err = f"{type(e).__name__}: {e}"

    with session_scope() as s:
        d = s.get(WebhookDelivery, delivery_id)
        if d is not None:
            d.attempts += 1
            d.http_status = status
            d.error = err
            if ok:
                d.status = "done"
                d.delivered_at = datetime.utcnow()
                d.next_retry_at = None
            elif d.attempts >= hook["max_retry"]:
                d.status = "failed"
                d.next_retry_at = None
            else:
                d.status = "retry"
                delay = RETRY_BACKOFF[min(d.attempts - 1, len(RETRY_BACKOFF) - 1)]
                d.next_retry_at = datetime.utcnow() + timedelta(seconds=delay)
    if not ok:
        logger.warning("webhook 投递失败 %s: %s", hook["url"], err)
    return ok


async def push_message(message_id: int, force: bool = False) -> int:
    """推一条消息到所有匹配的 webhook。返回成功数。"""
    if not settings.get("WEBHOOK_ENABLED") and not force:
        return 0
    with session_scope() as s:
        m = s.get(MonitorMessage, message_id)
        if m is None:
            return 0
        channel_id, is_dup = m.channel_id, m.duplicate_of is not None
        ch = s.get(Channel, channel_id)
        force_push = bool(ch and ch.force_push)

    hooks = _matching_webhooks(channel_id, is_dup, force or force_push)
    if not hooks:
        return 0
    payload = serialize(message_id)
    if payload is None:
        return 0

    done = 0
    for hook in hooks:
        with session_scope() as s:
            d = WebhookDelivery(webhook_id=hook["id"], message_id=message_id,
                                status="pending")
            s.add(d)
            s.flush()
            did = d.id
        if await _deliver(hook, payload, did):
            done += 1

    if done:
        with session_scope() as s:
            m = s.get(MonitorMessage, message_id)
            if m is not None:
                m.pushed_at = datetime.utcnow()
    return done


async def retry_pending_webhooks() -> int:
    """维护循环调用。重试到期的失败投递。"""
    now = datetime.utcnow()
    with session_scope() as s:
        rows = (s.query(WebhookDelivery)
                .filter(WebhookDelivery.status == "retry",
                        WebhookDelivery.next_retry_at.isnot(None),
                        WebhookDelivery.next_retry_at <= now)
                .limit(20).all())
        pending = [(r.id, r.webhook_id, r.message_id) for r in rows]
    if not pending:
        return 0

    n = 0
    for did, wid, mid in pending:
        with session_scope() as s:
            w = s.get(Webhook, wid)
            if w is None or not w.enabled:
                d = s.get(WebhookDelivery, did)
                if d:
                    d.status = "failed"
                    d.error = "webhook 已删除或停用"
                continue
            hook = {"id": w.id, "url": w.url, "secret": decrypt(w.secret) or "",
                    "max_retry": w.max_retry}
        payload = serialize(mid) if mid else None
        if payload is None:
            with session_scope() as s:
                d = s.get(WebhookDelivery, did)
                if d:
                    d.status = "failed"
                    d.error = "消息已删除"
            continue
        if await _deliver(hook, payload, did):
            n += 1
    return n


async def test_webhook(webhook_id: int) -> dict:
    """手动触发一次推送，方便调机器人。"""
    with session_scope() as s:
        w = s.get(Webhook, webhook_id)
        if w is None:
            return {"ok": False, "error": "webhook 不存在"}
        hook = {"id": w.id, "url": w.url, "secret": decrypt(w.secret) or "",
                "max_retry": 1}
        d = WebhookDelivery(webhook_id=w.id, message_id=None, status="pending")
        s.add(d)
        s.flush()
        did = d.id

    payload = {"id": 0, "channel": {"title": "tgmon 测试"},
               "text_zh": "这是一条来自 tgmon 的测试推送。",
               "text_raw": "This is a test push from tgmon.",
               "deeplink": None, "media": [], "has_media": False}
    ok = await _deliver(hook, payload, did)
    with session_scope() as s:
        d = s.get(WebhookDelivery, did)
        err = d.error if d else None
        code = d.http_status if d else None
    if not ok:
        log_event("warning", "webhook", f"测试推送失败: {err}")
    return {"ok": ok, "http_status": code, "error": err}
