"""⑦ 消息浏览 —— 时间线，看原文/译文/缩略图/术语命中，可重译、重推、标记误判。"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import Text, func, or_

from ... import classify
from ...db import session_scope
from ...kb.annotate import entity_list
from ...models import (
    Channel, GlossaryEntry, MessageMedia, MonitorMessage, ShareToken,
)
from ...util import opt_int, spoiler_segments
from ..deps import (
    get_task, guest_rate_limit, render, require_admin, require_login,
    require_login_api, require_worker, submit_task,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/messages")

PAGE_SIZE = 25


def _query(s, channel_id: int | None, q: str, dup: str, status: str,
           topic: str = "", version: str = "", game: str = ""):
    from ...message_query import query as message_query
    query = message_query(s, game=game, keyword=q, duplicates=dup,
                          channel_ids=[channel_id] if channel_id else None)
    if status:
        query = query.filter(MonitorMessage.translate_status == status)
    if topic:
        # topics 是 JSON 数组列，SQLite 没法索引查。LIKE 带引号匹配元素
        # （ '%"卡池"%' 不会命中「新卡池」之类的前缀串），量小无性能问题
        query = query.filter(
            MonitorMessage.topics.isnot(None),
            MonitorMessage.topics.cast(Text).like(f'%"{topic}"%'))
    if version:
        # 前缀匹配：输 7.1 也能带出 7.1.x
        query = query.filter(MonitorMessage.version_tag.like(f"{version}%"))
    return query


def _row_view(r, media) -> dict:
    """列表行 / 详情共用的字段投影。"""
    aligned = bool(r.spoiler_ranges) and (r.text_zh or "") == r.text_raw
    return {
        "id": r.id, "channel_id": r.channel_id,
        "text_raw": r.text_raw, "text_zh": r.text_zh,
        "translate_status": r.translate_status,
        "translate_error": r.translate_error,
        "provider_name": r.provider_name, "model": r.model,
        "from_cache": r.from_cache,
        "glossary_hits": r.glossary_hits or [],
        "glossary_miss": r.glossary_miss or [],
        "lang_detected": r.lang_detected,
        "text_dropped": r.text_dropped,
        "game_detected": r.game_detected,
        "game_scores": r.game_scores or None,
        "topics": r.topics or [],
        "version_tag": r.version_tag,
        # 剧透。ranges 相对 text_raw；只有显示文本就是原文时才对得上
        # （zh_first 跳过翻译时 text_zh==text_raw）。走模型翻译的消息
        # 译文被重写，区间无法对齐，只打标记不标段落
        "has_spoiler": r.has_spoiler,
        "spoiler_ranges": r.spoiler_ranges or [],
        "spoiler_aligned": aligned,
        "zh_segments": (spoiler_segments(r.text_zh, r.spoiler_ranges)
                        if aligned and r.text_zh else None),
        "raw_segments": (spoiler_segments(r.text_raw, r.spoiler_ranges)
                         if r.spoiler_ranges else None),
        "entities": entity_list(r.entities),
        "duplicate_of": r.duplicate_of, "dup_reason": r.dup_reason,
        "dup_overridden": r.dup_overridden,
        "deeplink": r.deeplink, "published_at": r.published_at,
        "cost": r.cost,
        "media": [{
            "id": m.id, "kind": m.kind, "thumb": m.thumb_path,
            "duration": m.duration, "orig_bytes": m.orig_bytes,
            "width": m.width, "height": m.height,
            "has_spoiler": m.has_spoiler,
            "video_status": m.video_status, "video_path": m.video_path,
            "video_bytes": m.video_bytes,
        } for m in media],
    }


def _load(page: int, channel_id: int | None, q: str, dup: str,
          status: str, topic: str = "", version: str = "",
          game: str = "") -> dict:
    offset = max(0, (page - 1)) * PAGE_SIZE
    with session_scope() as s:
        total = _query(s, channel_id, q, dup, status, topic, version, game)\
            .with_entities(func.count(MonitorMessage.id)).scalar() or 0
        rows = (_query(s, channel_id, q, dup, status, topic, version, game)
                .order_by(MonitorMessage.published_at.desc())
                .offset(offset).limit(PAGE_SIZE).all())
        chan_names = {c.id: c.title for c in s.query(Channel).all()}
        out = []
        for r in rows:
            media = (s.query(MessageMedia)
                     .filter(MessageMedia.message_id == r.id).all())
            v = _row_view(r, media)
            v["channel"] = chan_names.get(r.channel_id, "?")
            out.append(v)
        channels = [{"id": c.id, "title": c.title}
                    for c in s.query(Channel)
                    .filter(Channel.enabled.is_(True))
                    .order_by(Channel.title.asc()).all()]
        # 游戏下拉选项：消息侧判定的 ∪ 频道侧归属的。两个来源都要，
        # 只取一个会漏 —— Seele Leaks 这类频道 channel.game 为空，
        # 它的消息全靠 game_detected 才能按游戏筛
        games = {g for g, in s.query(MonitorMessage.game_detected).distinct()
                 if g}
        games |= {g for g, in s.query(Channel.game).distinct() if g}
        # 分享链接常驻渲染（2026-09 反馈：刷新后链接不可见）——
        # 页面加载时直接带出每条消息的已有链接。share 的渲染函数
        # 延迟导入：share.py 顶层 import 了本模块的 _row_view
        from .share import _links_fragment
        if rows:
            ids = [r.id for r in rows]
            sh_counts = dict(
                s.query(ShareToken.message_id,
                        func.count(ShareToken.id))
                .filter(ShareToken.message_id.in_(ids))
                .group_by(ShareToken.message_id).all())
            for v in out:
                n = sh_counts.get(v["id"], 0)
                v["share_count"] = n
                v["share_links_html"] = _links_fragment(v["id"]) if n else ""
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return {"messages": out, "total": total, "page": page, "pages": pages,
            "channels": channels, "game_options": sorted(games)}


@router.get("")
async def page_view(request: Request, page: str = "1",
                    channel_id: str = "", q: str = "", dup: str = "hide",
                    status: str = "", topic: str = "", version: str = "",
                    game: str = "",
                    user: str = Depends(require_login),
                    _rl: None = Depends(guest_rate_limit)):
    # page/channel_id 用 str 接收再 opt_int：空串（表单「全部」选项）会 422
    # dup 默认 hide：重复消息不进主列表（2026-09 反馈），专门界面在 /dedupe
    pg = max(1, opt_int(page) or 1)
    cid = opt_int(channel_id)
    data = _load(pg, cid, q, dup, status, topic, version, game)
    return render(request, "messages.html", {
        "nav_active": "messages", **data, "q": q, "dup": dup,
        "status": status, "channel_id": cid,
        "topic": topic, "version": version, "game": game,
        "topic_options": classify.topic_names(),
    })


@router.get("/select-ids")
async def select_ids(request: Request, channel_id: str = "", q: str = "",
                     dup: str = "hide", status: str = "", topic: str = "",
                     version: str = "", game: str = "",
                     user: str = Depends(require_admin)):
    """Return at most 50 ids matching the current message filters.

    The endpoint deliberately returns only identifiers, allowing the browser to
    keep a cross-page selection without moving message content into JavaScript.
    """
    cid = opt_int(channel_id)
    with session_scope() as s:
        ids = [x[0] for x in (_query(s, cid, q, dup, status, topic, version, game)
                              .order_by(MonitorMessage.published_at.desc())
                              .with_entities(MonitorMessage.id)
                              .limit(_BULK_MAX).all())]
        image_count = 0
        if ids:
            image_count = s.query(MessageMedia).filter(MessageMedia.message_id.in_(ids),
                                                        MessageMedia.thumb_path.isnot(None)).count()
    estimated_pages = max(1, (len(ids) + 9) // 10) if ids else 0
    return JSONResponse({"ids": ids, "count": len(ids), "image_count": image_count,
                         "estimated_pages": estimated_pages, "limit": _BULK_MAX})


@router.post("/{mid}/retranslate")
async def retranslate(mid: int, request: Request,
                      user: str = Depends(require_admin)):
    require_worker()
    tid = submit_task("retranslate", {"message_id": mid})
    waited = 0.0
    while waited < 150.0:
        t = get_task(tid)
        if t and t["status"] == "failed":
            return HTMLResponse(f"<span class='err'>{t['error']}</span>")
        if t and t["status"] == "done":
            r = t["result"] or {}
            if r.get("ok"):
                return HTMLResponse(
                    "<span class='ok'>已重译，刷新看结果</span>")
            return HTMLResponse(
                f"<span class='err'>{r.get('error') or r.get('status')}</span>")
        await asyncio.sleep(1.2)
        waited += 1.2
    return HTMLResponse("<span class='err'>重译超时</span>")


@router.post("/{mid}/repush")
async def repush(mid: int, request: Request, user: str = Depends(require_admin)):
    require_worker()
    tid = submit_task("repush", {"message_id": mid})
    waited = 0.0
    while waited < 60.0:
        t = get_task(tid)
        if t and t["status"] == "done":
            n = (t["result"] or {}).get("delivered", 0)
            return HTMLResponse(f"<span class='ok'>已推给 {n} 个 webhook</span>")
        if t and t["status"] == "failed":
            return HTMLResponse(f"<span class='err'>{t['error']}</span>")
        await asyncio.sleep(1.0)
        waited += 1.0
    return HTMLResponse("<span class='err'>推送超时</span>")


@router.post("/{mid}/unmark-dup")
async def unmark_dup(mid: int, request: Request,
                     user: str = Depends(require_admin)):
    """人工翻案：误判的放行。"""
    with session_scope() as s:
        row = s.get(MonitorMessage, mid)
        if row is None:
            return HTMLResponse("<span class='err'>不存在</span>")
        row.duplicate_of = None
        row.dup_overridden = True
    return HTMLResponse("<span class='ok'>已放行</span>")


# ---------------- 批量操作（2026-09 多选升级） ----------------
# 合并分享的路由在 share.py（POST /messages/bulk-share）——它要生成
# token，和 share 的生成逻辑放一起。批量上限与防呆见各路由。

_BULK_MAX = 50


def _parse_ids_str(raw: str) -> list[int] | None:
    """解析 ids（JSON 数组或逗号分隔）。非法返回 None，空返回 []。"""
    import json as _json
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        vals = (_json.loads(raw) if raw.startswith("[")
                else raw.split(","))
        return sorted({int(x) for x in vals if str(x).strip()})
    except (ValueError, TypeError):
        return None


@router.post("/bulk-repush")
async def bulk_repush(request: Request,
                      user: str = Depends(require_admin)):
    """批量重推 webhook：逐条提交 repush 任务（worker 异步执行）。

    路径用两段（/messages/bulk-repush）—— 不能写成 /messages/bulk/repush：
    三段会和先注册的 /messages/{mid}/repush 撞（mid 匹配 "bulk" 后
    int 转换失败 422，Starlette 不 fallthrough）。
    """
    require_worker()
    form = await request.form()
    ids = _parse_ids_str(str(form.get("ids") or ""))
    if ids is None:
        return HTMLResponse("<span class='err'>消息 id 列表格式不对</span>")
    if not ids:
        return HTMLResponse("<span class='err'>没有选中消息</span>")
    if len(ids) > _BULK_MAX:
        return HTMLResponse(
            f"<span class='err'>一次最多 {_BULK_MAX} 条（选中 {len(ids)} 条）</span>")
    # 只推真实存在的消息
    with session_scope() as s:
        found = {r.id for r in s.query(MonitorMessage.id)
                 .filter(MonitorMessage.id.in_(ids)).all()}
    n = 0
    for mid in ids:
        if mid in found:
            submit_task("repush", {"message_id": mid})
            n += 1
    return HTMLResponse(
        f"<span class='ok'>已提交 {n} 条重推任务（worker 侧逐条执行）</span>")


@router.post("/bulk-unmark-dup")
async def bulk_unmark_dup(request: Request,
                          user: str = Depends(require_admin)):
    """批量误判放行：只对 duplicate_of 非空的生效。

    同 bulk-repush：两段路径避免与 /messages/{mid}/unmark-dup 撞。
    """
    form = await request.form()
    ids = _parse_ids_str(str(form.get("ids") or ""))
    if ids is None:
        return HTMLResponse("<span class='err'>消息 id 列表格式不对</span>")
    if not ids:
        return HTMLResponse("<span class='err'>没有选中消息</span>")
    if len(ids) > _BULK_MAX:
        return HTMLResponse(
            f"<span class='err'>一次最多 {_BULK_MAX} 条（选中 {len(ids)} 条）</span>")
    with session_scope() as s:
        rows = (s.query(MonitorMessage)
                .filter(MonitorMessage.id.in_(ids),
                        MonitorMessage.duplicate_of.isnot(None)).all())
        for row in rows:
            row.duplicate_of = None
            row.dup_overridden = True
        n = len(rows)
    return HTMLResponse(
        f"<span class='ok'>已放行 {n} 条"
        + (f"（{len(ids) - n} 条本就不是重复，跳过）" if n < len(ids) else "")
        + "</span>")


@router.get("/{mid}/detail")
async def detail(mid: int, request: Request,
                 user: str = Depends(require_login_api),
                 _rl: None = Depends(guest_rate_limit)):
    with session_scope() as s:
        r = s.get(MonitorMessage, mid)
        if r is None:
            return HTMLResponse("<div class='flash err'>消息不存在</div>")
        ch = s.get(Channel, r.channel_id)
        media = (s.query(MessageMedia)
                 .filter(MessageMedia.message_id == r.id).all())
        dup_siblings = []
        if r.duplicate_of:
            sib = (s.query(MonitorMessage)
                   .filter(or_(MonitorMessage.duplicate_of == r.duplicate_of,
                               MonitorMessage.id == r.duplicate_of))
                   .all())
            names = {c.id: c.title for c in s.query(Channel).all()}
            dup_siblings = [{"id": x.id, "channel": names.get(x.channel_id, "?"),
                             "published_at": x.published_at} for x in sib]
        kb_games = sorted({g for g, in s.query(GlossaryEntry.game).distinct()
                           if g})
        m = _row_view(r, media)
        m.update({
            "channel": ch.title if ch else "?",
            "text_hash": r.text_hash, "simhash": r.simhash,
            "tokens_in": r.tokens_in, "tokens_out": r.tokens_out,
        })
        media_view = [{
            "kind": x.kind, "thumb": x.thumb_path,
            "phash": x.phash, "dhash": x.dhash,
            "duration": x.duration, "orig_bytes": x.orig_bytes,
            "thumb_bytes": x.thumb_bytes,
            "width": x.width, "height": x.height,
            "has_spoiler": x.has_spoiler,
            "video_status": x.video_status, "video_path": x.video_path,
            "video_bytes": x.video_bytes,
        } for x in media]
    return render(request, "fragments/message_detail.html", {
        "m": m, "media": media_view, "siblings": dup_siblings,
        "kb_games": kb_games,
    })
