"""定时维护：缩略图 TTL/LRU 清理、归档视频独立清理、孤儿文件回收、webhook 重试。

磁盘满会让 SQLite 写失败（整个系统停摆），所以清理不是可选项。

缩略图与归档视频是两套独立策略：视频又大又可再生（TG 深链永远能看），
所以它的 TTL（VIDEO_TTL_DAYS）和总量上限（VIDEO_MAX_TOTAL_MB）都比
缩略图激进。超限时先删视频再动缩略图 —— 同一条记录里视频占 95% 的字节。

磁盘水位（DISK_WATERMARK_HIGH/LOW_GB）是兜底闸：媒体目录总量超 10G
时按 LRU 删视频直到低于 3G —— 用户显式要求的容量策略（2026-09 QQ
升级：默认开视频归档后必须有这个保险丝）。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from .. import settings
from ..db import engine, session_scope
from ..models import MessageMedia, MonitorMessage, SystemEvent, Task
from ..paths import MEDIA_DIR, VIDEO_DIR
from ..sharing import protected_message_ids

logger = logging.getLogger(__name__)

CLEANUP_EVERY = 3600.0     # 每小时跑一次
WEBHOOK_EVERY = 20.0
# 内鬼站数据版本更新的间隔是小时级，20 分钟够快了。再密就是白给对方加负载
CHANGELOG_EVERY = 1200.0
WIKI_EVERY = 1800.0


def _unlink(base, rel: str | None) -> int:
    """删 base 下的相对路径文件。返回释放的字节数。"""
    if not rel:
        return 0
    p = base / rel
    try:
        if p.exists():
            size = p.stat().st_size
            p.unlink()
            return size
    except OSError as e:
        logger.debug("删除文件失败 %s: %s", p, e)
    return 0


def _dir_usage(d, *, skip_private: bool = False) -> int:
    """目录总字节数。skip_private：MEDIA_DIR 统计时排除视频子目录。"""
    total = 0
    for p in d.rglob("*"):
        if not p.is_file():
            continue
        if skip_private and VIDEO_DIR in p.parents:
            continue
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


def _clear_video(r) -> int:
    """清掉一条记录的归档视频（不动缩略图）。返回释放的字节数。"""
    got = _unlink(VIDEO_DIR, r.video_path)
    if r.original_path and r.original_path != r.video_path:
        got += _unlink(VIDEO_DIR, r.original_path)
    r.original_path, r.original_bytes = None, 0
    r.video_path = None
    r.video_bytes = 0
    r.video_status = "expired"
    if r.status != "superseded":
        r.status = "expired"
    return got


def _clear_thumb(r) -> int:
    """清掉一条记录的缩略图（不动归档视频）。返回释放的字节数。"""
    got = _unlink(MEDIA_DIR, r.thumb_path)
    r.thumb_path = None
    r.thumb_bytes = 0
    if r.kind == "photo":
        got += _unlink(VIDEO_DIR, r.original_path)
        r.original_path, r.original_bytes = None, 0
        if r.status != "superseded":
            r.status = "expired"
    return got


def _unshared_media(s):
    return s.query(MessageMedia).filter(~MessageMedia.message_id.in_(protected_message_ids(s)))


def cleanup_media() -> dict:
    """视频按自己的 TTL/总量清，缩略图按自己的清，最后回收孤儿文件。"""
    removed, freed = 0, 0

    # ---- 1. 视频 TTL ----
    v_ttl = int(settings.get("VIDEO_TTL_DAYS") or 0)
    if v_ttl > 0:
        cutoff = datetime.utcnow() - timedelta(days=v_ttl)
        with session_scope() as s:
            rows = (_unshared_media(s)
                    .filter(MessageMedia.created_at < cutoff,
                            MessageMedia.video_path.isnot(None))
                    .all())
            for r in rows:
                freed += _clear_video(r)
                removed += 1

    # ---- 2. 视频总量（LRU，按 last_access_at；鉴权路由访问时刷新） ----
    v_max = int(settings.get("VIDEO_MAX_TOTAL_MB") or 0)
    if v_max > 0:
        total = _dir_usage(VIDEO_DIR)
        if total > v_max * 1024 * 1024:
            with session_scope() as s:
                rows = (_unshared_media(s)
                        .filter(MessageMedia.video_path.isnot(None))
                        .order_by(MessageMedia.last_access_at.asc())
                        .limit(5000).all())
                for r in rows:
                    if total <= v_max * 1024 * 1024:
                        break
                    got = _clear_video(r)
                    total -= got
                    freed += got
                    removed += 1

    # ---- 3. 缩略图 TTL（只动 thumb，视频归第 1/2 步管） ----
    ttl_days = int(settings.get("MEDIA_TTL_DAYS") or 30)
    if ttl_days > 0:
        cutoff = datetime.utcnow() - timedelta(days=ttl_days)
        with session_scope() as s:
            rows = (_unshared_media(s)
                    .filter(MessageMedia.created_at < cutoff,
                            MessageMedia.thumb_path.isnot(None))
                    .all())
            for r in rows:
                freed += _clear_thumb(r)
                removed += 1

    # ---- 4. 缩略图总量（只统计非 private 部分；视频有自己的上限） ----
    max_mb = int(settings.get("MEDIA_MAX_MB") or 0)
    if max_mb > 0:
        total = _dir_usage(MEDIA_DIR, skip_private=True)
        if total > max_mb * 1024 * 1024:
            with session_scope() as s:
                rows = (_unshared_media(s)
                        .filter(MessageMedia.thumb_path.isnot(None))
                        .order_by(MessageMedia.last_access_at.asc())
                        .limit(5000).all())
                for r in rows:
                    if total <= max_mb * 1024 * 1024:
                        break
                    got = _clear_thumb(r)
                    total -= got
                    freed += got
                    removed += 1

    orphans = _cleanup_orphans()
    if removed or orphans:
        logger.info("媒体清理：清了 %d 项、%d 个孤儿文件，释放 %.1f MB",
                    removed, orphans, freed / 1048576)
    return {"removed": removed, "orphans": orphans, "freed_mb": round(freed / 1048576, 1)}


def cleanup_messages(batch: int = 500) -> dict:
    """消息 TTL：超期消息连同图片/视频/缩略图全删。

    与缩略图/视频各自独立的 TTL 不同 —— 那两个只清文件留消息行（还有
    译文可看），这个是整条消息消失。磁盘小、爆料时效性短，只保最近几天。

    先收集要删消息的媒体文件路径并删文件，再分批删行（每批独立事务，
    防一次删几千条把 SQLite 长事务卡死）。
    """
    ttl = int(settings.get("MESSAGE_TTL_DAYS") or 0)
    if ttl <= 0:
        return {"removed": 0}
    cutoff = datetime.utcnow() - timedelta(days=ttl)
    removed, freed = 0, 0
    while True:
        with session_scope() as s:
            rows = (s.query(MonitorMessage)
                    .filter(MonitorMessage.published_at < cutoff,
                            ~MonitorMessage.id.in_(protected_message_ids(s)))
                    .order_by(MonitorMessage.published_at.asc())
                    .limit(batch).all())
            if not rows:
                break
            ids = [r.id for r in rows]
            medias = (_unshared_media(s)
                      .filter(MessageMedia.message_id.in_(ids)).all())
            for m in medias:
                freed += _unlink(MEDIA_DIR, m.thumb_path)
                freed += _unlink(VIDEO_DIR, m.video_path)
            s.query(MonitorMessage)\
                .filter(MonitorMessage.id.in_(ids))\
                .delete(synchronize_session=False)
            removed += len(ids)
        # Keep the lexical index in sync with message TTL cleanup.  FTS5 is
        # optional, therefore both deletes are best effort.
        try:
            from sqlalchemy import text as sql_text
            with engine.begin() as conn:
                marks = ",".join(str(int(i)) for i in ids)
                conn.execute(sql_text(f"DELETE FROM retrieval_index WHERE ref_type='message' AND ref_id IN ({marks})"))
                conn.execute(sql_text(f"DELETE FROM retrieval_fts WHERE ref_type='message' AND ref_id IN ({','.join(repr(str(int(i))) for i in ids)})"))
        except Exception:
            pass
    if removed:
        from ..util import log_event
        log_event("info", "maintenance",
                  f"消息 TTL：删除 {removed} 条超期消息（>{ttl} 天），"
                  f"释放媒体 {freed / 1048576:.1f} MB")
        logger.info("消息 TTL 清理：删 %d 条，释放 %.1f MB", removed, freed / 1048576)
    return {"removed": removed, "freed_mb": round(freed / 1048576, 1)}


def _cleanup_orphans() -> int:
    """DB 里没有记录的文件。断电或异常中断会留下这些。

    VIDEO_DIR 默认在 MEDIA_DIR/private 下，扫 MEDIA_DIR 时必须跳过它：
    视频路径相对 VIDEO_DIR（cid/msgid.mp4），拿它跟相对 MEDIA_DIR 的
    集合（private/cid/msgid.mp4）比对永远对不上，会把所有视频误删。
    """
    with session_scope() as s:
        thumbs = {r[0] for r in s.query(MessageMedia.thumb_path)
                  .filter(MessageMedia.thumb_path.isnot(None)).all()}
        videos = {r[0] for r in s.query(MessageMedia.video_path)
                  .filter(MessageMedia.video_path.isnot(None)).all()}
        videos.update(r[0] for r in s.query(MessageMedia.original_path)
                      .filter(MessageMedia.original_path.isnot(None)))
    n = 0
    now_ts = datetime.utcnow().timestamp()
    for base, known in ((MEDIA_DIR, thumbs), (VIDEO_DIR, videos)):
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            if ".partial" in p.name:
                if now_ts - p.stat().st_mtime < 7 * 86400:
                    continue
            if base == MEDIA_DIR and VIDEO_DIR in p.parents:
                continue          # 视频目录单独扫
            rel = p.relative_to(base).as_posix()
            if rel.startswith("qq-cards/"):
                from ..models import ShareToken
                try:
                    sid = int(p.relative_to(base).parts[1].split("-")[0])
                except (ValueError, IndexError):
                    sid = 0
                with session_scope() as s:
                    share = s.get(ShareToken, sid) if sid else None
                    if share and not share.revoked and share.expires_at > datetime.utcnow():
                        continue
            if rel in known:
                continue
            # 刚写入还没提交的文件别删
            try:
                if now_ts - p.stat().st_mtime < 300:
                    continue
                p.unlink()
                n += 1
            except OSError:
                pass
    return n


def _trim_events(keep: int = 500) -> None:
    with session_scope() as s:
        total = s.query(SystemEvent).count()
        if total <= keep:
            return
        ids = [r[0] for r in s.query(SystemEvent.id)
               .order_by(SystemEvent.created_at.desc()).offset(keep).all()]
        if ids:
            s.query(SystemEvent).filter(SystemEvent.id.in_(ids)).delete(
                synchronize_session=False)


def _trim_tasks(keep_days: int = 3) -> None:
    cutoff = datetime.utcnow() - timedelta(days=keep_days)
    with session_scope() as s:
        s.query(Task).filter(Task.status.in_(("done", "failed")),
                             Task.created_at < cutoff).delete(
            synchronize_session=False)


def cleanup_disk_watermark() -> dict:
    """磁盘水位兜底：媒体总量超高位 → LRU 删视频直到低于低位。

    与 cleanup_media 的 VIDEO_MAX_TOTAL_MB（视频目录自身配额）互补：
    那是常规策略，这是用户显式要求的总容量保险丝 —— 默认开视频归档
    后（2026-09 QQ 升级），视频 + 图片 + 缩略图的总盘子必须有硬上限。
    """
    high_gb = int(settings.get("DISK_WATERMARK_HIGH_GB") or 0)
    low_gb = int(settings.get("DISK_WATERMARK_LOW_GB") or 0)
    if high_gb <= 0 or low_gb <= 0 or low_gb >= high_gb:
        return {"skipped": True}
    high = high_gb * 1024 * 1024 * 1024
    low = low_gb * 1024 * 1024 * 1024
    total = _dir_usage(MEDIA_DIR)  # 含 private（视频）
    if total <= high:
        return {"ok": True, "total_gb": round(total / 1024**3, 2)}

    removed, freed = 0, 0
    logger.warning("媒体总量 %.2fG 超水位 %sG，开始 LRU 清理视频",
                   total / 1024**3, high_gb)
    # 第一步：删最久未访问的归档视频
    with session_scope() as s:
        rows = (_unshared_media(s)
                .filter(MessageMedia.video_path.isnot(None))
                .order_by(MessageMedia.last_access_at.asc())
                .limit(10000).all())
        for r in rows:
            if total <= low:
                break
            got = _clear_video(r)
            total -= got
            freed += got
            removed += 1
    # 第二步：视频删光仍超 → LRU 删缩略图（保消息正文，只丢图）
    if total > low:
        with session_scope() as s:
            rows = (_unshared_media(s)
                    .filter(MessageMedia.thumb_path.isnot(None))
                    .order_by(MessageMedia.last_access_at.asc())
                    .limit(10000).all())
            for r in rows:
                if total <= low:
                    break
                got = _clear_thumb(r)
                total -= got
                freed += got
                removed += 1
    logger.warning("水位清理完成：删 %d 个文件，释放 %.2fG，现 %.2fG",
                   removed, freed / 1024**3, total / 1024**3)
    return {"removed": removed, "freed_gb": round(freed / 1024**3, 2),
            "total_gb": round(total / 1024**3, 2)}


async def sync_wiki_sources() -> None:
    if not settings.get("WIKI_SYNC_ENABLED"):
        return
    from ..kb.wiki import MediaWikiAdapter
    from ..models import KnowledgeSource
    interval = max(1, int(settings.get("WIKI_FETCH_INTERVAL_HOURS") or 24)) * 3600
    now = datetime.utcnow()
    with session_scope() as s:
        sources = [(x.id, x.url, x.categories, x.last_sync_at)
                   for x in s.query(KnowledgeSource).filter(KnowledgeSource.enabled.is_(True)).all()
                   if not x.last_sync_at or (now - x.last_sync_at).total_seconds() >= interval]
    for source_id, url, categories, _last in sources:
        try:
            await MediaWikiAdapter(url).sync(source_id, categories or ["characters"])
        except Exception as exc:
            logger.warning("Wiki 来源 %s 同步失败: %s", source_id, exc)
            with session_scope() as s:
                src = s.get(KnowledgeSource, source_id)
                if src:
                    src.last_error = str(exc)[:2000]


async def maintenance_loop(stop: asyncio.Event) -> None:
    from .. import outputs
    from ..kb.changelog import poll_all
    last_cleanup = 0.0
    # 启动即抓一次：worker 重启后不用等 20 分钟才知道有没有新版本
    last_changelog = -CHANGELOG_EVERY
    last_wiki_poll = -WIKI_EVERY
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        now = loop.time()
        try:
            if now - last_cleanup > CLEANUP_EVERY:
                last_cleanup = now
                await asyncio.to_thread(cleanup_media)
                await asyncio.to_thread(cleanup_messages)
                await asyncio.to_thread(cleanup_disk_watermark)
                await asyncio.to_thread(_trim_events)
                await asyncio.to_thread(_trim_tasks)
                # GitHub 图片中转仓库的过期清理（配置了才动作）
                try:
                    from ..qqbot.media import cleanup_github_relay
                    await cleanup_github_relay()
                except Exception as e:
                    logger.warning("GitHub 中转清理出错: %s", e)
            if settings.get("RETRIEVAL_ENABLED"):
                try:
                    from ..retrieval import index_pending
                    await asyncio.to_thread(index_pending)
                except Exception as e:
                    logger.debug("消息索引补建失败: %s", e)
            if settings.get("EMBEDDING_ENABLED"):
                try:
                    from ..retrieval import embed_pending
                    await embed_pending()
                except Exception as e:
                    logger.debug("embedding 后台索引失败: %s", e)
            if settings.get("MEMORY_ENABLED"):
                try:
                    from ..memory import summarize_all_pending
                    await summarize_all_pending()
                except Exception as e:
                    logger.debug("QQ 记忆摘要失败: %s", e)
            # 挂在维护循环里而不是 ingest：抓网站不需要 TG 客户端，
            # 账号没登录（need_credentials）时这条线也该照常跑
            if settings.get("CHANGELOG_ENABLED") and \
                    now - last_changelog > CHANGELOG_EVERY:
                last_changelog = now
                try:
                    await poll_all()
                except Exception as e:
                    logger.warning("changelog 轮询出错: %s", e)
            if settings.get("WIKI_SYNC_ENABLED") and now - last_wiki_poll > WIKI_EVERY:
                last_wiki_poll = now
                try:
                    await sync_wiki_sources()
                except Exception as e:
                    logger.warning("Wiki 轮询出错: %s", e)
            await outputs.retry_pending_webhooks()
            # QQ 群推送的失败重试（同款退避节奏）
            from .. import qqbot
            await qqbot.retry_pending()
        except Exception as e:
            logger.warning("维护任务出错: %s", e)
        await asyncio.sleep(WEBHOOK_EVERY)
