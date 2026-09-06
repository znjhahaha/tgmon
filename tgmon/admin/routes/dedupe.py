"""⑧ 去重复查 —— 判重记录列表、展开看在哪些频道出现过、人工翻案、阈值调节。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func

from ... import settings
from ...db import session_scope
from ...models import Channel, MonitorMessage
from ...util import opt_int
from ..deps import render, require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dedupe")

PAGE_SIZE = 30


def _load(page: int, reason: str) -> dict:
    offset = max(0, page - 1) * PAGE_SIZE
    with session_scope() as s:
        q = s.query(MonitorMessage).filter(MonitorMessage.duplicate_of.isnot(None))
        if reason:
            q = q.filter(MonitorMessage.dup_reason == reason)
        total = q.with_entities(func.count(MonitorMessage.id)).scalar() or 0
        rows = (q.order_by(MonitorMessage.published_at.desc())
                .offset(offset).limit(PAGE_SIZE).all())
        names = {c.id: c.title for c in s.query(Channel).all()}
        items = []
        for r in rows:
            first = s.get(MonitorMessage, r.duplicate_of)
            others = (s.query(MonitorMessage)
                      .filter(MonitorMessage.duplicate_of == r.duplicate_of)
                      .all())
            appeared = [names.get(first.channel_id, "?")] if first else []
            appeared += [names.get(x.channel_id, "?") for x in others]
            items.append({
                "id": r.id, "channel": names.get(r.channel_id, "?"),
                "reason": r.dup_reason,
                "text": (r.text_raw or "")[:180],
                "published_at": r.published_at,
                "first_id": r.duplicate_of,
                "first_channel": names.get(first.channel_id, "?") if first else "?",
                "first_at": first.published_at if first else None,
                "appeared": appeared,
                "count": len(set(appeared)),
            })
        by_reason = dict(s.query(MonitorMessage.dup_reason,
                                 func.count(MonitorMessage.id))
                         .filter(MonitorMessage.duplicate_of.isnot(None))
                         .group_by(MonitorMessage.dup_reason).all())
        overridden = (s.query(func.count(MonitorMessage.id))
                      .filter(MonitorMessage.dup_overridden.is_(True))
                      .scalar() or 0)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return {"items": items, "total": total, "page": page, "pages": pages,
            "by_reason": by_reason, "overridden": overridden}


@router.get("")
async def page_view(request: Request, page: str = "1", reason: str = "",
                    user: str = Depends(require_admin)):
    data = _load(max(1, opt_int(page) or 1), reason)
    return render(request, "dedupe.html", {
        "nav_active": "dedupe", **data, "reason": reason,
        "cfg": {
            "enabled": settings.get("DEDUP_ENABLED"),
            "window_days": settings.get("DEDUP_WINDOW_DAYS"),
            "simhash": settings.get("SIMHASH_DISTANCE"),
            "phash": settings.get("PHASH_DISTANCE"),
            "min_len": settings.get("DEDUP_MIN_TEXT_LEN"),
        },
    })


@router.post("/config")
async def save_config(request: Request,
                      dedup_enabled: str = Form(""),
                      window_days: int = Form(7),
                      simhash: int = Form(3),
                      phash: int = Form(5),
                      min_len: int = Form(20),
                      user: str = Depends(require_admin)):
    settings.set_many({
        "DEDUP_ENABLED": dedup_enabled.strip().lower() in ("on", "true", "1"),
        "DEDUP_WINDOW_DAYS": max(1, window_days),
        "SIMHASH_DISTANCE": max(0, min(simhash, 32)),
        "PHASH_DISTANCE": max(0, min(phash, 32)),
        "DEDUP_MIN_TEXT_LEN": max(0, min_len),
    })
    return HTMLResponse(
        "<div class='flash ok'>阈值已保存，对之后的新消息生效</div>"
        "<div class='hint'>阈值调大 = 更容易判重（可能误杀）；调小 = 更容易漏判。"
        "SimHash 3 与 pHash 5 是比较稳的起点。</div>")


@router.post("/{mid}/release")
async def release(mid: int, request: Request, user: str = Depends(require_admin)):
    """翻案：放行这条并标记为人工确认过，之后不再自动判重。"""
    with session_scope() as s:
        row = s.get(MonitorMessage, mid)
        if row is None:
            return HTMLResponse("<span class='err'>不存在</span>")
        row.duplicate_of = None
        row.dup_reason = None
        row.dup_overridden = True
    return HTMLResponse("<span class='ok'>已放行</span>")
