"""⑥ Prompt 模板 —— 三层可视化编辑 + 试译预览（拿真实历史消息试跑，不发送）。"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from ...db import session_scope
from ...models import Channel, MonitorMessage, PromptTemplate
from ...prompts import DEFAULT_GLOBAL_PROMPT
from ..deps import (
    get_task, redirect, render, require_admin, require_worker, submit_task,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/prompts")


def _all() -> dict:
    with session_scope() as s:
        rows = s.query(PromptTemplate).order_by(PromptTemplate.scope.asc(),
                                                PromptTemplate.scope_key.asc()).all()
        glob = next((r for r in rows if r.scope == "global"), None)
        games = [{"id": r.id, "scope_key": r.scope_key, "body": r.body,
                  "updated_at": r.updated_at}
                 for r in rows if r.scope == "game"]
        chans = (s.query(Channel)
                 .filter(Channel.prompt_override.isnot(None),
                         Channel.prompt_override != "")
                 .order_by(Channel.title.asc()).all())
        overrides = [{"id": c.id, "title": c.title,
                      "body": c.prompt_override} for c in chans]
        known_games = sorted({c.game for c in s.query(Channel)
                              .filter(Channel.game.isnot(None),
                                      Channel.game != "").all()})
    return {
        "global_body": glob.body if glob else DEFAULT_GLOBAL_PROMPT,
        "global_updated": glob.updated_at if glob else None,
        "games": games, "overrides": overrides, "known_games": known_games,
    }


def _samples(limit: int = 25) -> list[dict]:
    with session_scope() as s:
        rows = (s.query(MonitorMessage)
                .filter(MonitorMessage.text_raw != "")
                .order_by(MonitorMessage.published_at.desc())
                .limit(limit).all())
        return [{"id": r.id, "text": (r.text_raw or "")[:120],
                 "channel_id": r.channel_id} for r in rows]


@router.get("")
async def page(request: Request, user: str = Depends(require_admin)):
    return render(request, "prompts.html", {
        "nav_active": "prompts", **_all(), "samples": _samples(),
        "default_body": DEFAULT_GLOBAL_PROMPT,
    })


@router.post("/global")
async def save_global(request: Request, body: str = Form(...),
                      user: str = Depends(require_admin)):
    if not body.strip():
        return HTMLResponse("<div class='flash err'>不能存空 prompt</div>")
    with session_scope() as s:
        row = s.query(PromptTemplate).filter(PromptTemplate.scope == "global").first()
        if row is None:
            s.add(PromptTemplate(scope="global", scope_key="", body=body.strip()))
        else:
            row.body = body.strip()
    return HTMLResponse("<div class='flash ok'>已保存，下一条消息就生效</div>")


@router.post("/game")
async def save_game(request: Request, scope_key: str = Form(...),
                    body: str = Form(...), user: str = Depends(require_admin)):
    key = scope_key.strip()
    if not key:
        return HTMLResponse("<div class='flash err'>游戏分组名不能为空</div>")
    with session_scope() as s:
        row = (s.query(PromptTemplate)
               .filter(PromptTemplate.scope == "game",
                       PromptTemplate.scope_key == key).first())
        if row is None:
            s.add(PromptTemplate(scope="game", scope_key=key, body=body.strip()))
        else:
            row.body = body.strip()
    return redirect("/prompts")


@router.post("/game/{pid}/delete")
async def delete_game(pid: int, request: Request,
                      user: str = Depends(require_admin)):
    with session_scope() as s:
        row = s.get(PromptTemplate, pid)
        if row is not None and row.scope == "game":
            s.delete(row)
    return redirect("/prompts")


@router.post("/trial")
async def trial(request: Request, message_id: int = Form(...),
                body: str = Form(...), game: str = Form(""),
                user: str = Depends(require_admin)):
    """试译：用当前编辑框里的 prompt 跑一条真实历史消息，结果只显示不入库。"""
    require_worker()
    tid = submit_task("trial_translate", {
        "message_id": message_id, "prompt": body, "game": game.strip() or None,
    })
    waited = 0.0
    while waited < 150.0:
        t = get_task(tid)
        if t and t["status"] == "failed":
            return HTMLResponse(f"<div class='flash err'>{t['error']}</div>")
        if t and t["status"] == "done":
            r = t["result"] or {}
            if not r.get("ok"):
                return HTMLResponse(
                    f"<div class='flash err'>试译失败：{r.get('error')}</div>")
            out = (r.get("text_zh") or "").replace("<", "&lt;")
            hits = "、".join(r.get("glossary_hits") or []) or "无"
            miss = "、".join(r.get("glossary_miss") or []) or "无"
            return HTMLResponse(
                "<div class='flash ok'>试译完成（未入库）</div>"
                f"<div class='trial'><pre>{out}</pre>"
                f"<div class='hint'>术语命中：{hits}　漏译：{miss}　"
                f"后端：{r.get('provider_name') or '—'}</div></div>")
        await asyncio.sleep(1.2)
        waited += 1.2
    return HTMLResponse("<div class='flash err'>试译超时</div>")
