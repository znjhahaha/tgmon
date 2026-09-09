"""① 概览 —— worker/session 状态、今日量、花费、队列、磁盘、最近错误。"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import Text, func

from ...db import session_scope
from ...models import (
    AiUsage, Channel, MessageMedia, MonitorMessage, SystemEvent, Task,
)
from ...paths import MEDIA_DIR
from ...settings import get as _cfg_get
from ...util import local_day
from ..deps import (
    format_task_view, get_task, render, require_admin, require_admin_api,
)

router = APIRouter()


def _active_tasks() -> list[dict]:
    """正在排队/执行的任务（含计算后的百分比与友好展示格式）。"""
    with session_scope() as s:
        rows = (s.query(Task)
                .filter(Task.status.in_(("pending", "running")))
                .order_by(Task.id.desc()).limit(5).all())
        return [format_task_view({
            "id": t.id, "kind": t.kind, "status": t.status,
            "result": t.result, "error": t.error,
            "created_at": t.created_at, "finished_at": t.finished_at
        }) for t in rows]


def _latest_task() -> dict | None:
    """最近一条已完成/失败的任务（用于空闲时向管理员展示最近一次对齐/同步结果）。"""
    with session_scope() as s:
        t = (s.query(Task)
             .filter(Task.status.in_(("done", "failed")))
             .order_by(Task.id.desc()).first())
        if t is None:
            return None
        return format_task_view({
            "id": t.id, "kind": t.kind, "status": t.status,
            "result": t.result, "error": t.error,
            "created_at": t.created_at, "finished_at": t.finished_at
        })


def _stats() -> dict:
    now = datetime.utcnow()
    since_24h = now - timedelta(hours=24)
    day = local_day()
    with session_scope() as s:
        total = s.query(func.count(MonitorMessage.id)).scalar() or 0
        today = (s.query(func.count(MonitorMessage.id))
                 .filter(MonitorMessage.created_at >= since_24h).scalar() or 0)
        dups = (s.query(func.count(MonitorMessage.id))
                .filter(MonitorMessage.created_at >= since_24h,
                        MonitorMessage.duplicate_of.isnot(None)).scalar() or 0)
        failed = (s.query(func.count(MonitorMessage.id))
                  .filter(MonitorMessage.translate_status == "failed").scalar() or 0)
        # 空数组也要排掉。JSON 列里 [] 和 'null' 都不是 SQL NULL，
        # 光靠 isnot(None) 会把「翻译过但没漏译」的消息全算成待复查
        miss = (s.query(func.count(MonitorMessage.id))
                .filter(MonitorMessage.glossary_miss.isnot(None),
                        MonitorMessage.glossary_miss.cast(Text).notin_(
                            ("null", "[]"))).scalar() or 0)
        channels_on = (s.query(func.count(Channel.id))
                       .filter(Channel.enabled.is_(True)).scalar() or 0)
        channels_all = s.query(func.count(Channel.id)).scalar() or 0
        usage = s.query(func.sum(AiUsage.calls), func.sum(AiUsage.cost)) \
            .filter(AiUsage.day == day).first()
        thumb_bytes = s.query(func.sum(MessageMedia.thumb_bytes)).scalar() or 0
        events = (s.query(SystemEvent)
                  .filter(SystemEvent.level.in_(("error", "warning")))
                  .order_by(SystemEvent.created_at.desc()).limit(12).all())
        recent_events = [{"level": e.level, "source": e.source,
                          "message": e.message, "created_at": e.created_at}
                         for e in events]

    media_files = 0
    try:
        media_files = sum(1 for p in MEDIA_DIR.rglob("*") if p.is_file())
    except OSError:
        pass

    return {
        "total": total, "today": today, "dups": dups, "failed": failed,
        "glossary_miss": miss,
        "channels_on": channels_on, "channels_all": channels_all,
        "ai_calls": int(usage[0] or 0) if usage else 0,
        "ai_cost": float(usage[1] or 0.0) if usage else 0.0,
        "thumb_bytes": int(thumb_bytes), "media_files": media_files,
        "recent_events": recent_events,
        "active_tasks": _active_tasks(),
        "latest_task": _latest_task(),
        "channel_health": _channel_health(),
        "catchup_every": max(60, int(_cfg_get("CATCHUP_INTERVAL") or 300)),
        "last_catchup_at": _last_catchup_at(),
    }


def _channel_health() -> list[dict]:
    """启用的 telegram 频道拉取健康度（概览页卡片）。

    每频道：最新消息时间（距今多久 = 频道活跃度）+ 落后条数
    （TG 侧见到的最新 ID - 已入库最新 ID）。按最新消息倒序，最多 10 个。
    """
    from sqlalchemy import Integer, cast
    with session_scope() as s:
        maxes = dict(s.query(
            MonitorMessage.channel_id,
            func.max(cast(MonitorMessage.tg_message_id, Integer)))
            .group_by(MonitorMessage.channel_id).all())
        chans = (s.query(Channel)
                 .filter(Channel.enabled.is_(True),
                         Channel.source_type == "telegram")
                 .order_by(Channel.last_message_at.desc().nullslast())
                 .limit(10).all())
        out = []
        for c in chans:
            gap = None
            if c.last_tg_id is not None:
                mx = maxes.get(c.id)
                gap = c.last_tg_id - (mx or 0)
            out.append({"id": c.id, "title": c.title,
                        "last_message_at": c.last_message_at, "gap": gap})
        return out


def _last_catchup_at():
    """最近一轮对齐的时间（概览页「上次对齐」展示）。"""
    with session_scope() as s:
        t = (s.query(Task)
             .filter(Task.kind == "catchup_round")
             .order_by(Task.id.desc()).first())
        return (t.finished_at or t.created_at) if t is not None else None


@router.get("/")
async def overview(request: Request, user: str = Depends(require_admin)):
    return render(request, "overview.html",
                  {"nav_active": "overview", "s": _stats()})


@router.get("/fragment/overview-stats")
async def overview_fragment(request: Request,
                            user: str = Depends(require_admin_api)):
    """HTMX 每 10s 轮询这个片段，只有几百字节。"""
    from ..deps import worker_status
    return render(request, "fragments/overview_stats.html",
                  {"s": _stats(), "worker": worker_status()})


@router.get("/tasks/{tid}")
async def task_progress_card(tid: int, request: Request,
                             user: str = Depends(require_admin_api)):
    """通用任务进度轮询端点（HTMX 1.5s 轮询）。渲染统一可视化进度卡片。"""
    t = get_task(tid)
    if t is None:
        from fastapi.responses import HTMLResponse
        return HTMLResponse("<div class='flash err'>任务不存在</div>")
    v = format_task_view(t)
    return render(request, "fragments/task_progress.html", {"t": v})
