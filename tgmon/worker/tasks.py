"""admin 下发的任务处理器。

为什么要队列：Telethon 的 session 是 SQLite 文件，两个进程同时用同一个 session
会互锁、并可能写坏授权状态。所以只有 worker 持有连接，admin 想做任何需要 TG
的事都写一条 task，由 worker 执行后回填 result。网页登录就是靠这个跑通的。
"""
from __future__ import annotations

import logging
import asyncio
from datetime import datetime
from types import SimpleNamespace

from telethon import utils as tl_utils
from telethon.errors import (
    PhoneCodeExpiredError, PhoneCodeInvalidError, SessionPasswordNeededError,
)

from .. import outputs, settings
from ..db import session_scope
from ..models import Channel, MessageMedia, MonitorMessage, Task, WorkerState
from ..providers import test_provider

logger = logging.getLogger(__name__)


async def handle(runner, task_id: int, kind: str, payload: dict) -> dict:
    """分发。返回值写入 task.result。抛异常则写 task.error。"""
    fn = _HANDLERS.get(kind)
    if fn is None:
        raise ValueError(f"未知任务类型 {kind}")
    # 执行期间把动作写进 worker_state.current_action —— 概览页
    # 「系统现在在干什么」的直接答案，不然对账/补历史在后台跑毫无感知
    _set_action(_action_label(kind, payload))
    try:
        # 需要边跑边回写进度的任务拿到 task_id
        if kind in ("backfill", "kb_smart_sync", "sync_dialogs"):
            return await fn(runner, payload or {}, task_id)
        return await fn(runner, payload or {})
    finally:
        _set_action("")


def _set_action(text: str) -> None:
    """更新 WorkerState.current_action。失败不抛 —— 展示层不能影响任务。"""
    try:
        with session_scope() as s:
            row = s.get(WorkerState, 1)
            if row is not None:
                row.current_action = text or ""
    except Exception:
        pass


def _action_label(kind: str, payload: dict) -> str:
    if kind == "backfill":
        return f"补历史（频道 {payload.get('channel_id')}，{payload.get('limit')} 条）"
    if kind == "kb_import":
        return f"知识库导入（{payload.get('game')}）"
    if kind == "kb_smart_sync":
        g = payload.get("game") or "全部"
        return f"知识库智能同步（{g}）"
    if kind == "kb_import_gachabase":
        return f"知识库补充抓取（{payload.get('game')}）"
    if kind == "wiki_sync":
        return f"Wiki 同步（来源 {payload.get('source_id')}）"
    if kind == "changelog_poll":
        return "抓 changelog"
    if kind == "media_cleanup":
        return "媒体清理"
    if kind == "retranslate":
        return f"重译消息 {payload.get('message_id')}"
    if kind == "archive_video":
        return f"归档视频 {payload.get('channel_id')}/{payload.get('tg_message_id')}"
    if kind == "sync_dialogs":
        return "同步频道列表"
    return f"任务 {kind}"


def _task_progress(task_id: int, result: dict) -> None:
    """执行中回写 task.result 供轮询。finish() 的终值会覆盖这里。

    只在 status=running 时写：任务已结束还去改 result 会盖掉终态
    （比如 failed 的 error 说明没被覆盖，但 result 会被补写）。
    """
    if not task_id:
        return
    try:
        with session_scope() as s:
            row = s.get(Task, task_id)
            if row is not None and row.status == "running":
                row.result = result
    except Exception:
        pass


def _parse_day(v) -> datetime | None:
    """YYYY-MM-DD → naive datetime。坏输入返回 None（= 不设下限）。"""
    if not v:
        return None
    try:
        return datetime.strptime(str(v).strip()[:10], "%Y-%m-%d")
    except ValueError:
        return None


async def _iter_and_ingest(client, cid: int, tg_id: int, *, limit: int,
                           offset_id: int | None = None,
                           min_date: datetime | None = None,
                           progress_cb=None, persistent_cursor: bool = False) -> dict:
    """拉一个频道的历史消息并逐批入库。backfill 与 catch-up 共用。

    返回 {"ingested": 入库数, "total": 批次数}。
    progress_cb(done, total) 每处理一批调一次。
    """
    from .. import jobs
    from ..models import SourceCursor
    entity = await client.get_entity(tg_id)
    if persistent_cursor:
        with session_scope() as s:
            cursor = s.get(SourceCursor, cid)
            if cursor and not cursor.complete:
                offset_id, min_date = cursor.offset_id or None, cursor.cutoff
    messages = []
    exhausted, oldest, highest = True, offset_id or 0, 0
    # offset_id=None 不能传 —— Telethon 1.38.1 内部会 max(None, 0) 直接炸
    iter_kwargs: dict = {"limit": limit}
    if offset_id:
        iter_kwargs["offset_id"] = offset_id
    async for msg in client.iter_messages(entity, **iter_kwargs):
        # msg.date 是 UTC aware；DB 统一 naive，对齐再比
        if min_date and msg.date and msg.date.replace(tzinfo=None) < min_date:
            break
        messages.append(msg)
        oldest, highest = min(oldest or msg.id, msg.id), max(highest, msg.id)
    if len(messages) >= limit:
        exhausted = False
    event_ids = await asyncio.to_thread(jobs.save_events, cid, messages)
    if persistent_cursor:
        # The page is durable before advancing the cursor. A crash between
        # these transactions only replays the page, which unique keys absorb.
        with session_scope() as s:
            cursor = s.get(SourceCursor, cid)
            if cursor is None:
                cursor = SourceCursor(channel_id=cid)
                s.add(cursor)
            cursor.offset_id, cursor.high_id = oldest, max(cursor.high_id or 0, highest)
            cursor.complete, cursor.cutoff = exhausted, min_date
            cursor.updated_at, cursor.error = datetime.utcnow(), None
    if progress_cb:
        progress_cb(len(event_ids), len(messages))
    return {"ok": True, "ingested": len(event_ids), "total": len(messages),
            "complete": exhausted, "offset_id": oldest}


async def _backfill(runner, payload: dict, task_id: int = 0) -> dict:
    """把某频道的历史消息补进来，方便刚勾选频道就能看到东西。

    默认语义已改为「对齐最近 N 天」（ALIGN_WINDOW_DAYS，一般 3 天）：
    不指定 min_date 时自动带窗口下限，窗口外的不拉。
    limit：最多拉多少条（上限 1000 —— 再多 worker 单任务跑几小时，
    心跳还在但页面早关了，意义不大）。
    offset_id：从这条消息之前（更旧）开始拉 —— 「最近一段已入库，想补更早的」。
    min_date（YYYY-MM-DD）：显式指定时优先（想补更早历史用）。

    进度实时回写 task.result（每处理一批写一次），admin 每 2 秒轮询展示。
    """
    cid = int(payload.get("channel_id") or 0)
    limit = max(1, min(int(payload.get("limit") or 20), 1000))
    offset_id = int(payload.get("offset_id") or 0) or None
    min_date = _parse_day(payload.get("min_date"))
    if min_date is None:
        # 未显式指定日期 → 默认对齐窗口（最近 N 天）
        from datetime import datetime as _dt, timedelta as _td
        min_date = _dt.utcnow() - _td(days=_backfill_days())
    with session_scope() as s:
        ch = s.get(Channel, cid)
        if ch is None:
            raise ValueError("频道不存在")
        # website 源的 tg_id 是域名，int() 会炸。这类频道的「补历史」是 changelog
        # 抓取，不走这条路
        if ch.source_type != "telegram":
            raise ValueError("这是网站源，补历史请去知识库页抓 changelog")
        tg_id = int(ch.tg_id)
    client = await runner.require_authorized_client()

    def cb(done: int, total: int) -> None:
        pct = int(done / total * 100) if total > 0 else 0
        _task_progress(task_id, {
            "ingested": done, "done": done, "total": total, "percent": pct,
            "step_name": f"正在拉取与翻译第 {done}/{total} 批"
        })

    return await _iter_and_ingest(client, cid, tg_id, limit=limit,
                                  offset_id=offset_id, min_date=min_date,
                                  progress_cb=cb)


async def _catchup_now(runner, payload: dict) -> dict:
    """让 worker 的对齐循环立即跑一轮（后台「立即对齐」按钮）。

    payload.channel_id 非空时只对齐该频道（频道页/编辑页的单频道按钮）。
    """
    cid = int(payload.get("channel_id") or 0) or None
    runner.request_catchup(cid)
    return {"ok": True, "hint": "对齐循环已触发，几秒内开始"}


def _backfill_days() -> int:
    from ..settings import get
    return max(1, int(get("ALIGN_WINDOW_DAYS") or 3))


# ---------------- 登录流程 ----------------

async def _send_code(runner, payload: dict) -> dict:
    phone = (payload.get("phone") or settings.get("PHONE_NUMBER") or "").strip()
    if not phone:
        raise ValueError("手机号为空")
    client = await runner.require_client()
    sent = await client.send_code_request(phone)
    runner.login_phone = phone
    runner.login_hash = sent.phone_code_hash
    logger.info("验证码已发送到 %s（发到 TG 客户端，不是短信）", phone)
    return {"sent": True, "phone": phone,
            "hint": "验证码发到你的 Telegram 客户端，不是短信"}


async def _sign_in(runner, payload: dict) -> dict:
    code = (payload.get("code") or "").strip()
    if not code:
        raise ValueError("验证码为空")
    if not runner.login_hash:
        raise ValueError("没有待验证的登录请求，请先点发送验证码")
    client = await runner.require_client()
    try:
        await client.sign_in(phone=runner.login_phone, code=code,
                             phone_code_hash=runner.login_hash)
    except SessionPasswordNeededError:
        return {"need_2fa": True}
    except PhoneCodeInvalidError:
        raise ValueError("验证码不对")
    except PhoneCodeExpiredError:
        runner.login_hash = None
        raise ValueError("验证码已过期，请重新发送")
    me = await client.get_me()
    runner.login_hash = None
    await runner.after_login()
    return {"ok": True, "user": _me_name(me)}


async def _sign_in_2fa(runner, payload: dict) -> dict:
    password = payload.get("password") or ""
    if not password:
        raise ValueError("两步验证密码为空")
    client = await runner.require_client()
    await client.sign_in(password=password)
    me = await client.get_me()
    runner.login_hash = None
    await runner.after_login()
    return {"ok": True, "user": _me_name(me)}


async def _logout(runner, payload: dict) -> dict:
    client = runner.client
    if client is not None and client.is_connected():
        try:
            await client.log_out()
        except Exception as e:
            logger.warning("log_out 失败（继续删除本地 session）: %s", e)
    await runner.drop_session()
    return {"ok": True}


def _me_name(me) -> str:
    name = getattr(me, "first_name", "") or ""
    uname = getattr(me, "username", None)
    return f"{name} (@{uname})" if uname else name or str(getattr(me, "id", ""))


# ---------------- 频道同步 ----------------

async def _sync_dialogs(runner, payload: dict, task_id: int = 0) -> dict:
    """拉取账号已加入的全部频道/群组，写进 channel 表。

    不覆盖你在后台改过的设置（enabled / game / prompt 等），只更新元信息。
    """
    _task_progress(task_id, {"step": "pulling", "step_name": "正在从 Telegram 拉取对话与频道列表..."})
    client = await runner.require_authorized_client()
    seen, added, updated = 0, 0, 0
    rows = []
    async for dialog in client.iter_dialogs():
        ent = dialog.entity
        if not (dialog.is_channel or dialog.is_group):
            continue
        try:
            marked = tl_utils.get_peer_id(ent)
        except Exception:
            continue
        kind = "group" if dialog.is_group else "channel"
        if getattr(ent, "megagroup", False):
            kind = "megagroup"
        rows.append({
            "tg_id": str(marked),
            "username": getattr(ent, "username", None),
            "title": (dialog.name or getattr(ent, "title", "") or "")[:300],
            "kind": kind,
            "is_private": not bool(getattr(ent, "username", None)),
            "last": dialog.date.replace(tzinfo=None) if dialog.date else None,
        })
        seen += 1

    with session_scope() as s:
        for r in rows:
            row = (s.query(Channel)
                   .filter(Channel.source_type == "telegram",
                           Channel.tg_id == r["tg_id"])
                   .first())
            if row is None:
                s.add(Channel(source_type="telegram", tg_id=r["tg_id"],
                              username=r["username"], title=r["title"],
                              kind=r["kind"], is_private=r["is_private"],
                              enabled=False, last_message_at=r["last"]))
                added += 1
            else:
                row.title = r["title"]
                row.username = r["username"]
                row.kind = r["kind"]
                row.is_private = r["is_private"]
                if r["last"] and (row.last_message_at is None
                                  or r["last"] > row.last_message_at):
                    row.last_message_at = r["last"]
                updated += 1
    await runner.refresh_watchlist()
    return {"seen": seen, "added": added, "updated": updated}


# ---------------- 运维 ----------------

async def _test_provider(runner, payload: dict) -> dict:
    pid = int(payload.get("provider_id") or 0)
    if not pid:
        raise ValueError("缺少 provider_id")
    return await test_provider(pid)


async def _retranslate(runner, payload: dict) -> dict:
    """重译一条消息。走和 pipeline 完全相同的判定路径。

    以前这里只用 `ch.game`，于是「按消息识别游戏」对重译完全不生效 ——
    多游戏频道（channel.game 为空）里重译出来的还是不带任何术语表的结果。
    现在一并重算 game / topics / version 并回写。

    payload 里可以带 game 覆盖判定结果，用于后台手工纠正。
    """
    from .. import classify, glossary, lang, prompts, translate as tr
    from ..kb.annotate import annotate_from_hits

    mid = int(payload.get("message_id") or 0)
    forced_game = (payload.get("game") or "").strip() or None

    with session_scope() as s:
        row = s.get(MonitorMessage, mid)
        if row is None:
            raise ValueError("消息不存在")
        text_raw, chan_id = row.text_raw, row.channel_id
        ch = s.get(Channel, chan_id)
        channel_copy = (SimpleNamespace(
            game=ch.game, games=ch.games or None,
            theme=ch.theme or "gaming",
            prompt_override=ch.prompt_override,
            bilingual_policy=ch.bilingual_policy or "zh_first")
                         if ch else None)
    if not text_raw:
        return {"ok": False, "error": "这条没有文本"}

    hits = glossary.find_hits(
        text_raw, glossary.terms_all(langs=("en", "ja", "zh")))
    if getattr(channel_copy, "theme", "gaming") == "gaming":
        det = await classify.resolve_game(text_raw, channel_copy, hits=hits)
    else:
        det, hits = classify.Detection(), []
    theme = getattr(channel_copy, "theme", "gaming")
    game = (forced_game or det.game or (channel_copy.game if channel_copy else None)) if theme == "gaming" else None
    if forced_game:
        det.game, det.method = glossary.normalize_game(forced_game), "manual"
        det.reasons.insert(0, f"后台手工指定 → {forced_game}")

    scoped = [h for h in hits if h.game in ("", glossary.normalize_game(game))]
    entities_data = annotate_from_hits(scoped, game)

    prompt = prompts.resolve_prompt(channel_copy, game)
    if getattr(channel_copy, "theme", "gaming") == "gaming":
        prompt += prompts.game_context(game)
    # 绕过缓存读取：重译的意图就是不要旧结果
    policy = getattr(channel_copy, "bilingual_policy", "zh_first")
    route = lang.route(text_raw, policy=policy,
        zh_min_chars=int(settings.get("BILINGUAL_ZH_MIN_CHARS") or 10)) if settings.get("LANG_ROUTE_ENABLED") else None
    if route and not route.translate_text:
        res = tr.TranslateResult(text_zh=route.keep_text, status="skipped_zh")
    else:
        res = await tr.translate(route.translate_text if route else text_raw, prompt, game, entities_data,
                                 use_cache=bool(payload.get("use_cache")), hits=hits,
                                 bilingual_policy=policy,
                                 **({"theme": theme} if theme != "gaming" else {}))
        if res.status == "ok" and route and route.keep_text:
            res.text_zh = lang.stitch(route, res.text_zh)

    with session_scope() as s:
        row = s.get(MonitorMessage, mid)
        if row is not None:
            if row.text_raw != text_raw:
                return {"ok": False, "status": "stale", "error": "Source content changed"}
            row.text_zh = res.text_zh or None
            row.translate_status = res.status
            row.translate_error = res.error
            row.provider_name = res.provider_name
            row.model = res.model
            row.glossary_hits = res.glossary_hits or None
            row.glossary_miss = res.glossary_miss or None
            row.from_cache = res.from_cache
            row.entities = (entities_data or {}).get("entities") or None
            row.game_detected = det.game
            row.game_scores = det.as_json()
            row.topics = det.topics or None
            row.version_tag = det.version
            s.flush()
            from ..content import record
            record(mid, session=s)
    if settings.get("RETRIEVAL_ENABLED"):
        try:
            from ..retrieval import index_message
            await asyncio.to_thread(index_message, mid)
        except Exception:
            logger.debug("重译后检索索引失败 #%s", mid, exc_info=True)
    return {"ok": res.status == "ok", "status": res.status,
            "error": res.error, "game": det.game}


async def _trial_translate(runner, payload: dict) -> dict:
    """试译：用界面上正在编辑的 prompt 跑一条真实历史消息,结果不入库。"""
    from .. import translate as tr
    from ..kb.annotate import annotate
    mid = int(payload.get("message_id") or 0)
    prompt = (payload.get("prompt") or "").strip()
    game = payload.get("game")
    if not prompt:
        raise ValueError("prompt 为空")
    with session_scope() as s:
        row = s.get(MonitorMessage, mid)
        if row is None:
            raise ValueError("样例消息不存在")
        text_raw = row.text_raw
        if game is None:
            ch = s.get(Channel, row.channel_id)
            game = ch.game if ch else None
    if not text_raw:
        return {"ok": False, "error": "这条没有文本"}

    from ..classify import resolve_game
    game = (await resolve_game(text_raw, game_hint=game)).game
    entities_data = annotate(text_raw, game)
    res = await tr.translate(text_raw, prompt, game, entities_data, use_cache=False)
    return {
        "ok": res.status == "ok", "text_zh": res.text_zh, "error": res.error,
        "glossary_hits": res.glossary_hits, "glossary_miss": res.glossary_miss,
        "provider_name": res.provider_name,
    }


async def _repush(runner, payload: dict) -> dict:
    mid = int(payload.get("message_id") or 0)
    n = await outputs.push_message(mid, force=True)
    # QQ 出站一并重推（force 同样跳过过滤）
    from .. import qqbot
    nqq = 0
    try:
        nqq = await qqbot.push_message(mid, force=True)
    except Exception as e:
        logger.warning("QQ 重推失败（消息 %s）: %s", mid, e)
    return {"ok": True, "delivered": n, "qq_delivered": nqq}


async def _restart_worker(runner, payload: dict) -> dict:
    runner.request_exit("收到重启任务")
    return {"ok": True}


async def _media_cleanup(runner, payload: dict) -> dict:
    from .maintenance import cleanup_media, cleanup_messages
    r1 = cleanup_media()
    # 消息 TTL 也挂这里：后台「立即清理」按钮一键清全部超期数据
    r2 = cleanup_messages()
    return {"media": r1, "messages": r2}


async def _archive_video(runner, payload: dict) -> dict:
    """Archive one video outside the ingestion critical path.

    Only this worker touches the Telethon session. The media row is updated
    after the source has been fetched and the output has been atomically
    written by ``media._archive_video``.
    """
    from .. import media

    media_id = int(payload.get("media_id") or 0)
    channel_id = int(payload.get("channel_id") or 0)
    tg_message_id = int(payload.get("tg_message_id") or 0)
    if not media_id or not channel_id or not tg_message_id:
        raise ValueError("archive_video payload is incomplete")
    if runner.client is None:
        raise RuntimeError("Telegram client is offline")
    with session_scope() as s:
        media_row = s.get(MessageMedia, media_id)
        channel = s.get(Channel, channel_id)
        if media_row is None or channel is None:
            raise ValueError("archive_video source no longer exists")
        if media_row.video_status == "ok" and media_row.video_path:
            return {"ok": True, "status": "already_done", "media_id": media_id}
        out = media.MediaOut(
            kind=media_row.kind, thumb_path=media_row.thumb_path,
            thumb_bytes=media_row.thumb_bytes, width=media_row.width,
            height=media_row.height, duration=media_row.duration,
            orig_bytes=media_row.orig_bytes, mime=media_row.mime,
            phash=media_row.phash, dhash=media_row.dhash,
            has_spoiler=media_row.has_spoiler,
            video_path=media_row.video_path, video_bytes=media_row.video_bytes,
            video_status=media_row.video_status)
        tg_id = channel.tg_id
        max_mb = int(payload.get("video_max_mb") or channel.video_max_mb or 0)
    entity = await runner.client.get_entity(tg_id)
    message = await runner.client.get_messages(entity, ids=tg_message_id)
    if message is None:
        raise RuntimeError("Telegram video message not found")
    await media._archive_video(runner.client, message, channel_id, out, max_mb)
    with session_scope() as s:
        row = s.get(MessageMedia, media_id)
        if row is not None:
            row.video_path, row.video_bytes, row.video_status = (
                out.video_path, out.video_bytes, out.video_status)
    return {"ok": out.video_status == "ok", "status": out.video_status,
            "media_id": media_id}


async def _kb_import(runner, payload: dict) -> dict:
    """拉游戏官方本地化数据进知识库。

    放在 worker 而不是 admin：这是几十秒级的外网请求，admin 是同步 SSR，
    卡在这里等于整个后台无响应。

    hsr 支持带 categories 子集（如只导命途+属性），其他游戏忽略该参数。
    """
    from ..kb.importer import import_genshin, import_hsr, import_zzz
    game = (payload.get("game") or "").strip()
    fn = {"genshin": import_genshin, "hsr": import_hsr,
          "zzz": import_zzz}.get(game)
    if fn is None:
        raise ValueError(f"未知游戏 {game}")
    cats = payload.get("categories")
    if game == "hsr" and isinstance(cats, list) and cats:
        return await fn(categories=[c for c in cats if c])
    return await fn()


async def _kb_smart_sync(runner, payload: dict, task_id: int = 0) -> dict:
    """智能全量同步知识库：串联执行官方源与 Gachabase 补充数据。

    支持 game="all" 或单个游戏 ("genshin", "hsr", "zzz")。
    多阶段回写进度百分比，供前端动态进度条展示。
    """
    from ..kb.importer import import_genshin, import_hsr, import_zzz
    from ..kb.gachabase import import_gachabase

    game = (payload.get("game") or "all").strip().lower()
    steps = []

    if game in ("all", "genshin"):
        steps.append(("原神官方数据 (TextMap)", lambda: import_genshin()))
        steps.append(("原神补充数据 (Gachabase)", lambda: import_gachabase("原神")))

    if game in ("all", "hsr"):
        steps.append(("崩铁官方数据与命途属性", lambda: import_hsr()))
        steps.append(("崩铁补充数据 (Gachabase)", lambda: import_gachabase("崩坏:星穹铁道")))

    if game in ("all", "zzz"):
        steps.append(("绝区零官方角色 (TextMap)", lambda: import_zzz()))
        steps.append(("绝区零音擎与邦布 (Gachabase)", lambda: import_gachabase("绝区零")))

    total_steps = len(steps)
    total_added, total_aliases = 0, 0

    for idx, (title, fn) in enumerate(steps, 1):
        _task_progress(task_id, {
            "done": idx - 1, "total": total_steps,
            "step_name": f"正在同步：{title}",
            "percent": int((idx - 1) / total_steps * 100),
            "added": total_added, "aliases": total_aliases,
        })
        try:
            res = await fn()
            total_added += (res or {}).get("added", 0)
            total_aliases += (res or {}).get("aliases", 0)
        except Exception as e:
            logger.warning("知识库同步阶段 [%s] 出错: %s", title, e)

        _task_progress(task_id, {
            "done": idx, "total": total_steps,
            "step_name": f"已完成：{title}",
            "percent": int(idx / total_steps * 100),
            "added": total_added, "aliases": total_aliases,
        })

    return {"ok": True, "added": total_added, "aliases": total_aliases, "steps": total_steps}


async def _kb_import_gachabase(runner, payload: dict) -> dict:
    """抓 gachabase.net 补齐官方源缺的实体（ZZZ 音擎/邦布/驱动盘等）。

    和 _kb_import 分开而不是合并成一个带 source 参数的任务：这个源是同人站，
    抓的还有未上线的 beta 条目，出问题要能单独按 origin='gachabase' 回滚，
    任务记录也得分得清是哪个源导的。
    """
    from ..kb.gachabase import import_gachabase
    game = (payload.get("game") or "").strip()
    return await import_gachabase(game)


async def _wiki_sync(runner, payload: dict) -> dict:
    from ..kb.wiki import MediaWikiAdapter
    from ..models import KnowledgeSource
    source_id = int(payload.get("source_id") or 0)
    with session_scope() as s:
        src = s.get(KnowledgeSource, source_id)
        if src is None:
            raise ValueError("Wiki source does not exist")
        url, categories = src.url, src.categories or ["characters"]
    return await MediaWikiAdapter(url).sync(source_id, categories)


async def _changelog_poll(runner, payload: dict) -> dict:
    """手动抓一次内鬼站 changelog。定时那条挂在 maintenance_loop 里。

    game 留空则三个游戏都抓。手动触发的意义是「刚配好想立刻看到东西」，
    以及站点改版后不用等 20 分钟就能验证正则还灵不灵。
    """
    from ..kb.changelog import poll, poll_all
    game = (payload.get("game") or "").strip()
    if not game:
        return await poll_all()
    return await poll(game)


from ..unified_jobs import reclassify, knowledge_index

_HANDLERS = {
    "unified_backfill": reclassify,
    "knowledge_index": knowledge_index,
    "send_code": _send_code,
    "sign_in": _sign_in,
    "sign_in_2fa": _sign_in_2fa,
    "logout": _logout,
    "sync_dialogs": _sync_dialogs,
    "test_provider": _test_provider,
    "retranslate": _retranslate,
    "archive_video": _archive_video,
    "trial_translate": _trial_translate,
    "repush": _repush,
    "restart_worker": _restart_worker,
    "media_cleanup": _media_cleanup,
    "backfill": _backfill,
    "catchup_now": _catchup_now,
    "kb_smart_sync": _kb_smart_sync,
    "kb_import": _kb_import,
    "kb_import_gachabase": _kb_import_gachabase,
    "wiki_sync": _wiki_sync,
    "changelog_poll": _changelog_poll,
}


def claim_next() -> tuple[int, str, dict] | None:
    """取一条 pending 任务并标记 running。"""
    with session_scope() as s:
        from sqlalchemy import select, update
        candidate = select(Task.id).where(Task.status == "pending").order_by(Task.id).limit(1).scalar_subquery()
        row = s.execute(update(Task).where(Task.id == candidate).values(
            status="running", started_at=datetime.utcnow()).returning(
            Task.id, Task.kind, Task.payload)).first()
        if row is None:
            return None
        return row.id, row.kind, dict(row.payload or {})


def finish(task_id: int, result: dict | None, error: str | None) -> None:
    with session_scope() as s:
        row = s.get(Task, task_id)
        if row is None:
            return
        row.status = "failed" if error else "done"
        row.result = result
        row.error = error
        row.finished_at = datetime.utcnow()
