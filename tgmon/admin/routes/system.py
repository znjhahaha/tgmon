"""⑩ 系统 —— 媒体保留策略、AI 预算、日志级别、备份导出、重启。"""
from __future__ import annotations

import io
import json
import logging
import shutil
import zipfile
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import func

from ... import settings
from ...db import session_scope
from ...models import (
    AiUsage, AppSetting, Channel, GlossaryAlias, GlossaryEntry, MessageMedia,
    PromptTemplate, RssFeed, SystemEvent,
)
from ...paths import DB_PATH, MEDIA_DIR, VIDEO_DIR
from ...util import local_day
from ..deps import (
    render, require_admin, require_worker, submit_task, touch_restart,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/system")


def _disk() -> dict:
    total, used, free = shutil.disk_usage("/app")
    media_bytes, media_files = 0, 0
    try:
        for p in MEDIA_DIR.rglob("*"):
            if not p.is_file():
                continue
            if VIDEO_DIR in p.parents:
                continue          # 视频单独统计
            media_files += 1
            media_bytes += p.stat().st_size
    except OSError:
        pass
    video_bytes, video_files = 0, 0
    try:
        for p in VIDEO_DIR.rglob("*"):
            if p.is_file():
                video_files += 1
                video_bytes += p.stat().st_size
    except OSError:
        pass
    with session_scope() as s:
        video_rows = (s.query(MessageMedia)
                      .filter(MessageMedia.video_path.isnot(None)).count())
    db_bytes = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    return {"total": total, "used": used, "free": free,
            "media_bytes": media_bytes, "media_files": media_files,
            "video_bytes": video_bytes, "video_files": video_files,
            "video_rows": video_rows,
            "db_bytes": db_bytes}


def _usage_rows(days: int = 14) -> list[dict]:
    with session_scope() as s:
        rows = (s.query(AiUsage.day,
                        func.sum(AiUsage.calls), func.sum(AiUsage.tokens_in),
                        func.sum(AiUsage.tokens_out), func.sum(AiUsage.cost))
                .group_by(AiUsage.day)
                .order_by(AiUsage.day.desc()).limit(days).all())
        return [{"day": r[0], "calls": int(r[1] or 0),
                 "tokens_in": int(r[2] or 0), "tokens_out": int(r[3] or 0),
                 "cost": float(r[4] or 0)} for r in rows]


@router.get("")
async def page(request: Request, user: str = Depends(require_admin)):
    with session_scope() as s:
        events = [{"level": e.level, "source": e.source, "message": e.message,
                   "created_at": e.created_at}
                  for e in s.query(SystemEvent)
                  .order_by(SystemEvent.created_at.desc()).limit(60).all()]
    return render(request, "system.html", {
        "nav_active": "system", "disk": _disk(), "usage": _usage_rows(),
        "today": local_day(), "events": events,
        "cfg": {
            "message_ttl_days": settings.get("MESSAGE_TTL_DAYS"),
            "media_ttl_days": settings.get("MEDIA_TTL_DAYS"),
            "align_window_days": settings.get("ALIGN_WINDOW_DAYS"),
            "media_max_mb": settings.get("MEDIA_MAX_MB"),
            "thumb_width": settings.get("THUMB_WIDTH"),
            "photo_width": settings.get("PHOTO_WIDTH"),
            "webp_quality": settings.get("WEBP_QUALITY"),
            "ffmpeg_fallback": settings.get("FFMPEG_FALLBACK"),
            "video_enabled": settings.get("VIDEO_ARCHIVE_ENABLED"),
            "video_max_mb": settings.get("VIDEO_MAX_MB"),
            "video_max_seconds": settings.get("VIDEO_MAX_SECONDS"),
            "video_height": settings.get("VIDEO_HEIGHT"),
            "video_crf": settings.get("VIDEO_CRF"),
            "video_ttl_days": settings.get("VIDEO_TTL_DAYS"),
            "video_max_total_mb": settings.get("VIDEO_MAX_TOTAL_MB"),
            "ai_daily_call_limit": settings.get("AI_DAILY_CALL_LIMIT"),
            "ai_daily_cost_limit": settings.get("AI_DAILY_COST_LIMIT"),
            "ai_over_limit_action": settings.get("AI_OVER_LIMIT_ACTION"),
            "translate_enabled": settings.get("TRANSLATE_ENABLED"),
            "translate_cache": settings.get("TRANSLATE_CACHE_ENABLED"),
            "glossary_check": settings.get("GLOSSARY_CHECK_ENABLED"),
            "append_original": settings.get("APPEND_ORIGINAL"),
            "changelog_enabled": settings.get("CHANGELOG_ENABLED"),
            "changelog_first_run_keep": settings.get("CHANGELOG_FIRST_RUN_KEEP"),
            "catchup_enabled": settings.get("CATCHUP_ENABLED"),
            "catchup_interval": settings.get("CATCHUP_INTERVAL"),
            "catchup_max_gap": settings.get("CATCHUP_MAX_GAP"),
            "retrieval_enabled": settings.get("RETRIEVAL_ENABLED"),
            "embedding_enabled": settings.get("EMBEDDING_ENABLED"),
            "embedding_batch_size": settings.get("EMBEDDING_BATCH_SIZE"),
            "memory_enabled": settings.get("MEMORY_ENABLED"),
            "memory_ttl_days": settings.get("MEMORY_TTL_DAYS"),
            "memory_recent_turns": settings.get("MEMORY_RECENT_TURNS"),
            "memory_summary_trigger": settings.get("MEMORY_SUMMARY_TRIGGER"),
            "wiki_sync_enabled": settings.get("WIKI_SYNC_ENABLED"),
            "wiki_fetch_interval_hours": settings.get("WIKI_FETCH_INTERVAL_HOURS"),
            "log_level": settings.get("LOG_LEVEL"),
        },
    })


@router.post("/retrieval")
async def save_retrieval(request: Request,
                         retrieval_enabled: str = Form(""),
                         embedding_enabled: str = Form(""),
                         embedding_batch_size: int = Form(32),
                         memory_enabled: str = Form(""),
                         memory_ttl_days: int = Form(30),
                         memory_recent_turns: int = Form(8),
                         memory_summary_trigger: int = Form(12),
                         wiki_sync_enabled: str = Form(""),
                         wiki_fetch_interval_hours: int = Form(24),
                         user: str = Depends(require_admin)):
    on = lambda x: str(x).strip().lower() in ("on", "1", "true")
    settings.set_many({
        "RETRIEVAL_ENABLED": on(retrieval_enabled),
        "EMBEDDING_ENABLED": on(embedding_enabled),
        "EMBEDDING_BATCH_SIZE": max(1, min(embedding_batch_size, 256)),
        "MEMORY_ENABLED": on(memory_enabled),
        "MEMORY_TTL_DAYS": max(1, min(memory_ttl_days, 3650)),
        "MEMORY_RECENT_TURNS": max(2, min(memory_recent_turns, 30)),
        "MEMORY_SUMMARY_TRIGGER": max(4, min(memory_summary_trigger, 100)),
        "WIKI_SYNC_ENABLED": on(wiki_sync_enabled),
        "WIKI_FETCH_INTERVAL_HOURS": max(1, min(wiki_fetch_interval_hours, 720)),
    })
    return HTMLResponse("<div class='flash ok'>检索、记忆与 Wiki 设置已保存</div>")


@router.post("/catchup")
async def save_catchup(request: Request,
                       catchup_enabled: str = Form(""),
                       catchup_interval: int = Form(300),
                       catchup_max_gap: int = Form(50),
                       align_window_days: int = Form(3),
                       user: str = Depends(require_admin)):
    settings.set_many({
        "CATCHUP_ENABLED": catchup_enabled.strip().lower() in ("on", "1"),
        "CATCHUP_INTERVAL": max(60, catchup_interval),
        "CATCHUP_MAX_GAP": max(1, catchup_max_gap),
        "ALIGN_WINDOW_DAYS": max(1, align_window_days),
    })
    return HTMLResponse("<div class='flash ok'>已保存，下一轮对齐生效</div>")


@router.post("/catchup-now")
async def catchup_now(request: Request, user: str = Depends(require_admin)):
    """立即触发一轮对账（不用等间隔）。走任务队列转给 worker。"""
    require_worker()
    tid = submit_task("catchup_now", {})
    return HTMLResponse(_catchup_fragment(tid))


def _catchup_fragment(tid: int) -> str:
    return (f"<div class='flash' hx-get='/channels/task/{tid}' "
            f"hx-trigger='every 2s' hx-swap='outerHTML'>对账任务已下发…</div>")


@router.post("/video")
async def save_video(request: Request,
                     video_enabled: str = Form(""),
                     video_max_mb: int = Form(50),
                     video_max_seconds: int = Form(180),
                     video_height: int = Form(720),
                     video_crf: int = Form(28),
                     video_ttl_days: int = Form(7),
                     video_max_total_mb: int = Form(3000),
                     user: str = Depends(require_admin)):
    settings.set_many({
        "VIDEO_ARCHIVE_ENABLED": video_enabled.strip().lower() in ("on", "1"),
        "VIDEO_MAX_MB": max(0, video_max_mb),
        "VIDEO_MAX_SECONDS": max(0, video_max_seconds),
        "VIDEO_HEIGHT": max(240, min(video_height, 2160)),
        "VIDEO_CRF": max(18, min(video_crf, 40)),
        "VIDEO_TTL_DAYS": max(0, video_ttl_days),
        "VIDEO_MAX_TOTAL_MB": max(0, video_max_total_mb),
    })
    return HTMLResponse("<div class='flash ok'>已保存，立即生效。"
                        "开了总开关还要在频道配置里勾「归档视频」才会真的下载</div>")


@router.post("/media")
async def save_media(request: Request,
                     message_ttl_days: int = Form(5),
                     media_ttl_days: int = Form(30),
                     media_max_mb: int = Form(2048),
                     thumb_width: int = Form(640),
                     photo_width: int = Form(1280),
                     webp_quality: int = Form(80),
                     ffmpeg_fallback: str = Form(""),
                     user: str = Depends(require_admin)):
    settings.set_many({
        "MESSAGE_TTL_DAYS": max(0, message_ttl_days),
        "MEDIA_TTL_DAYS": max(0, media_ttl_days),
        "MEDIA_MAX_MB": max(0, media_max_mb),
        "THUMB_WIDTH": max(120, min(thumb_width, 2000)),
        "PHOTO_WIDTH": max(240, min(photo_width, 3000)),
        "WEBP_QUALITY": max(40, min(webp_quality, 100)),
        "FFMPEG_FALLBACK": ffmpeg_fallback.strip().lower() in ("on", "1"),
    })
    return HTMLResponse("<div class='flash ok'>已保存，立即生效</div>")


@router.post("/budget")
async def save_budget(request: Request,
                      ai_daily_call_limit: int = Form(2000),
                      ai_daily_cost_limit: float = Form(0.0),
                      ai_over_limit_action: str = Form("store_only"),
                      translate_enabled: str = Form(""),
                      translate_cache: str = Form(""),
                      glossary_check: str = Form(""),
                      append_original: str = Form(""),
                      changelog_enabled: str = Form(""),
                      changelog_first_run_keep: int = Form(1),
                      user: str = Depends(require_admin)):
    settings.set_many({
        "AI_DAILY_CALL_LIMIT": max(0, ai_daily_call_limit),
        "AI_DAILY_COST_LIMIT": max(0.0, ai_daily_cost_limit),
        "AI_OVER_LIMIT_ACTION": ai_over_limit_action
        if ai_over_limit_action in ("store_only", "stop") else "store_only",
        "TRANSLATE_ENABLED": translate_enabled.strip().lower() in ("on", "1"),
        "TRANSLATE_CACHE_ENABLED": translate_cache.strip().lower() in ("on", "1"),
        "GLOSSARY_CHECK_ENABLED": glossary_check.strip().lower() in ("on", "1"),
        "APPEND_ORIGINAL": append_original.strip().lower() in ("on", "1"),
        "CHANGELOG_ENABLED": changelog_enabled.strip().lower() in ("on", "1"),
        "CHANGELOG_FIRST_RUN_KEEP": max(0, changelog_first_run_keep),
    })
    return HTMLResponse("<div class='flash ok'>已保存，立即生效</div>")


@router.post("/log-level")
async def save_log_level(request: Request, log_level: str = Form("INFO"),
                         user: str = Depends(require_admin)):
    lvl = log_level.strip().upper()
    if lvl not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        return HTMLResponse("<div class='flash err'>级别不合法</div>")
    settings.set_many({"LOG_LEVEL": lvl})
    return HTMLResponse(
        "<div class='flash ok'>已保存。日志级别在进程启动时读取，"
        "点下面的「重启 worker」或重启容器后生效</div>")


@router.post("/cleanup-media")
async def cleanup_media(request: Request, user: str = Depends(require_admin)):
    require_worker()
    submit_task("media_cleanup", {})
    return HTMLResponse(
        "<div class='flash ok'>清理任务已下发，稍后刷新看磁盘占用</div>")


@router.post("/restart-worker")
async def restart_worker(request: Request, user: str = Depends(require_admin)):
    touch_restart()
    return HTMLResponse(
        "<div class='flash ok'>已请求重启。worker 会在几秒内退出并被 compose "
        "自动拉起</div>")


@router.get("/backup.zip")
async def backup(request: Request, user: str = Depends(require_admin)):
    """配置 + 术语表 + prompt + feed 定义打包下载。不含 session 与密钥明文。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        with session_scope() as s:
            cfg = [{"key": r.key, "value": r.value if not r.is_secret else None,
                    "type": r.type, "group": r.group, "is_secret": r.is_secret}
                   for r in s.query(AppSetting).all()]
            # 知识库：实体 + 别名。手工过审的状态是人工劳动的成果，
            # 备份必须带上，否则换机器要重新审一遍两百多条
            gl = []
            for e in s.query(GlossaryEntry).all():
                gl.append({
                    "game": e.game, "category": e.category,
                    "canonical_zh": e.canonical_zh, "note": e.note,
                    "status": e.status, "origin": e.origin,
                    "origin_ref": e.origin_ref, "attrs": e.attrs,
                    "enabled": e.enabled,
                    "aliases": [
                        {"surface": a.surface, "lang": a.lang,
                         "alias_kind": a.alias_kind, "match_mode": a.match_mode,
                         "case_sensitive": a.case_sensitive,
                         "enabled": a.enabled}
                        for a in s.query(GlossaryAlias)
                        .filter(GlossaryAlias.entry_id == e.id).all()
                    ],
                })
            pr = [{"scope": r.scope, "scope_key": r.scope_key, "body": r.body}
                  for r in s.query(PromptTemplate).all()]
            ch = [{"tg_id": r.tg_id, "username": r.username, "title": r.title,
                   "enabled": r.enabled, "game": r.game,
                   "prompt_override": r.prompt_override,
                   "force_push": r.force_push, "translate": r.translate}
                  for r in s.query(Channel).all()]
            fd = [{"slug": r.slug, "title": r.title,
                   "description": r.description, "channel_ids": r.channel_ids,
                   "games": r.games, "keywords": r.keywords,
                   "max_items": r.max_items, "public": r.public}
                  for r in s.query(RssFeed).all()]
        z.writestr("app_setting.json", json.dumps(cfg, ensure_ascii=False, indent=2))
        z.writestr("glossary.json", json.dumps(gl, ensure_ascii=False, indent=2))
        z.writestr("prompts.json", json.dumps(pr, ensure_ascii=False, indent=2))
        z.writestr("channels.json", json.dumps(ch, ensure_ascii=False, indent=2))
        z.writestr("rss_feeds.json", json.dumps(fd, ensure_ascii=False, indent=2))
        z.writestr("README.txt",
                   "tgmon 配置备份\n"
                   "不含：session 文件、加密后的密钥明文（api_hash / api_key / "
                   "webhook secret）。\n"
                   "换机器时另外搬 secret.key 与 sessions/ 目录。\n")
    buf.seek(0)
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M")
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename="tgmon-backup-{stamp}.zip"'})


@router.post("/purge-cache")
async def purge_cache(request: Request, user: str = Depends(require_admin)):
    from ...models import TranslationCache
    with session_scope() as s:
        n = s.query(TranslationCache).delete(synchronize_session=False)
    return HTMLResponse(f"<div class='flash ok'>已清空 {n} 条翻译缓存</div>")
