"""Normalize source events and persist content before independent processing."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import (
    classify, dedup, glossary, lang, media as media_mod, prompts, settings,
    translate as tr,
)
from .db import session_scope
from .kb.annotate import annotate_from_hits
from .models import Channel, MessageMedia, MonitorMessage, Task
from .util import log_event

logger = logging.getLogger(__name__)


@dataclass
class ChannelSnapshot:
    """频道配置的只读快照。

    用 dataclass 而不是再构造一个 Channel 实例：同主键的临时 ORM 对象会在后续
    session 里引发身份冲突，而且一旦被 autoflush 碰到就可能写脏数据。
    """
    id: int
    tg_id: str | None
    username: str | None
    title: str
    game: str | None
    games: list[str] | None
    prompt_override: str | None
    force_push: bool
    translate: bool
    allow_photo: bool
    allow_video: bool
    allow_document: bool
    max_media_mb: int
    bilingual_policy: str = "zh_first"
    keep_video: bool = False
    video_max_mb: int = 0
    theme: str = "gaming"


def deeplink_for(channel: ChannelSnapshot, msg_id: int | str) -> str:
    if channel.username:
        return f"https://t.me/{channel.username}/{msg_id}"
    raw = (channel.tg_id or "").lstrip("-")
    if raw.startswith("100"):
        raw = raw[3:]
    return f"https://t.me/c/{raw}/{msg_id}"


def already_ingested(channel_id: int, tg_message_id: str) -> bool:
    """去重第 1 层：唯一索引对应的存在性检查。零成本，防重启重放。"""
    with session_scope() as s:
        return s.execute(
            select(MonitorMessage.id).where(
                MonitorMessage.channel_id == channel_id,
                MonitorMessage.tg_message_id == str(tg_message_id),
            )
        ).first() is not None


# ---------------- 相册合并（去重第 1.5 层） ----------------
# 同一相册（grouped_id）会被多条路径拆成多个批次到达：实时监听的
# 防抖窗口切分、对账补拉的 limit 边界切分、实时与补拉并发。第一层
# 只查批次首条 tg_message_id，部分批次首条不同就全部放行 —— 于是
# 「3 张图出现 3 条记录」（2026-09-04 反馈：1图/2图/3图 递增三条）。
# 修复：同 (channel_id, grouped_id) 已有记录（anchor）时，后续批次
# 不再新建记录，而是把缺的媒体/文本并进 anchor。

def _album_anchor_q(s, channel_id: int, grouped_id: str):
    """同相册的 anchor 记录查询。非重复的优先（修复脚本会把多余的
    同组记录标成 duplicate_of=anchor，真正的 anchor 不该是它们）。"""
    return (
        select(MonitorMessage)
        .where(MonitorMessage.channel_id == channel_id,
               MonitorMessage.grouped_id == grouped_id)
        .order_by(MonitorMessage.duplicate_of.isnot(None),
                  MonitorMessage.id.asc())
        .limit(1)
    )


def _find_album_anchor(channel_id: int, grouped_id: str) -> int | None:
    """读侧预检：相册 anchor 的 id。没有返回 None（走正常入库路径）。"""
    with session_scope() as s:
        row = s.execute(_album_anchor_q(s, channel_id, grouped_id)).first()
    return int(row[0].id) if row else None


def _attach_to_anchor(s, anchor: MonitorMessage, text_raw: str,
                      result, media_outs: list, translate: bool,
                      archive_channel: ChannelSnapshot | None = None,
                      replace_snapshot: bool = False) -> bool:
    """把后续批次的媒体/文本并进 anchor。在已开的写事务里调用。

    媒体按 thumb_path 去重（缩略图文件名 = tg 消息 id，天然幂等）；
    文本仅在 anchor 缺失时补。返回是否有实质变更。
    """
    changed = False
    existing = {x.thumb_path for x in anchor.media if x.thumb_path}
    current_media = [x for x in anchor.media if x.status != "superseded"]
    source_ids = {x.source_tg_id: x for x in current_media if x.source_tg_id}
    incoming = {str(o.source_message_id) for o in media_outs if o.source_message_id}
    if replace_snapshot:
        for old in current_media:
            if old.source_tg_id and old.source_tg_id not in incoming:
                old.status = "superseded"
                changed = True
    for o in media_outs:
        old = source_ids.get(str(o.source_message_id))
        if old:
            if old.source_identity == o.source_identity:
                continue
            old.status = "superseded"
            changed = True
        if o.thumb_path and o.thumb_path in existing:
            continue
        media_row = MessageMedia(
            message_id=anchor.id, kind=o.kind, thumb_path=o.thumb_path,
            source_tg_id=str(o.source_message_id) if o.source_message_id else None,
            source_identity=o.source_identity, status=o.status, error=o.error,
            original_path=o.original_path, original_bytes=o.original_bytes, sha256=o.sha256,
            thumb_bytes=o.thumb_bytes, width=o.width, height=o.height,
            duration=o.duration, orig_bytes=o.orig_bytes, mime=o.mime,
            phash=o.phash, dhash=o.dhash, video_path=o.video_path,
            video_bytes=o.video_bytes, video_status=o.video_status,
            has_spoiler=o.has_spoiler,
        )
        s.add(media_row)
        if archive_channel is not None and (o.status == "queued" or o.video_status == "queued"):
            s.flush()
            _queue_media(s, archive_channel.id, media_row,
                         o.source_message_id or anchor.tg_message_id)
        existing.add(o.thumb_path)
        if o.source_message_id:
            source_ids[str(o.source_message_id)] = media_row
        changed = True
        anchor.has_media = True
    if ((text_raw and not (anchor.text_raw or "").strip()) or
            (replace_snapshot and text_raw != (anchor.text_raw or ""))):
        anchor.text_raw = text_raw
        norm = dedup.normalize(text_raw)
        anchor.text_hash = dedup.text_hash(norm) if norm else None
        min_len = int(settings.get("DEDUP_MIN_TEXT_LEN") or 20)
        anchor.simhash = dedup.simhash(norm) if len(norm) >= min_len else None
        # 翻译：本批已译好就直接用（race 路径省一次调用），
        # 否则按频道开关标 pending（待重译）或 disabled
        if result is not None and result.status == "ok" and result.text_zh:
            anchor.text_zh = result.text_zh
            anchor.translate_status = "ok"
            anchor.provider_name = result.provider_name
            anchor.model = result.model
            anchor.tokens_in = result.tokens_in
            anchor.tokens_out = result.tokens_out
            anchor.cost = result.cost
            anchor.from_cache = result.from_cache
        else:
            anchor.text_zh = None
            anchor.translate_status = "pending" if translate and text_raw else "disabled"
        anchor.translate_error = None
        changed = True
    if changed:
        anchor.duplicate_of, anchor.dup_reason = None, None
        s.flush()
        s.expire(anchor, ["media"])
        anchor.has_media = any(x.status != "superseded" for x in anchor.media)
        from .content import record
        record(anchor.id, session=s)
    return changed


def _queue_media(s, channel_id, media_row, source_id):
    from .jobs import enqueue
    enqueue("media", f"media:{media_row.id}", {"channel_id": channel_id,
        "media_id": media_row.id, "tg_message_id": str(source_id)}, session=s)


def _submit_retranslate(s, message_id: int) -> None:
    """给补了文本的 anchor 排一次重译（worker 任务队列异步执行）。

    挂在调用方的写事务里：媒体/文本并入与任务排队同生共死，避免
    嵌套 session 在 SQLite 上抢写锁。
    """
    s.add(Task(kind="retranslate", status="pending",
               payload={"message_id": message_id}))
    logger.info("相册合并补了文本，消息 %s 已排队重译", message_id)


async def _merge_album(channel, anchor_id: int, messages: list,
                       text_raw: str, media_outs: list, *, replace_snapshot=False,
                       deferred=False) -> None:
    """相册后续批次并入 anchor：不新建记录、不重推（返回 None 语义）。"""
    gid = str(getattr(messages[0], "grouped_id", "") or "")
    changed = False
    with session_scope() as s:
        anchor = s.get(MonitorMessage, anchor_id)
        if anchor is None:
            return          # anchor 被清理（TTL）—— 本批放弃，宁缺毋滥
        changed = _attach_to_anchor(s, anchor, text_raw, None, media_outs,
                                    channel.translate, archive_channel=channel,
                                    replace_snapshot=replace_snapshot)
        if changed and anchor.translate_status == "pending":
            if deferred:
                from .jobs import enqueue
                import hashlib
                revision = hashlib.sha256(text_raw.encode()).hexdigest()
                enqueue("translate", f"translate:{anchor.id}:{revision}",
                        {"message_id": anchor.id}, session=s)
            else:
                _submit_retranslate(s, anchor.id)
        anchor_id = anchor.id
    if changed:
        logger.info("相册合并：批次 %s 并入消息 #%s",
                    [m.id for m in messages], anchor_id)


def _find_text_dup(text_hash: str, sim: str, exclude_channel: int | None) -> tuple[int, str] | None:
    """去重第 2 层：精确指纹 → SimHash 近似。返回 (首条 id, 原因)。"""
    if not settings.get("DEDUP_ENABLED"):
        return None
    window = int(settings.get("DEDUP_WINDOW_DAYS") or 7)
    since = datetime.utcnow() - timedelta(days=window)
    max_dist = int(settings.get("SIMHASH_DISTANCE") or 3)

    with session_scope() as s:
        row = s.execute(
            select(MonitorMessage.id).where(
                MonitorMessage.text_hash == text_hash,
                MonitorMessage.duplicate_of.is_(None),
                MonitorMessage.published_at >= since,
            ).order_by(MonitorMessage.published_at.asc()).limit(1)
        ).first()
        if row:
            return int(row[0]), "text_exact"

        if not sim:
            return None
        # SimHash 只能全表扫窗口内的记录。窗口 7 天 + 几十频道量级完全够用
        cands = s.execute(
            select(MonitorMessage.id, MonitorMessage.simhash).where(
                MonitorMessage.simhash.isnot(None),
                MonitorMessage.simhash != "",
                MonitorMessage.duplicate_of.is_(None),
                MonitorMessage.published_at >= since,
            ).order_by(MonitorMessage.published_at.asc()).limit(5000)
        ).all()
    for mid, cand in cands:
        if dedup.hamming_hex(sim, cand) <= max_dist:
            return int(mid), "simhash"
    return None


def _find_image_dup(fingerprints: list[tuple[str | None, str | None]]) -> tuple[int, str] | None:
    """去重第 3 层：图片感知哈希。抓「同图配不同文案」与纯图转发。

    pHash 与 dHash 取距离较小者判定 —— 实测两者互补：不透明水印 pHash 距离 22
    （漏判）而 dHash 只有 1；整体调亮 / 强压缩两者都是 1-2。取 min 后在 91 组
    不同截图上的误判为 0，所以这是纯赚的召回率。

    注意：裁剪与加边框两者都抓不到（距离 11-29），这是感知哈希的固有局限。

    无区分度指纹（全零/近零，视频黑首帧等）不参与距离计算 —— 2026-09 案例：
    黑首帧的 242s 预告片和 35s 过场动画 dhash 距离 1 被误判成同一视频。
    一边无效就只用另一边，两边都无效不判重。
    """
    if not settings.get("DEDUP_ENABLED") or not fingerprints:
        return None
    window = int(settings.get("DEDUP_WINDOW_DAYS") or 7)
    since = datetime.utcnow() - timedelta(days=window)
    max_dist = int(settings.get("PHASH_DISTANCE") or 5)

    with session_scope() as s:
        cands = s.execute(
            select(MessageMedia.message_id, MessageMedia.phash, MessageMedia.dhash)
            .join(MonitorMessage, MonitorMessage.id == MessageMedia.message_id)
            .where((MessageMedia.phash.isnot(None) | MessageMedia.dhash.isnot(None)),
                   MonitorMessage.duplicate_of.is_(None),
                   MonitorMessage.published_at >= since)
            .order_by(MessageMedia.id.asc()).limit(5000)
        ).all()

    # A message is a duplicate only when its complete fingerprint set matches
    # one existing message.  Matching one image from a larger album would hide
    # the remaining new images (the production data-loss bug).
    grouped: dict[int, list[tuple[str | None, str | None]]] = {}
    for mid, cand_p, cand_d in cands:
        grouped.setdefault(int(mid), []).append((cand_p, cand_d))
    incoming = [(p, d) for p, d in fingerprints
                if dedup.informative(p) or dedup.informative(d)]
    if not incoming:
        return None
    for mid, existing in grouped.items():
        existing = [(p, d) for p, d in existing
                    if dedup.informative(p) or dedup.informative(d)]
        if len(existing) != len(incoming):
            continue
        remaining = list(existing)
        matched = True
        for ph, dh in incoming:
            found = None
            for i, (cand_p, cand_d) in enumerate(remaining):
                dists = []
                if dedup.informative(ph) and dedup.informative(cand_p):
                    dists.append(dedup.hamming_hex(ph, cand_p))
                if dedup.informative(dh) and dedup.informative(cand_d):
                    dists.append(dedup.hamming_hex(dh, cand_d))
                if dists and min(dists) <= max_dist and max(dists) <= 15:
                    found = i
                    break
            if found is None:
                matched = False
                break
            remaining.pop(found)
        if matched and not remaining:
            return mid, "phash"
    return None


def _existing_translation(message_id: int) -> str | None:
    with session_scope() as s:
        row = s.get(MonitorMessage, message_id)
        if row and row.translate_status == "ok" and row.text_zh:
            return row.text_zh
    return None


def _extract_text(messages: list) -> tuple[str, object | None]:
    """相册里通常只有一条带 caption。取最长的那条非空文本。

    同时返回文本所属的消息对象 —— message entities（含剧透区间）挂在
    文本那条上，不一定是相册的第一条。
    """
    best_msg, best_len = None, 0
    for m in messages:
        t = (m.message or "").strip()
        if len(t) > best_len:
            best_msg, best_len = m, len(t)
    return ((best_msg.message or "").strip(), best_msg) if best_msg else ("", None)


def _spoiler_ranges(text_msg, text: str) -> list[list[int]]:
    """从 message entities 里提取剧透区间，UTF-16 偏移换算成字符区间。

    返回 [[start, length], ...]，相对 text（字符索引）。TG 保证 entity
    不跨消息实体；相册合并后 text 取自 text_msg，区间也只对它有效。
    """
    from telethon.tl.types import MessageEntitySpoiler
    from .util import utf16_to_char_offset

    ranges: list[list[int]] = []
    for ent in (getattr(text_msg, "entities", None) or []):
        if not isinstance(ent, MessageEntitySpoiler):
            continue
        start = utf16_to_char_offset(text, ent.offset)
        end = utf16_to_char_offset(text, ent.offset + ent.length)
        if end > start:
            ranges.append([start, end - start])
    return ranges


async def _process_media(client, channel: ChannelSnapshot,
                         messages: list, *, deferred: bool = False) -> list:
    """批内媒体处理（频道过滤 + 大小上限）。主路径与相册合并共用。"""
    media_outs = []
    for m in messages:
        if not getattr(m, "media", None):
            continue
        try:
            out = media_mod.describe(m) if deferred else await media_mod.process(
                client, m, channel.id,
                keep_video=channel.keep_video,
                video_max_mb=channel.video_max_mb,
                defer_archive=True)
        except Exception as e:
            logger.warning("处理媒体失败 msg=%s: %s", m.id, e)
            log_event("warning", "media", f"{channel.title} msg {m.id}: {e}")
            continue
        if out is None:
            continue
        if out.kind == "photo" and not channel.allow_photo:
            continue
        if out.kind == "video" and not channel.allow_video:
            continue
        if out.kind == "document" and not channel.allow_document:
            continue
        limit_mb = channel.max_media_mb or 0
        if limit_mb and out.orig_bytes > limit_mb * 1024 * 1024:
            out.status, out.error = "blocked_size", f"源文件超过频道 {limit_mb} MB 限制"
            if out.kind == "video":
                out.video_status = "skipped_size"
        media_outs.append(out)
    return media_outs


async def ingest(client, channel_row_id: int, messages: list, *, deferred: bool = False,
                 complete_snapshot: bool = False) -> int | None:
    """处理一条消息（相册为一组）。返回入库的 message id，跳过时返回 None。"""
    if not messages:
        return None
    messages = sorted(messages, key=lambda m: m.id)
    first = messages[0]

    with session_scope() as s:
        ch = s.get(Channel, channel_row_id)
        if ch is None or not ch.enabled:
            return None
        # 快照一份，session 关了还要用
        channel = ChannelSnapshot(
            id=ch.id, tg_id=ch.tg_id, username=ch.username, title=ch.title,
            game=ch.game, games=ch.games or None,
            prompt_override=ch.prompt_override,
            force_push=ch.force_push, translate=ch.translate,
            allow_photo=ch.allow_photo, allow_video=ch.allow_video,
            allow_document=ch.allow_document, max_media_mb=ch.max_media_mb,
            bilingual_policy=ch.bilingual_policy or "zh_first",
            keep_video=ch.keep_video, video_max_mb=ch.video_max_mb or 0)
        channel.theme = ch.theme or "gaming"

    # ---------- 相册合并（第 1.5 层，读侧预检） ----------
    # 同 grouped_id 已有记录 → 后续批次并入 anchor，不再新建。
    # 纯图的部分批次没有文本指纹、图片又互不相同，其余任何一层都拦不住。
    gid = str(getattr(first, "grouped_id", "") or "")
    if gid:
        anchor_id = _find_album_anchor(channel.id, gid)
        if anchor_id is not None:
            media_outs = await _process_media(client, channel, messages, deferred=deferred)
            text_raw, _ = _extract_text(messages)
            await _merge_album(channel, anchor_id, messages, text_raw,
                               media_outs, replace_snapshot=complete_snapshot,
                               deferred=deferred)
            return None

    if already_ingested(channel.id, first.id):
        if complete_snapshot or getattr(first, "edit_date", None):
            with session_scope() as s:
                anchor_id = s.query(MonitorMessage.id).filter_by(
                    channel_id=channel.id, tg_message_id=str(first.id)).scalar()
            media_outs = await _process_media(client, channel, messages, deferred=deferred)
            text_raw, _ = _extract_text(messages)
            await _merge_album(channel, anchor_id, messages, text_raw, media_outs,
                               replace_snapshot=True, deferred=deferred)
        return None

    text_raw, text_msg = _extract_text(messages)
    norm = dedup.normalize(text_raw)
    min_len = int(settings.get("DEDUP_MIN_TEXT_LEN") or 20)
    t_hash = dedup.text_hash(norm) if norm else None
    sim = dedup.simhash(norm) if len(norm) >= min_len else ""

    dup: tuple[int, str] | None = None
    if t_hash and len(norm) >= min_len:
        dup = _find_text_dup(t_hash, sim, channel.id)
        if dup is not None and dup[1] != "text_exact":
            logger.info("文本相似候选 #%s，保留原文和独立译文", dup[0])
            dup = None
        if dup:
            with session_scope() as s:
                old = s.get(MonitorMessage, dup[0])
                if old is None or old.text_raw != text_raw or old.theme != channel.theme:
                    dup = None

    # ---------- 媒体 ----------
    media_outs = await _process_media(client, channel, messages, deferred=deferred)
    if dup is not None and media_outs:
        # Equal captions are common across a sequence of new images/videos.
        # Until complete file identities prove equality, preserve this story.
        logger.info("同文消息 #%s 带独立媒体，保留为新内容", dup[0])
        dup = None

    if dup is None:
        # Image hashes are useful for related-story candidates, but a visual
        # similarity alone must not hide a new story or discard its caption.
        # Text/album identity remains the only automatic duplicate decision.
        fps = [(o.phash, o.dhash) for o in media_outs
               if dedup.informative(o.phash) or dedup.informative(o.dhash)]
        image_candidate = _find_image_dup(fps)
        if image_candidate:
            logger.info("媒体相似候选 #%s，保留为独立消息", image_candidate[0])

    # ---------- 语言路由 ----------
    # 在翻译之前决定「要不要翻译」。全中文原文一个字都不送模型
    lang_route = None
    text_for_translate = text_raw
    text_dropped = None

    if text_raw and settings.get("LANG_ROUTE_ENABLED"):
        lang_route = lang.route(
            text_raw, policy=channel.bilingual_policy,
            zh_min_chars=int(settings.get("BILINGUAL_ZH_MIN_CHARS") or 10))
        text_for_translate = lang_route.translate_text
        text_dropped = lang_route.dropped_text or None

    # ---------- 游戏判定 / 内容分类 ----------
    # 整条管线只做这一次全文匹配，结果供三处复用：判定游戏、标注实体、
    # 翻译对照表。之前是三次独立匹配（各自 find_hits 一遍 3400+ 别名）
    hits = []
    det = classify.Detection()
    if text_raw and channel.theme == "gaming":
        hits = await asyncio.to_thread(glossary.find_hits,
            text_raw, glossary.terms_all(langs=("en", "ja", "zh")))
        det = (classify.detect(text_raw, channel, hits) if deferred else
               await classify.resolve_game(text_raw, channel, hits=hits))
    elif text_raw:
        from .themes import get_theme
        det.topics = [str(topic) for topic in get_theme(channel.theme).config.get("topics", [])
                      if str(topic).casefold() in text_raw.casefold()]

    game = (det.game or channel.game) if channel.theme == "gaming" else None

    # ---------- 实体标注 ----------
    # 中文原文不翻译也照样标注 —— 这正是中文别名（卡妈、看板娘）的用处。
    # 只留本游戏与通用术语：跨游戏匹配是为了判定游戏，标注要按判定结果收敛，
    # 否则「沃雅妮莎」这条原神消息会把同名的别家实体也标上
    entities_data = None
    if settings.get("ENTITY_TAG_ENABLED"):
        scoped = [h for h in hits if h.game in ("", glossary.normalize_game(game))]
        entities_data = annotate_from_hits(scoped, game)

    # ---------- 翻译 ----------
    result = tr.TranslateResult(status="skipped")
    if not text_for_translate:
        if lang_route and lang_route.keep_text:
            # 原文本来就是中文（或双语重复的中文段）：原样输出，零 AI 调用
            result = tr.TranslateResult(text_zh=lang_route.keep_text,
                                        status="skipped_zh")
        else:
            result.status = "skipped"          # 纯图无文字：零 AI 调用
    elif not channel.translate:
        result.status = "disabled"
    elif deferred:
        result.status = "pending"
    elif (dup is not None and not channel.force_push
          and dup[1] == "text_exact"):
        # 文本判重（text_exact / simhash）是同文案：复用译文零成本且正确。
        # phash 判重是「同图配不同文案」—— 图重复不代表文字重复，复用译文
        # 必然张冠李戴（2026-09 案例：黑首帧假阳性撞上崩铁推文，ZZZ 视频挂
        # 着崩铁译文）。phash 判重的消息落到下面正常翻译分支。
        reused = _existing_translation(dup[0])
        if reused:
            result = tr.TranslateResult(text_zh=reused, status="ok", from_cache=True)
        else:
            result.status = "skipped"
    else:
        prompt = prompts.resolve_prompt(channel, game)
        if channel.theme == "gaming":
            prompt += prompts.game_context(game)
        try:
            try:
                result = await tr.translate(text_for_translate, prompt, game,
                                            entities_data, hits=hits,
                                            bilingual_policy=channel.bilingual_policy,
                                            **({"theme": channel.theme} if channel.theme != "gaming" else {}))
            except TypeError as exc:
                # Keep compatibility with third-party/test translators that
                # still implement the pre-policy signature.
                if "bilingual_policy" not in str(exc):
                    raise
                result = await tr.translate(text_for_translate, prompt, game,
                                            entities_data, hits=hits)
        except Exception as e:
            logger.exception("翻译异常")
            result = tr.TranslateResult(status="failed", error=str(e))
        # per_block：只有非中文段送去翻译，译完要按原文块序把中文段拼回来，
        # 否则正文会只剩译文、原文的中文段凭空消失
        if (result.status == "ok" and lang_route is not None
                and lang_route.keep_text):
            result.text_zh = lang.stitch(lang_route, result.text_zh or "")

    if result.status == "failed":
        log_event("error", "translate",
                  f"{channel.title} msg {first.id} 翻译失败: {result.error}")

    # ---------- 入库 ----------
    published = getattr(first, "date", None)
    if published is not None and published.tzinfo is not None:
        published = published.replace(tzinfo=None)

    sender_name = None
    try:
        if getattr(first, "post_author", None):
            sender_name = first.post_author
    except Exception:
        pass

    try:
        lang_detected = lang_route.lang if lang_route else None
        # 剧透：文本区间只在「显示文本就是原文」时才有意义（skipped_zh 的
        # 纯中文场景）。走模型翻译的 ok 路径译文被重写，区间无法对齐 ——
        # 只打 has_spoiler 标记，这是诚实限制，不做强行对齐
        text_spoiler = _spoiler_ranges(text_msg, text_raw) if text_raw else []
        has_spoiler = bool(text_spoiler) or any(o.has_spoiler for o in media_outs)
        spoiler_ranges = (text_spoiler
                          if text_spoiler and (result.text_zh or "") == text_raw
                          else None)
        new_id = _persist(channel, first, messages, text_raw, t_hash, sim, dup,
                          result, media_outs, published, sender_name, entities_data,
                          lang_detected, text_dropped, det,
                          spoiler_ranges=spoiler_ranges, has_spoiler=has_spoiler)
        if new_id is not None and settings.get("RETRIEVAL_ENABLED"):
            try:
                from .retrieval import index_message
                await asyncio.to_thread(index_message, new_id)
            except Exception as e:
                logger.debug("消息检索索引失败 #%s: %s", new_id, e)
        return new_id
    except IntegrityError:
        # 去重第 1 层的兜底：唯一索引挡住并发重复插入。这不是错误，是设计
        logger.info("消息 %s/%s 已存在（并发重复），跳过", channel.title, first.id)
        return None


def _persist(channel, first, messages, text_raw, t_hash, sim, dup, result,
             media_outs, published, sender_name, entities_data=None,
             lang_detected=None, text_dropped=None, det=None,
             spoiler_ranges=None, has_spoiler=False) -> int | None:
    with session_scope() as s:
        # ---------- 相册合并（写事务内复查，堵并发 TOCTOU） ----------
        # 读侧预检到这里的窗口期里（媒体下载可达数十秒），并发的兄弟
        # 批次可能已把同相册记录插进来。写事务串行化（SQLite 写锁），
        # 这里再查一次：有 anchor 就并入而不是再插一条。
        gid = str(getattr(first, "grouped_id", "") or "")
        if gid:
            anchor = s.execute(
                _album_anchor_q(s, channel.id, gid)).scalar_one_or_none()
            if anchor is not None:
                changed = _attach_to_anchor(s, anchor, text_raw, result,
                                            media_outs, channel.translate,
                                            archive_channel=channel)
                need_retranslate = (changed and
                                    anchor.translate_status == "pending")
                anchor_id = anchor.id
                if need_retranslate:
                    _submit_retranslate(s, anchor_id)
                if changed:
                    logger.info("相册合并（并发兜底）：消息批次 %s 并入 #%s",
                                first.id, anchor_id)
                return None

        row = MonitorMessage(
            channel_id=channel.id,
            tg_message_id=str(first.id),
            grouped_id=str(first.grouped_id) if getattr(first, "grouped_id", None) else None,
            text_raw=text_raw,
            theme=channel.theme,
            text_zh=result.text_zh or None,
            translate_status=result.status,
            translate_error=result.error,
            provider_name=result.provider_name,
            model=result.model,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cost=result.cost,
            from_cache=result.from_cache,
            glossary_hits=result.glossary_hits or None,
            glossary_miss=result.glossary_miss or None,
            text_hash=t_hash,
            simhash=sim or None,
            duplicate_of=dup[0] if dup else None,
            dup_reason=dup[1] if dup else None,
            deeplink=getattr(first, "source_url", None) or deeplink_for(channel, first.id),
            sender_name=sender_name,
            has_media=bool(media_outs),
            # JSON 列，直接给 list ——
            # 自己 json.dumps 会被 SQLAlchemy 再编码一层，读出来是字符串不是列表
            entities=(entities_data or {}).get("entities") or None,
            lang_detected=lang_detected,
            text_dropped=text_dropped,
            game_detected=(det.game if det else None),
            game_scores=(det.as_json() if det else None),
            topics=((det.topics or None) if det else None),
            version_tag=(det.version if det else None),
            spoiler_ranges=spoiler_ranges,
            has_spoiler=has_spoiler,
            published_at=published or datetime.utcnow(),
        )
        s.add(row)
        s.flush()
        archive_tasks = []
        for o in media_outs:
            media_row = MessageMedia(
                message_id=row.id, kind=o.kind, thumb_path=o.thumb_path,
                source_tg_id=str(o.source_message_id) if o.source_message_id else None,
                source_identity=o.source_identity, status=o.status, error=o.error,
                original_path=o.original_path, original_bytes=o.original_bytes, sha256=o.sha256,
                thumb_bytes=o.thumb_bytes, width=o.width, height=o.height,
                duration=o.duration, orig_bytes=o.orig_bytes, mime=o.mime,
                phash=o.phash, dhash=o.dhash, video_path=o.video_path,
                video_bytes=o.video_bytes, video_status=o.video_status,
                has_spoiler=o.has_spoiler,
            )
            s.add(media_row)
            if o.status == "queued" or o.video_status == "queued":
                archive_tasks.append((media_row, o))
        s.flush()
        for media_row, out in archive_tasks:
            _queue_media(s, channel.id, media_row, out.source_message_id or first.id)
        if result.status == "pending":
            from .jobs import enqueue
            enqueue("translate", f"translate:{row.id}:{t_hash}", {"message_id": row.id}, session=s)
        ch = s.get(Channel, channel.id)
        if ch is not None:
            ch.message_count += 1
            ch.last_message_at = row.published_at
        new_id = row.id
        s.flush()
        from .content import record
        record(new_id, session=s)

    logger.info("入库 #%s [%s] dup=%s 翻译=%s", new_id, channel.title,
                dup[1] if dup else "-", result.status)
    return new_id
