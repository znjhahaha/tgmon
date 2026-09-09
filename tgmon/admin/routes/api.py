"""JSON API —— 给机器人调。API Key 鉴权（Header）+ 速率限制。"""
from __future__ import annotations

import hashlib
import logging
import time
from collections import defaultdict, deque
from datetime import datetime

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Text, func, or_

from ... import outputs, settings
from ...db import session_scope
from ...kb.annotate import entity_names
from ...models import ApiKey, Channel, MonitorMessage
from ..deps import current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

# key_id -> 最近请求时间戳。进程内限流，够用且零依赖
_hits: dict[int, deque] = defaultdict(deque)


def auth(request: Request, x_api_key: str | None) -> int | None:
    """返回 key id。已登录后台的浏览器会话也放行，方便自己点着看。"""
    if current_user(request):
        return None
    if not x_api_key:
        raise HTTPException(401, "缺少 X-API-Key 请求头")
    digest = hashlib.sha256(x_api_key.strip().encode()).hexdigest()
    with session_scope() as s:
        row = s.query(ApiKey).filter(ApiKey.key_hash == digest).first()
        if row is None or not row.enabled:
            raise HTTPException(401, "API Key 无效或已吊销")
        row.use_count += 1
        row.last_used_at = datetime.utcnow()
        kid, rate = row.id, row.rate_per_min
    _rate_limit(kid, rate)
    return kid


def _rate_limit(key_id: int, rate: int) -> None:
    limit = rate or int(settings.get("API_RATE_PER_MIN") or 120)
    now = time.monotonic()
    q = _hits[key_id]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, f"超过速率限制 {limit}/分钟")
    q.append(now)


@router.get("/messages")
async def list_messages(request: Request,
                        channel_id: int | None = None,
                        game: str | None = None,
                        theme: str | None = None,
                        q: str | None = None,
                        since: str | None = Query(None, description="ISO 时间"),
                        include_duplicates: bool = False,
                        status: str | None = None,
                        entity: str | None = Query(None, description="按实体名过滤"),
                        limit: int = Query(50, ge=1, le=200),
                        offset: int = Query(0, ge=0),
                        x_api_key: str | None = Header(None, alias="X-API-Key")):
    auth(request, x_api_key)
    with session_scope() as s:
        from ...message_query import query as message_query
        query = message_query(s, game=game or "", keyword=q or "",
                              theme=theme or "",
                              channel_ids=[channel_id] if channel_id else None,
                              duplicates="all" if include_duplicates else "hide")
        if status:
            query = query.filter(MonitorMessage.translate_status == status)
        if since:
            try:
                dt = datetime.fromisoformat(since.replace("Z", ""))
                query = query.filter(MonitorMessage.published_at >= dt)
            except ValueError:
                raise HTTPException(400, "since 格式不对，用 ISO 8601")
        if entity:
            # entities 是 JSON 数组，SQLite 没法索引查它。LIKE 粗筛把候选缩小，
            # 再在 Python 里精确比对 name —— 否则「甘雨」会匹配到「甘雨的信」。
            # 分页必须在精确过滤之后做，所以这条路径不能用 SQL 的 offset/limit
            cand = (query.with_entities(MonitorMessage.id, MonitorMessage.entities)
                    .filter(MonitorMessage.entities.isnot(None),
                            MonitorMessage.entities.cast(Text).like(f"%{entity}%"))
                    .order_by(MonitorMessage.published_at.desc()).all())
            matched = [r[0] for r in cand if entity in entity_names(r[1])]
            total = len(matched)
            ids = matched[offset:offset + limit]
        else:
            total = query.with_entities(func.count(MonitorMessage.id)).scalar() or 0
            ids = [r[0] for r in query.with_entities(MonitorMessage.id)
                   .order_by(MonitorMessage.published_at.desc())
                   .offset(offset).limit(limit).all()]

    items = outputs.serialize_many(ids)
    return JSONResponse({"total": total, "limit": limit, "offset": offset,
                         "items": items})


@router.get("/messages/{mid}")
async def get_message(mid: int, request: Request,
                      x_api_key: str | None = Header(None, alias="X-API-Key")):
    auth(request, x_api_key)
    data = outputs.serialize(mid)
    if data is None:
        raise HTTPException(404, "消息不存在")
    return JSONResponse(data)


@router.get("/search")
async def search_messages(request: Request, q: str = "", game: str | None = None,
                          channel_id: int | None = None, limit: int = Query(8, ge=1, le=100),
                          since: str | None = None,
                          entity: str | None = None,
                          x_api_key: str | None = Header(None, alias="X-API-Key")):
    auth(request, x_api_key)
    from ...retrieval import search
    dt = None
    if since:
        try:
            dt = datetime.fromisoformat(since.replace("Z", ""))
        except ValueError:
            raise HTTPException(400, "since 格式不对，用 ISO 8601")
    hits = search(q, game=game, channel_id=channel_id, since=dt, limit=limit, entity=entity)
    return JSONResponse({"items": [{**h.__dict__, "published_at": h.published_at.isoformat() if h.published_at else None} for h in hits]})


@router.get("/messages/{mid}/related")
async def related_messages(mid: int, request: Request,
                           limit: int = Query(6, ge=1, le=50),
                           x_api_key: str | None = Header(None, alias="X-API-Key")):
    auth(request, x_api_key)
    from ...retrieval import related
    return JSONResponse({"items": [{**h.__dict__, "published_at": h.published_at.isoformat() if h.published_at else None} for h in related(mid, limit=limit)]})


@router.get("/channels")
async def list_channels(request: Request, only_enabled: bool = True,
                        x_api_key: str | None = Header(None, alias="X-API-Key")):
    auth(request, x_api_key)
    with session_scope() as s:
        query = s.query(Channel)
        if only_enabled:
            query = query.filter(Channel.enabled.is_(True))
        rows = query.order_by(Channel.title.asc()).all()
        return JSONResponse({"items": [{
            "id": c.id, "title": c.title, "username": c.username,
            "game": c.game, "enabled": c.enabled, "kind": c.kind,
            "message_count": c.message_count,
            "last_message_at": (c.last_message_at.isoformat() + "Z"
                                if c.last_message_at else None),
        } for c in rows]})


@router.get("/stats")
async def stats(request: Request,
                x_api_key: str | None = Header(None, alias="X-API-Key")):
    auth(request, x_api_key)
    from ..deps import worker_status
    with session_scope() as s:
        total = s.query(func.count(MonitorMessage.id)).scalar() or 0
        dups = (s.query(func.count(MonitorMessage.id))
                .filter(MonitorMessage.duplicate_of.isnot(None)).scalar() or 0)
    st = worker_status()
    return JSONResponse({"messages": total, "duplicates": dups,
                         "worker_online": st["online"],
                         "worker_status": st["status"],
                         "queue_depth": st["queue_depth"]})


@router.post("/webhook/test")
async def webhook_test(request: Request, webhook_id: int,
                       x_api_key: str | None = Header(None, alias="X-API-Key")):
    """手动触发一次推送，方便调机器人。"""
    auth(request, x_api_key)
    return JSONResponse(await outputs.test_webhook(webhook_id))
