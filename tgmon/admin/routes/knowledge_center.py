"""Operational controls for the shared knowledge, memory and delivery services."""
from datetime import datetime
from html import escape

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func

from ... import settings
from ...db import session_scope
from ...models import (Conversation, KnowledgePage, KnowledgeSource, MemberProfile,
                       MonitorMessage, QqDelivery, QqEvent, QqGroup, RetrievalIndex, Task)
from ..deps import require_admin, require_worker, submit_task, redirect

router = APIRouter()
TABS = (("terms", "资料与术语"), ("sources", "来源同步"), ("retrieval", "检索与索引"),
        ("memory", "对话记忆"), ("diagnostics", "诊断"))
GAMES = ("原神", "崩坏:星穹铁道", "绝区零", "崩坏3")


def context(tab):
    from ... import embeddings
    with session_scope() as s:
        data = {"center_tabs": TABS, "center_games": GAMES,
                "documents": s.query(KnowledgePage).order_by(KnowledgePage.id.desc()).limit(30).all() if tab == "terms" else [],
                "memories": s.query(Conversation).order_by(Conversation.updated_at.desc()).limit(100).all() if tab == "memory" else [],
                # 记忆 v2：成员档案（昵称 + 个人事实），面板直接展示
                "member_profiles": s.query(MemberProfile).order_by(MemberProfile.updated_at.desc()).limit(100).all() if tab == "memory" else [],
                "member_nicknames": {p.member_openid: (p.nickname or "").strip()
                                     for p in s.query(MemberProfile).all()
                                     if (p.nickname or "").strip()} if tab == "memory" else {},
                "jobs": s.query(Task).filter(Task.kind.in_(("unified_backfill", "knowledge_index", "wiki_sync", "kb_smart_sync"))).order_by(Task.id.desc()).limit(20).all(),
                "index_counts": dict(s.query(RetrievalIndex.ref_type, func.count()).group_by(RetrievalIndex.ref_type)),
                "vector_count": s.query(RetrievalIndex).filter(RetrievalIndex.embedding.isnot(None), RetrievalIndex.embedding_model == embeddings.MODEL_ID).count(),
                "message_count": s.query(MonitorMessage).count(),
                "events": s.query(QqEvent).filter(QqEvent.status.in_(("review", "ready")), QqEvent.parts.isnot(None)).order_by(QqEvent.updated_at.desc()).limit(25).all() if tab == "diagnostics" else [],
                "classifications": s.query(MonitorMessage).order_by(MonitorMessage.id.desc()).limit(25).all() if tab == "diagnostics" else [],
                "subscriptions": s.query(QqGroup).order_by(QqGroup.id).all() if tab == "diagnostics" else []}
    data["center_cfg"] = {key: settings.get(key) for key in ("RETRIEVAL_ENABLED", "EMBEDDING_ENABLED", "EMBEDDING_BATCH_SIZE",
        "MEMORY_ENABLED", "MEMORY_TTL_DAYS", "MEMORY_RECENT_TURNS", "MEMORY_LAST_ERROR", "WIKI_SYNC_ENABLED", "WIKI_FETCH_INTERVAL_HOURS", "EMBEDDING_LAST_ERROR")}
    return data


@router.post("/job/{kind}")
async def job(kind: str, user: str = Depends(require_admin)):
    if kind not in ("unified_backfill", "knowledge_index"):
        raise HTTPException(400)
    require_worker()
    tid = submit_task(kind, {"after": 0})
    return HTMLResponse(f"<p>任务 #{tid} 已排队</p>")


@router.get("/lookup")
async def lookup(q: str = "", game: str = "", source: str = "", user: str = Depends(require_admin)):
    from ...retrieval import search, build_context
    types = (source,) if source in ("message", "wiki") else ("message", "wiki")
    hits = search(q, game=game, ref_types=types, limit=8)
    return HTMLResponse("<pre class='evidence'>" + escape(build_context(hits, 15000) or "没有匹配资料") + "</pre>")


@router.post("/classification/{mid}")
async def classification(mid: int, game: str = Form(""), user: str = Depends(require_admin)):
    from ...glossary import normalize_game
    from ...retrieval import index_message
    game = normalize_game(game)
    if game and game not in GAMES:
        raise HTTPException(400, "未知游戏")
    with session_scope() as s:
        row = s.get(MonitorMessage, mid)
        if row is None:
            raise HTTPException(404)
        row.game_detected = game or None
        row.game_scores = {"method": "manual", "classifier": "unified-v1", "reasons": [f"{user} 人工确认"], "game": game}
    index_message(mid)
    return HTMLResponse("<span class='ok'>分类已保存</span>")


@router.post("/document/{pid}")
async def document(pid: int, status: str = Form(...), user: str = Depends(require_admin)):
    if status not in ("active", "disabled", "pending"):
        raise HTTPException(400)
    with session_scope() as s:
        page = s.get(KnowledgePage, pid)
        if not page or not page.content.strip():
            raise HTTPException(400, "资料内容为空")
        page.attrs = {**(page.attrs or {}), "document_review": status}
    from ...kb.service import sync_page_index
    sync_page_index(pid)
    return redirect("/kb?tab=terms")


@router.post("/source/{sid}/trust")
async def source_trust(sid: int, trusted: str = Form(""), user: str = Depends(require_admin)):
    with session_scope() as s:
        source = s.get(KnowledgeSource, sid)
        if not source:
            raise HTTPException(404)
        source.trusted = trusted == "on"
        page_ids = [x.id for x in s.query(KnowledgePage.id).filter(KnowledgePage.source_id == sid)]
    from ...glossary import touch_version
    touch_version()
    from ...kb.service import sync_page_index
    for page_id in page_ids:
        sync_page_index(page_id)
    return HTMLResponse("<span class='ok'>已保存</span>")


@router.post("/memory/{cid}/save")
async def memory_save(cid: int, key: str = Form(""), value: str = Form(""), user: str = Depends(require_admin)):
    with session_scope() as s:
        conv = s.get(Conversation, cid)
        if not conv or conv.scope_type not in ("private_v2", "member_v2"):
            raise HTTPException(400)
        identity = conv.context_state or {}
    from ...memory import remember
    remember(identity.get("group", ""), identity.get("member", ""), value, key=key)
    return redirect("/kb?tab=memory")


@router.post("/memory/{cid}/forget")
async def memory_forget(cid: int, query: str = Form(""), user: str = Depends(require_admin)):
    from ...memory import forget
    with session_scope() as s:
        conv = s.get(Conversation, cid)
        if not conv:
            raise HTTPException(404)
        identity = conv.context_state or {}
    if identity.get("member"):
        forget("user", identity["member"], query or None, group=identity.get("group", ""))
    else:
        forget("conversation", str(cid), query or None)
    return redirect("/kb?tab=memory")


@router.post("/subscription/{gid}")
async def subscription(gid: int, request: Request, user: str = Depends(require_admin)):
    form = await request.form()
    chosen = list(dict.fromkeys(x for x in form.getlist("games") if x in GAMES))
    with session_scope() as s:
        group = s.get(QqGroup, gid)
        if not group:
            raise HTTPException(404)
        group.games = [] if form.get("all_games") else chosen
        if not group.games and not form.get("all_games"):
            raise HTTPException(400, "请选择游戏或全部游戏")
    return HTMLResponse("<span class='ok'>订阅已保存</span>")


@router.post("/event/{key}/resolve")
async def resolve_event(key: str, state: str = Form(...), user: str = Depends(require_admin)):
    if state not in ("done", "failed"):
        raise HTTPException(400)
    with session_scope() as s:
        row = s.get(QqEvent, key)
        if not row or row.status == "delivering":
            raise HTTPException(409)
        parts = {k: dict(v) for k, v in (row.parts or {}).items()}
        for part in parts.values():
            if part.get("status") in ("unknown", "sending"):
                part.update(status=state, error=f"{user} 核实结果", at=datetime.utcnow().isoformat())
        row.parts = parts
        row.status = "delivered" if parts and all(p["status"] == "done" for p in parts.values()) else "ready"
        s.query(QqDelivery).filter_by(dedup_key=key).update({"status": "retry", "next_retry_at": datetime.utcnow()})
    return redirect("/kb?tab=diagnostics")
