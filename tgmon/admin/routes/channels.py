"""③ 频道管理 —— 从账号同步已加入的频道，勾选要监控的，逐个配置。

不用手抄 ID、不用找私密频道链接：点同步，列表就出来了。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from ...db import session_scope
from ...models import Channel, MonitorMessage
from ...paths import EXTENSIONS_DIR
from ...themes import load_themes
from ...util import opt_int
from ...settings import get as _cfg_get
from ..deps import (
    format_task_view, get_task, redirect, render, require_admin,
    require_admin_api, require_worker, submit_task, templates,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/channels")


def _last_catchup_channels() -> dict[int, dict]:
    """最近一轮对齐的分频道明细（runner 写进 Task.result.channels）。

    {"cid": {...明细...}}。没有对齐记录（worker 从未跑过）返回空。
    """
    from ...models import Task
    with session_scope() as s:
        t = (s.query(Task)
             .filter(Task.kind == "catchup_round",
                     Task.status == "done",
                     Task.result.isnot(None))
             .order_by(Task.id.desc()).first())
        if t is None or not t.result:
            return {}
        chans = (t.result or {}).get("channels") or {}
        # JSON 键是字符串（JSONNull 列），频道侧按 int 匹配
        return {int(k): v for k, v in chans.items()
                if isinstance(v, dict)}


def _list(q: str = "", only_on: bool = False) -> list[dict]:
    with session_scope() as s:
        query = s.query(Channel)
        if q:
            like = f"%{q}%"
            query = query.filter(Channel.title.ilike(like)
                                 | Channel.username.ilike(like)
                                 | Channel.tg_id.ilike(like))
        if only_on:
            query = query.filter(Channel.enabled.is_(True))
        rows = query.order_by(Channel.enabled.desc(),
                             Channel.last_message_at.desc().nullslast(),
                             Channel.title.asc()).limit(500).all()
        # 对账状态列：库里每个频道的最大消息 ID（CAST 整数，tg_message_id 是 VARCHAR）
        from sqlalchemy import Integer, cast, func
        maxes = dict(s.query(
            MonitorMessage.channel_id,
            func.max(cast(MonitorMessage.tg_message_id, Integer)))
            .group_by(MonitorMessage.channel_id).all())
        last_round = _last_catchup_channels()
        out = []
        for r in rows:
            d = {
                "id": r.id, "tg_id": r.tg_id, "username": r.username,
                "source_type": r.source_type,
                "title": r.title, "kind": r.kind, "is_private": r.is_private,
                "enabled": r.enabled, "game": r.game or "",
                "games": r.games or [],
                "prompt_override": r.prompt_override or "",
                "force_push": r.force_push, "translate": r.translate,
                "bilingual_policy": r.bilingual_policy or "zh_first",
                "allow_photo": r.allow_photo, "allow_video": r.allow_video,
                "allow_document": r.allow_document,
                "max_media_mb": r.max_media_mb, "keep_video": r.keep_video,
                "video_max_mb": r.video_max_mb,
                "message_count": r.message_count,
                "last_message_at": r.last_message_at,
                "last_tg_id": r.last_tg_id,
                "last_catchup_at": r.last_catchup_at,
                "db_max_tg": maxes.get(r.id),
                "last_round": last_round.get(r.id),
            }
            # 落后条数：TG 侧见到的最新 - 已入库最新（None = 还没对账过）
            d["catchup_gap"] = (d["last_tg_id"] - d["db_max_tg"]
                                if d["last_tg_id"] is not None
                                and d["db_max_tg"] is not None else None)
            out.append(d)
        return out


def _games() -> list[str]:
    """已有频道的 game 值 + KB 里的全部 game（含冒号的规范值）。"""
    from ...models import GlossaryEntry
    with session_scope() as s:
        ch = (s.query(Channel.game).filter(Channel.game.isnot(None),
                                           Channel.game != "")
              .distinct().all())
        kb = s.query(GlossaryEntry.game).distinct().all()
        return sorted({r[0] for r in ch + kb if r[0]})


@router.get("")
async def page(request: Request, q: str = "", only_on: str = "0",
               user: str = Depends(require_admin)):
    return render(request, "channels.html", {
        "nav_active": "channels", "channels": _list(q, bool(opt_int(only_on))),
        "q": q, "only_on": bool(opt_int(only_on)), "games": _games(),
        "backfill_default": int(_cfg_get("BACKFILL_LIMIT") or 20),
        "align_days": int(_cfg_get("ALIGN_WINDOW_DAYS") or 3),
        "catchup_max_gap": int(_cfg_get("CATCHUP_MAX_GAP") or 50),
    })


@router.get("/fragment/list")
async def list_fragment(request: Request, q: str = "", only_on: str = "0",
                        user: str = Depends(require_admin_api)):
    return render(request, "fragments/channel_rows.html",
                  {"channels": _list(q, bool(opt_int(only_on))),
                   "games": _games(),
                   "backfill_default": int(_cfg_get("BACKFILL_LIMIT") or 20),
                   "align_days": int(_cfg_get("ALIGN_WINDOW_DAYS") or 3),
                   "catchup_max_gap": int(_cfg_get("CATCHUP_MAX_GAP") or 50)})


@router.get("/{cid}")
async def edit_page(cid: int, request: Request,
                    user: str = Depends(require_admin)):
    """独立编辑页。行内巨型表单已拆到这里 —— 列表页只留一目了然的行。"""
    with session_scope() as s:
        r = s.get(Channel, cid)
        if r is None:
            return redirect("/channels")
        c = {
            "id": r.id, "tg_id": r.tg_id, "username": r.username,
            "source_type": r.source_type, "title": r.title,
            "enabled": r.enabled, "game": r.game or "",
            "theme": r.theme or "gaming",
            "games": r.games or [],
            "prompt_override": r.prompt_override or "",
            "force_push": r.force_push, "translate": r.translate,
            "bilingual_policy": r.bilingual_policy or "zh_first",
            "allow_photo": r.allow_photo, "allow_video": r.allow_video,
            "allow_document": r.allow_document,
            "max_media_mb": r.max_media_mb, "keep_video": r.keep_video,
            "video_max_mb": r.video_max_mb,
            "message_count": r.message_count,
            "last_message_at": r.last_message_at,
        }
    return render(request, "channels_edit.html", {
        "nav_active": "channels", "c": c, "games": _games(),
        "themes": load_themes(EXTENSIONS_DIR / "themes"),
        "backfill_default": int(_cfg_get("BACKFILL_LIMIT") or 20),
        "align_days": int(_cfg_get("ALIGN_WINDOW_DAYS") or 3),
    })


@router.post("/sync")
async def sync(request: Request, user: str = Depends(require_admin)):
    """同步频道列表。改为异步轮询：频道多时同步要等一分多钟，
    手机上页面早关了 —— 立即返回进度占位，前端每 2 秒轮询。"""
    require_worker()
    tid = submit_task("sync_dialogs", {})
    return HTMLResponse(_task_fragment(tid, "正在从 Telegram 拉取频道列表…"))


def _task_fragment(tid: int, inner: str = "") -> str:
    """轮询占位片段。渲染统一动态进度条组件。"""
    t = get_task(tid) or {"id": tid, "kind": "", "status": "pending", "result": {}}
    v = format_task_view(t)
    if inner and not v["detail"]:
        v["detail"] = inner
    return templates.get_template("fragments/task_progress.html").render({"t": v})


@router.post("/{cid}/toggle")
async def toggle(cid: int, request: Request, user: str = Depends(require_admin)):
    with session_scope() as s:
        row = s.get(Channel, cid)
        if row is None:
            return HTMLResponse("<span class='err'>频道不存在</span>")
        row.enabled = not row.enabled
        state, title = row.enabled, row.title
    logger.info("频道 %s → %s", title, "启用" if state else "暂停")
    label = "监控中" if state else "已暂停"
    cls = "on" if state else "off"
    return HTMLResponse(
        f"<button class='pill {cls}' hx-post='/channels/{cid}/toggle' "
        f"hx-swap='outerHTML'>{label}</button>")


@router.post("/{cid}/save")
async def save(cid: int, request: Request,
               game: str = Form(""),
               theme: str = Form("gaming"),
               games: str = Form(""),
               prompt_override: str = Form(""),
               translate: str = Form(""),
               bilingual_policy: str = Form("zh_first"),
               force_push: str = Form(""),
               allow_photo: str = Form(""),
               allow_video: str = Form(""),
               allow_document: str = Form(""),
               keep_video: str = Form(""),
               video_max_mb: str = Form("0"),
               max_media_mb: str = Form("0"),
               user: str = Depends(require_admin)):
    def flag(v: str) -> bool:
        return v.strip().lower() in ("on", "true", "1", "yes")

    try:
        mb = max(0, int(max_media_mb or 0))
    except ValueError:
        mb = 0
    try:
        vmb = max(0, int(video_max_mb or 0))
    except ValueError:
        vmb = 0

    # games 字段：逗号分隔的游戏列表，存为 JSON 数组
    games_list = [g.strip() for g in games.split(",") if g.strip()] if games.strip() else None

    with session_scope() as s:
        row = s.get(Channel, cid)
        if row is None:
            return redirect("/channels")
        row.game = game.strip() or None
        row.theme = theme.strip() or "gaming"
        row.games = games_list
        row.prompt_override = prompt_override.strip() or None
        row.translate = flag(translate)
        if bilingual_policy in ("zh_first", "per_block", "always"):
            row.bilingual_policy = bilingual_policy
        row.force_push = flag(force_push)
        row.allow_photo = flag(allow_photo)
        row.allow_video = flag(allow_video)
        row.allow_document = flag(allow_document)
        row.keep_video = flag(keep_video)
        row.video_max_mb = vmb
        row.max_media_mb = mb
    logger.info("频道 %s 配置已保存", cid)
    # 编辑页是原生表单提交，跳回列表页看效果（htmx 片段场景已不存在）
    return redirect("/channels")


@router.post("/{cid}/align")
async def align(cid: int, request: Request, user: str = Depends(require_admin)):
    """单频道立即对齐：给 worker 下发 catchup 任务（只对齐窗口内缺口）。"""
    require_worker()
    tid = submit_task("catchup_now", {"channel_id": cid})
    return HTMLResponse(_task_fragment(tid, "对齐任务已下发，正在比对缺口…"))


@router.post("/{cid}/backfill")
async def backfill(cid: int, request: Request, limit: int = Form(None),
                   offset_id: int = Form(None), min_date: str = Form(""),
                   user: str = Depends(require_admin)):
    """勾选频道后补历史，不用等新消息就能验证管线。

    任务可能跑几分钟（一次 1000 条 × 逐条 AI 调用），同步等会超时 ——
    立即返回进度占位片段，前端 htmx 每 2 秒轮询 /channels/task/{tid}。
    """
    from ...settings import get
    if limit is None:
        limit = int(get("BACKFILL_LIMIT") or 20)
    limit = max(1, min(limit, 1000))
    require_worker()
    payload: dict = {"channel_id": cid, "limit": limit}
    if offset_id:
        payload["offset_id"] = offset_id
    day = (min_date or "").strip()[:10]
    if day:
        payload["min_date"] = day
    tid = submit_task("backfill", payload)
    return HTMLResponse(_task_fragment(tid, "任务已下发，等 worker 领取…"))


@router.get("/task/{tid}")
async def task_progress(tid: int, request: Request,
                        user: str = Depends(require_admin_api)):
    """sync / backfill / align 的进度轮询端点。渲染统一可视化进度卡片。"""
    t = get_task(tid)
    if t is None:
        return HTMLResponse("<div class='flash err'>任务不存在</div>")
    v = format_task_view(t)
    return render(request, "fragments/task_progress.html", {"t": v})


@router.post("/{cid}/delete")
async def delete(cid: int, request: Request, user: str = Depends(require_admin)):
    with session_scope() as s:
        row = s.get(Channel, cid)
        if row is not None:
            s.delete(row)
    return redirect("/channels")
