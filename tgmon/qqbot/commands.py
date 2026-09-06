"""QQ 群内命令与 AI 对话 —— 事件进入 tgmon 后的业务处理层。

事件从两条路进来（webhook /qqbot/callback 与 botpy WS 桥
/qqbot/bridge），都汇到 qqbot.handle_callback_event()，群 @ 与
C2C 消息再进本模块。

命令集（content 清洗后按前缀匹配，中文别名跟在后面）：
  /help            命令列表
  /status          运行状态（桥在线 / 群数 / 今日推送）
  /latest [n]      最新 n 条爆料（默认 3，数字可到 5，按发布时间排序）
  /new             未读增量（只返回本群上次拉取之后的新消息）
  /search <关键词>  模糊搜索，最近 3 条命中
  /game <名称> [n]  按游戏过滤最新
  /peek <消息id>    单条完整内容 + 全部图片
  /video <消息id>   发送该消息的归档视频
  /stats           今日统计（消息数 / 游戏分布 / 频道数）
  /on  /off        本群推送开关（仅群内）
  /ai <问题>       显式 AI 问答
  /translate 文本  按已审核知识库翻译；可指定 [游戏] 或 #消息id
  非命令文本       QQ_AI_FALLBACK 开（默认）时走 agent 意图路由

回复结构：命令函数返回 list[Reply]（文本/图片/视频），由发送层
按 msg_seq 递增逐条发出。被动回复每条 @ 消息限 5 次，_clip() 兜底。

排序纪律：查「最新」一律 order_by(published_at.desc(), id.desc()) ——
补拉历史消息的 id 与发布时间顺序不一致（2026-09-04 的 /latest bug）。

事务纪律：AI 调用是网络 IO，一律在 session 外做（db.py 的老规矩）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func

from .. import settings
from ..db import session_scope
from ..models import (Channel, MessageMedia, MonitorMessage, QqDelivery,
                      QqGroup, QqInbound)
from ..util import log_event

logger = logging.getLogger(__name__)

# 与 push_message 的 _format_text 同一保守上限（平台超限报 40054007）
MAX_TEXT = 1800
# 被动回复每条 @ 消息最多 5 次（平台限制），长图查询会把多条内容合并到卡片内
MAX_REPLIES = 5
LATEST_MAX = 5

DEFAULT_AI_SYSTEM = (
    "你是 tgmon 群助手，一个游戏资讯推送机器人的对话人格。"
    "回答简洁口语化，用简体中文；可以聊游戏和日常，"
    "涉及具体资讯拿不准时明说不确定。回复尽量控制在 300 字以内。"
)

HELP_TEXT = (
    "我是 tgmon 机器人，可以这样吩咐我：\n"
    "/help - 本帮助\n"
    "/latest 3 - 最新 3 条爆料（带图，数字可改 1-5）\n"
    "/new - 只看本群还没看过的新消息\n"
    "/search 关键词 - 搜索爆料\n"
    "/ask 问题 - 根据爆料回答（带引用）\n"
    "/translate [崩铁] 原文 - 按知识库翻译（或 /translate #消息id）\n"
    "/related 消息id - 相关消息\n"
    "/timeline 关键词 - 消息时间线\n"
    "/game 原神 - 按游戏看最新\n"
    "/peek 消息id - 看某条完整内容和全部图片\n"
    "/video 消息id - 领取该条的视频\n"
    "/stats - 今日统计\n"
    "/status - 运行状态\n"
    "/on /off - 本群推送开关\n"
    "/ai 问题 - AI 问答\n"
    "/remember 事实 /forget 关键词 - 记住或忘记个人信息\n"
    "/nickname 昵称 - 设置我对你的称呼（或直接说\"叫我XX\"）\n"
    "直接 @ 我说人话也行（比如\"今天有什么新爆料\"）。"
)

# 显式 @ 片段（部分客户端会带在 content 里）
_AT_TAG_RE = re.compile(r"<@!?\S+>\s*")


@dataclass
class Reply:
    """一条待发回复。kind: text / image / video。"""
    kind: str = "text"
    text: str = ""
    thumb_path: str = ""   # kind=image：media/ 相对路径
    mid: int = 0           # kind=video：消息 id（签名 URL 用）
    thumb_paths: list[str] = field(default_factory=list)  # 同条消息的整组图片
    story_ids: list[int] = field(default_factory=list)
    bundle_id: int = 0
    memory_skip: bool = False


def replies_text(replies: list[Reply]) -> str:
    """回复列表 → 留痕用纯文本（媒体记占位符）。"""
    parts = []
    for r in replies:
        if r.text:
            parts.append(r.text)
        if r.kind == "image":
            parts.append(f"[图片×{len(r.thumb_paths)}]" if len(r.thumb_paths) > 1 else "[图片]")
        elif r.kind == "video":
            parts.append("[视频]")
    return "\n".join(p for p in parts if p)


def _clip(replies: list[Reply]) -> list[Reply]:
    """被动回复 5 次/条的限制兜底：超限时砍尾并提示。"""
    if len(replies) <= MAX_REPLIES:
        return replies
    kept = replies[:MAX_REPLIES - 1]
    kept.append(Reply(kind="text", text="（内容太多被截断，缩小范围或用 "
                                        "/peek 消息id 看单条）"))
    return kept


def _clean_content(raw: str) -> str:
    """清洗群 @ 消息 content：去显式 @ 片段、前导空格与裸 @ 前缀。"""
    t = (raw or "").strip()
    t = _AT_TAG_RE.sub("", t).strip()
    if t.startswith("@"):
        t = t[1:].lstrip()
    return t.strip()


def _record_conversation(group: str, member: str, content: str,
                          replies: list[Reply], event_id: str = "") -> None:
    """Record short-term turns without copying explicit private facts to a group."""
    if any(r.memory_skip for r in replies) or (content or "").lower().startswith(("/remember", "/forget", "/memory", "/nickname", "记住", "忘记", "叫我")):
        return
    try:
        from ..memory import record_turn
        from ..member_profile import display_name
        # 说话人昵称快照落库：注入时模型直接读到可读名，免二次查表
        speaker = display_name(member) if member else ""
        record_turn(group, member, "user", content, event_id=event_id, speaker=speaker)
        for i, reply in enumerate(replies):
            if reply.text:
                record_turn(group, member, "assistant", reply.text, event_id=event_id, ordinal=i)
    except Exception:
        logger.debug("记录 QQ 记忆失败", exc_info=True)


def _bridge_online() -> bool:
    last_seen = str(settings.get("QQ_BRIDGE_LAST_SEEN") or "")
    if not last_seen:
        return False
    try:
        delta = (datetime.utcnow() - datetime.fromisoformat(last_seen)) \
            .total_seconds()
        return 0 <= delta < 180
    except ValueError:
        return False


# ---------------- 查询辅助 ----------------

def _query_latest(n: int, game: str | None = None) -> list[dict]:
    """按发布时间倒序取 n 条。返回 [{...}] 的普通 dict（session 外可用）。"""
    from ..message_query import latest
    return latest(n, game)


_CN_COUNTS = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_COUNT = r"([0-9]{1,3}|[一二两三四五六七八九十])"
_COUNT_WITH_UNIT = re.compile(r"(?<![\d.])" + _COUNT + r"\s*(?:条|篇|则|个)")


def _requested_count(content: str, default: int | None = None) -> int | None:
    """Parse explicit counts; the caller applies the story limit."""
    text = unicodedata.normalize("NFKC", content or "").strip()
    match = _COUNT_WITH_UNIT.search(text)
    if not match and re.match(r"^(?:/(?:latest|game|游戏)|最新|最近)\b", text, re.I):
        match = re.search(r"\s" + _COUNT + r"$", text)
    if match:
        value = match.group(1)
        return int(value) if value.isdigit() else _CN_COUNTS[value]
    return default


def _natural_latest_request(content: str) -> tuple[str, int] | None:
    """Recognize complete latest-story requests; leave substantive questions to AI."""
    from ..glossary import _GAME_ALIASES
    folded = re.sub(r"\s+", "", unicodedata.normalize("NFKC", content).casefold())
    if folded.startswith("/") or not re.search(r"最新|最近|近期|爆料|消息|资讯", folded):
        return None
    aliases = {unicodedata.normalize("NFKC", key): value for key, value in _GAME_ALIASES.items()}
    aliases.update({game: game for game in _GAME_ALIASES.values()})
    pattern = "|".join((r"(?<![a-z])" + re.escape(key) + r"(?![a-z])") if key.isascii()
                       else re.escape(key) for key in sorted(aliases, key=len, reverse=True))
    games = set()

    def remove_game(match):
        games.add(aliases[match.group(0)])
        return ""

    remaining = re.sub(pattern, remove_game, folded)
    remaining = _COUNT_WITH_UNIT.sub("", remaining)
    remaining = re.sub(
        r"发给我|给我发|给我|帮我|麻烦|请|能不能|可以|发送|推送|发|来|看看|看一下|看下|看|"
        r"查询|查一下|查|一下|最新|最近|近期|爆料|消息|资讯|内容|有什么|有哪些|"
        r"全部游戏|所有游戏|全部|所有|今天|现在|的|吗|呢|吧|新|有", "", remaining)
    remaining = re.sub(r"[,。!?：:、\[\]【】]", "", remaining)
    if remaining or len(games) > 1:
        return None
    default = int(settings.get("QQ_LATEST_DEFAULT_N") or 3)
    count = _requested_count(content, default)
    return next(iter(games), ""), max(1, min(count, LATEST_MAX))




def _advance_cursor(openid: str, rows: list[dict]) -> None:
    """/latest 与 /new 拉取后推进该群的已读游标。"""
    if not rows or not openid:
        return
    top = max(rows, key=lambda r: (r["published_at"] or datetime.min, r["id"]))["id"]
    with session_scope() as s:
        g = s.query(QqGroup).filter(QqGroup.group_openid == openid).first()
        if g is not None:
            g.last_seen_msg_id = top


def mark_read(openid: str, mids: list[int]) -> None:
    if not openid or not mids:
        return
    from ..message_query import by_ids
    rows = by_ids(mids)
    with session_scope() as s:
        group = s.query(QqGroup).filter_by(group_openid=openid).first()
        if not group:
            return
        cursors = dict(group.cursors or {})
        for row in rows:
            value = {"published_at": (row["published_at"] or datetime.min).isoformat(), "id": row["id"]}
            previous = cursors.get(row["game"], {})
            if (value["published_at"], value["id"]) > (previous.get("published_at", ""), previous.get("id", 0)):
                cursors[row["game"]] = value
        group.cursors = cursors
    _advance_cursor(openid, rows)


def _fmt_row(r: dict, with_id: bool = True) -> str:
    """卡片文本。纯图爆料只留标题行（图片随后单独发），
    不再放「（无文本）」占位（2026-09 群反馈）。"""
    head = f"【{r['game']}】" if r["game"] else f"【{r['title']}】"
    text = r["text"] or ""
    if r["has_spoiler"]:
        text = f"⚠ 含剧透\n{text}" if text else "⚠ 含剧透"
    if len(text) > MAX_TEXT - 40:
        text = text[:MAX_TEXT - 40] + "…"
    tag = f" #{r['id']}" if with_id else ""
    return f"{head}{tag}\n{text}".strip()


def _row_reply(row: dict) -> Reply:
    """Keep a message's caption and complete album in one QQ media reply."""
    photos = row.get("photos") or []
    if photos:
        return Reply(kind="image", text=_fmt_row(row), thumb_path=photos[0],
                     thumb_paths=list(photos), story_ids=[row["id"]])
    return Reply(text=_fmt_row(row), story_ids=[row["id"]])


# ---------------- 命令实现 ----------------

def _cmd_status() -> list[Reply]:
    today = datetime.now().strftime("%Y-%m-%d")
    with session_scope() as s:
        total = s.query(QqGroup).count()
        enabled = s.query(QqGroup).filter(QqGroup.enabled.is_(True)).count()
        sent = (s.query(QqGroup)
                .filter(QqGroup.sent_date == today)
                .with_entities(QqGroup.sent_today).all())
        sent_today = sum(x[0] or 0 for x in sent)
    state = "在线" if _bridge_online() else "离线（事件通道异常）"
    return [Reply(text=(f"tgmon 状态\n· 事件通道：{state}\n"
                        f"· 接入群：{total}（推送中 {enabled}）\n"
                        f"· 今日已推送：{sent_today} 条"))]


def _cmd_latest(content: str, openid: str) -> list[Reply]:
    n = int(settings.get("QQ_LATEST_DEFAULT_N") or 3)
    n = _requested_count(content, n) or n
    n = max(1, min(n, LATEST_MAX))
    rows = _query_latest(n)
    if not rows:
        return [Reply(text="还没有收录任何消息。")]
    out: list[Reply] = []
    for i, r in enumerate(rows, 1):
        out.append(_row_reply(r))
    return _clip(out)


def _cmd_new(openid: str) -> list[Reply]:
    """未读增量：比该群游标（上次拉取的最新一条）更新 的消息。"""
    from sqlalchemy import and_, or_
    from ..message_query import query, effective_game, project
    with session_scope() as s:
        group = s.query(QqGroup).filter_by(group_openid=openid).first()
        q = query(s)
        cursor_filters = []
        for game, cursor in ((group.cursors if group else {}) or {}).items():
            try:
                stamp = datetime.fromisoformat(cursor["published_at"])
                mid = int(cursor["id"])
            except (ValueError, KeyError, TypeError):
                continue
            current_game = func.coalesce(effective_game(), "")
            cursor_filters.append(and_(current_game == game, or_(
                MonitorMessage.published_at > stamp,
                and_(MonitorMessage.published_at == stamp, MonitorMessage.id > mid))))
        if cursor_filters:
            current_game = func.coalesce(effective_game(), "")
            tracked = tuple((group.cursors if group else {}).keys())
            # A newly subscribed or newly classified game has no cursor yet;
            # include it as unread instead of letting one existing game's
            # cursor suppress it from the OR branch.
            q = q.filter(or_(*cursor_filters, ~current_game.in_(tracked)))
        if group and group.games:
            q = q.filter(effective_game().in_(group.games))
        rows = [project(s, m) for m in q.order_by(MonitorMessage.published_at.desc(),
                                                MonitorMessage.id.desc()).limit(LATEST_MAX)]
    return [_row_reply(row) for row in rows] or [Reply(text="没有新消息。")]




def _query_by_id(mid: int) -> dict | None:
    from ..message_query import by_ids
    rows = by_ids([mid])
    return rows[0] if rows else None


def _cmd_search(content: str) -> list[Reply]:
    parts = content.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return [Reply(text="用法：/search 关键词")]
    kw = parts[1].strip()[:40]
    from ..retrieval import search
    hits = [{"id": h.ref_id, "title": h.title, "game": h.game or "",
            "text": h.text.strip()} for h in search(kw, limit=20)
            if h.ref_type == "message"][:3]
    if not hits:
        return [Reply(text=f"没搜到含「{kw}」的消息。")]
    out = [Reply(text=f"「{kw}」{len(hits)} 条命中：")]
    for h in hits:
        t = h["text"].replace("\n", " ")[:100]
        out.append(Reply(text=f"【{h['game'] or h['title']}】#{h['id']} {t}"))
    out.append(Reply(text="看详情：/peek 消息id"))
    return _clip(out)


def _cmd_related(content: str) -> list[Reply]:
    parts = content.split()
    if len(parts) < 2:
        return [Reply(text="用法：/related 消息id")]
    try:
        mid = int(parts[1])
    except ValueError:
        return [Reply(text="消息 id 要是数字。")]
    from ..retrieval import related
    hits = related(mid, limit=5)
    if not hits:
        return [Reply(text=f"#{mid} 暂无相关消息。")]
    out = [Reply(text=f"与 #{mid} 相关的消息：")]
    out += [Reply(text=f"【消息#{h.ref_id}】{h.text.replace(chr(10), ' ')[:120]}") for h in hits]
    return _clip(out)


def _cmd_timeline(content: str) -> list[Reply]:
    parts = content.split(maxsplit=1)
    if len(parts) < 2:
        return [Reply(text="用法：/timeline 关键词")]
    from ..retrieval import search
    keyword = parts[1].strip()
    hits = [h for h in search(keyword, limit=50) if h.ref_type == "message"]
    if not hits:
        return [Reply(text=f"没搜到含「{keyword}」的消息。")]
    groups: dict[str, list] = {}
    for h in hits:
        day = h.published_at.strftime("%Y-%m-%d") if h.published_at else "未知日期"
        key = f"{day} · v{h.version}" if h.version else day
        groups.setdefault(key, []).append(h)
    lines = [f"「{keyword}」时间线："]
    for day, hs in groups.items():
        lines.append(f"{day}（{len(hs)} 条）")
        lines.extend(f"· #{h.ref_id} {h.text.replace(chr(10), ' ')[:100]}" for h in hs[:5])
    return [Reply(text="\n".join(lines)[:MAX_TEXT])]


async def _cmd_ask(question: str, group_openid: str, member_openid: str) -> list[Reply]:
    from ..retrieval import build_context, search, SearchHit
    from .. import memory
    from ..message_query import by_ids
    ctx = memory.load_context(group_openid, member_openid)
    state = ctx.get("state") or {}
    hits = search(question, game=state.get("game") or None, limit=8)
    if any(word in question for word in ("她", "他", "它", "这条", "上一条", "继续")):
        for row in by_ids(state.get("last_message_ids") or []):
            if not any(h.ref_type == "message" and h.ref_id == row["id"] for h in hits):
                hits.insert(0, SearchHit("message", row["id"], 1, row["text"], row["title"],
                    row["title"], row["game"], row["published_at"], row["deeplink"], row["version"]))
    if not hits:
        return [Reply(text="我没有找到可靠的资料，暂时不能确定。")]
    from ..providers import registry
    from ..translate import budget_blocked, record_usage, _cost_for
    if budget_blocked():
        return [Reply(text="今日 AI 额度已用完。")]
    system = ("根据提供的资料回答问题，简体中文，简洁。区分测试服爆料和已确认资料。"
              "每个事实标注资料中的【消息#id】或【Wiki#id】引用。无证据则说明不确定。"
              "按发言人理解近期对话，不把其他人的偏好当成当前用户的偏好。")
    res = await registry.complete_with_failover(system,
        "问题：" + question + "\n" + memory.build_chat_context(group_openid, member_openid)
        + "\n证据：\n" + build_context(hits))
    if not res.ok:
        return [Reply(text="AI 暂时不可用，稍后再试。")]
    record_usage(res.provider_name, res.tokens_in, res.tokens_out,
                 _cost_for(res.provider_name, res.tokens_in, res.tokens_out))
    answer = res.text.strip()[:MAX_TEXT]
    allowed = {("消息" if h.ref_type == "message" else "Wiki", str(h.ref_id)) for h in hits}
    cited = set(re.findall(r"【(消息|Wiki)#(\d+)】", answer))
    if cited - allowed:
        answer = "以下是查到的资料：\n" + build_context(hits[:3], max_chars=MAX_TEXT - 30)
    elif not cited:
        answer += "\n" + " ".join(f"【{kind}#{mid}】" for kind, mid in sorted(allowed)[:3])
    return [Reply(text=answer)]


def _cmd_game(content: str) -> list[Reply]:
    parts = content.split()
    if len(parts) < 2:
        return [Reply(text="用法：/game 游戏名 [条数]")]
    name = " ".join(parts[1:])
    name = re.sub(r"\s+" + _COUNT + r"(?:\s*(?:条|篇|则|个))?$", "", name).strip()
    n = _requested_count(content, 3) or 3
    n = max(1, min(n, LATEST_MAX))
    rows = _query_latest(n, game=name)
    if not rows:
        # 游戏名可能是别名（如"绝区零"vs"ZZZ"），做一次模糊
        like = f"%{name}%"
        with session_scope() as s:
            got = (s.query(MonitorMessage.game_detected)
                   .filter(MonitorMessage.game_detected.like(like))
                   .distinct().limit(5).all())
            names = [x[0] for x in got if x[0]]
        if names:
            return [Reply(text=f"没有「{name}」的记录。相近的游戏有："
                            + "、".join(names)
                            + "，换个名字再试。")]
        return [Reply(text=f"没有「{name}」的记录。")]
    # The outer dispatcher turns story replies into one immutable long-card
    # bundle. A heading here would consume one of the five reply slots.
    return _clip([_row_reply(r) for r in rows])


def _cmd_peek(content: str) -> list[Reply]:
    parts = content.split()
    if len(parts) < 2:
        return [Reply(text="用法：/peek 消息id（id 在 /latest /search 结果里）")]
    try:
        mid = int(parts[1])
    except ValueError:
        return [Reply(text="消息 id 要是数字，在 /latest 结果的 #后面。")]
    r = _query_by_id(mid)
    if r is None:
        return [Reply(text=f"#{mid} 不存在（可能已被清理）。")]
    out = [_row_reply(r)]
    if r["video_mid"]:
        out.append(Reply(text=f"视频：/video {r['video_mid']}，"
                              f"或去 TG 看 {r['deeplink']}"))
    return _clip(out)


async def _cmd_video(content: str) -> list[Reply]:
    """发送归档视频（签名 URL 上传 + msg_type=7）。未归档给 TG 链接。"""
    parts = content.split()
    if len(parts) < 2:
        return [Reply(text="用法：/video 消息id")]
    try:
        mid = int(parts[1])
    except ValueError:
        return [Reply(text="消息 id 要是数字。")]
    with session_scope() as s:
        row = (s.query(MessageMedia)
               .filter(MessageMedia.message_id == mid,
                       MessageMedia.kind == "video").first())
        if row is None:
            return [Reply(text=f"#{mid} 不是视频消息（或已清理）。")]
        has_video = bool(row.video_path)
        deeplink = row.message.deeplink if row.message else ""
    if not has_video:
        return [Reply(text=f"#{mid} 的视频还没归档（老消息或超时长上限）。"
                          f"去 TG 看原片：{deeplink}")]
    return [Reply(kind="video", mid=mid)]


def _cmd_stats() -> list[Reply]:
    today = datetime.now().strftime("%Y-%m-%d")
    since = datetime.utcnow() - timedelta(hours=24)
    with session_scope() as s:
        total = (s.query(MonitorMessage)
                 .filter(MonitorMessage.published_at >= since).count())
        games = (s.query(MonitorMessage.game_detected, func.count())
                 .filter(MonitorMessage.published_at >= since,
                         MonitorMessage.game_detected.isnot(None))
                 .group_by(MonitorMessage.game_detected)
                 .order_by(func.count().desc())
                 .limit(3).all())
        channels = s.query(Channel).filter(Channel.enabled.is_(True)).count()
    gs = "、".join(f"{g}({n})" for g, n in games if g) or "暂无"
    return [Reply(text=(f"最近 24 小时：{total} 条消息\n"
                        f"游戏分布：{gs}\n"
                        f"监控频道：{channels} 个"))]


def _cmd_toggle(openid: str, on: bool) -> list[Reply]:
    with session_scope() as s:
        g = s.query(QqGroup).filter(QqGroup.group_openid == openid).first()
        if g is None:
            return [Reply(text="本群还没登记，先 @ 我发任意消息激活一下再试。")]
        g.enabled = on
        if on:
            g.dropped_at = None
    if on:
        return [Reply(text="本群推送已开启，新爆料会自动发到这里。")]
    return [Reply(text="本群推送已关闭，@ 我发 /on 可重新开启。")]


async def _ai(question: str, group: str = "", member: str = "") -> str:
    if not settings.get("QQ_AI_ENABLED"):
        return "AI 功能当前关闭。"
    system = str(settings.get("QQ_AI_SYSTEM") or "").strip() or DEFAULT_AI_SYSTEM
    from ..providers import registry
    from .. import memory
    ctx = memory.build_chat_context(group, member)
    res = await registry.complete_with_failover(system,
        (ctx + "\n" if ctx else "") + "当前消息：" + question)
    if not res.ok:
        log_event("warning", "qqbot", f"群内 AI 调用失败: {res.error}")
        return "AI 暂时不可用，稍后再试。"
    return res.text.strip() or "（AI 没说话）"


async def _cmd_translate(text: str, game: str | None = None) -> list[Reply]:
    """Translate explicit text or a stored message's original, without a channel."""
    if not settings.get("QQ_AI_ENABLED"):
        return [Reply(text="AI 功能当前关闭。")]
    text = (text or "").strip()
    scoped = re.match(r"^[\[【]([^\]】]+)[\]】]\s*", text)
    if scoped:
        from ..glossary import normalize_game
        candidate = normalize_game(scoped[1])
        if candidate in ("原神", "崩坏:星穹铁道", "绝区零", "崩坏3"):
            game, text = candidate, text[scoped.end():].strip()
    if not text:
        return [Reply(text="用法：/translate [游戏名] 原文，或 /translate #消息id；也可以用 /tr、翻译：原文。")]
    if re.fullmatch(r"#\d+", text):
        mid = int(text[1:])
        with session_scope() as s:
            pair = (s.query(MonitorMessage, Channel)
                    .join(Channel, Channel.id == MonitorMessage.channel_id)
                    .filter(MonitorMessage.id == mid).first())
            if pair is None:
                return [Reply(text=f"#{mid} 不存在（可能已被清理）。")]
            message, channel = pair
            text = (message.text_raw or "").strip()
            game = game or message.game_detected or channel.game
        if not text:
            return [Reply(text=f"#{mid} 没有可翻译的正文。")]
    if len(text) > 6000:
        return [Reply(text="一次最多翻译 6000 字，请把原文分成几段发送。")]
    from ..translate import translate_with_kb
    result = await translate_with_kb(text, game)
    if result.status == "disabled":
        return [Reply(text="翻译功能当前关闭。")]
    if result.status == "budget":
        return [Reply(text="翻译暂时不可用：" + str(result.error or "今日额度已用完。"))]
    if result.status != "ok" or not result.text_zh.strip():
        return [Reply(text="翻译暂时不可用，稍后再试。")]
    output = result.text_zh.strip()
    unresolved = [name for name in result.glossary_miss if name not in result.glossary_corrected]
    if unresolved:
        output += "\n（部分术语未采用知识库译法：" + "、".join(unresolved[:5]) + "）"
    replies = [Reply(text=output[start:start + MAX_TEXT]) for start in range(0, len(output), MAX_TEXT)]
    return _clip(replies)


async def _dispatch_raw(content: str, openid: str, in_group: bool,
                    member_openid: str = "") -> list[Reply]:
    """命令路由。openid 空串 = C2C 私聊（无群概念）。"""
    if not content:
        return [Reply(text=HELP_TEXT)]
    low = content.lower()
    if low in ("/help", "/帮助", "help", "帮助"):
        return [Reply(text=HELP_TEXT)]
    if low in ("/status", "/状态", "状态"):
        return _cmd_status()
    if low in ("/stats", "/统计", "统计"):
        return _cmd_stats()
    translate_cmd = re.match(r"^(?:/translate|/tr|翻译)(?=$|\s|[:：\[【\"“])", content, re.I)
    if translate_cmd:
        return await _cmd_translate(content[translate_cmd.end():].lstrip(" \t\r\n:："))
    if low.startswith("/latest") or re.fullmatch(r"最新(?:\s+[1-5])?", low):
        return _cmd_latest(content, openid)
    if low in ("/new", "/未读", "未读"):
        return _cmd_new(openid) if in_group and openid \
            else [Reply(text="这条命令要在群里用。")]
    if low.startswith("/search") or low.startswith("搜索"):
        return _cmd_search(content)
    if low.startswith("/related") or low.startswith("相关"):
        return _cmd_related(content)
    if low.startswith("/timeline") or low.startswith("时间线"):
        return _cmd_timeline(content)
    if low.startswith("/ask") or low.startswith("问"):
        parts = content.split(maxsplit=1)
        return await _cmd_ask(parts[1].strip() if len(parts) > 1 else "", openid, member_openid)
    if low.startswith("/remember") or low.startswith("记住"):
        fact = re.sub(r"^(?:/remember|记住)\s*", "", content, flags=re.I).strip()
        from ..member_profile import add_fact
        return [Reply(text="已记住。" if add_fact(member_openid, fact) else "用法：/remember 你希望我记住的事实", memory_skip=True)]
    if low.startswith("/forget") or low.startswith("忘记"):
        q = re.sub(r"^(?:/forget|忘记)\s*", "", content, flags=re.I).strip() or None
        if q in ("全部", "所有", "所有记忆"):
            q = None
        from ..member_profile import forget_fact
        from ..memory import forget as forget_legacy
        n = forget_fact(member_openid, q) + forget_legacy("user", member_openid, q, group=openid)
        return [Reply(text=f"已删除 {n} 条记忆。", memory_skip=True)]
    if low.startswith("/nickname") or low.startswith("叫我"):
        name = re.sub(r"^(?:/nickname|叫我)\s*", "", content, flags=re.I).strip()
        from ..member_profile import set_nickname
        if name:
            set_nickname(member_openid, name)
            return [Reply(text=f"好的，以后叫你{name}。", memory_skip=True)]
        return [Reply(text="用法：/nickname 昵称（或直接对我说“叫我XX”）", memory_skip=True)]
    if low.startswith("/game") or low.startswith("/游戏"):
        return _cmd_game(content)
    if low.startswith("/peek") or low.startswith("查看"):
        return _cmd_peek(content)
    if low.startswith("/video") or low.startswith("视频"):
        return await _cmd_video(content)
    if low in ("/on", "/开", "开"):
        return _cmd_toggle(openid, True) if in_group and openid \
            else [Reply(text="这条命令要在群里用。")]
    if low in ("/off", "/关", "关"):
        return _cmd_toggle(openid, False) if in_group and openid \
            else [Reply(text="这条命令要在群里用。")]
    if low.startswith("/ai"):
        q = content[3:].strip()
        return [Reply(text=await _ai(q, openid, member_openid) if q else "用法：/ai <问题>")]
    if low.startswith("/"):
        name = content.split()[0]
        return [Reply(text=f"不认识的命令 {name}，/help 看列表。")]
    # 非命令 → agent 意图路由（可配置退回纯 AI 聊天）
    if settings.get("QQ_AI_ENABLED") and settings.get("QQ_AI_FALLBACK"):
        if settings.get("QQ_AI_AGENT_ENABLED"):
            from . import agent
            return await agent.handle(content, openid, in_group, member_openid)
        return [Reply(text=await _ai(content, openid, member_openid))]
    return [Reply(text=HELP_TEXT)]


async def _dispatch(content: str, openid: str, in_group: bool,
                    member_openid: str = "") -> list[Reply]:
    from .. import memory
    from ..glossary import normalize_game
    from ..message_query import by_ids
    ctx = memory.load_context(openid, member_openid)
    if content.strip() in ("/memory", "我的记忆", "查看记忆"):
        from ..member_profile import get_profile
        prof = get_profile(member_openid)
        lines = ([f"称呼：{prof['nickname']}"] if prof["nickname"] else [])
        seen = set(prof["facts"])
        lines += [f"· {x}" for x in prof["facts"]]
        # 旧版 conversation.facts 的存量数据也展示（迁移前保存的）
        lines += [f"· {x}" for x in ctx.get("facts", {}) if x not in seen]
        return [Reply(text="\n".join(lines) or "还没有保存的个人记忆。", memory_skip=True)]
    if content.strip().startswith("修改记忆"):
        parts = content[len("修改记忆"):].strip().split("=>", 1)
        if len(parts) != 2 or not all(x.strip() for x in parts):
            return [Reply(text="请给出旧内容和新内容，用 => 分开。", memory_skip=True)]
        from ..member_profile import add_fact, forget_fact
        forget_fact(member_openid, parts[0].strip())
        memory.forget("user", member_openid, parts[0].strip(), group=openid)
        add_fact(member_openid, parts[1].strip())
        return [Reply(text="记忆已更新。", memory_skip=True)]
    if content.strip().startswith(("/subscribe", "订阅")) and in_group:
        name = re.sub(r"^(?:/subscribe|订阅)\s*", "", content).strip()
        games = [normalize_game(x) for x in re.split(r"[,，、\s]+", name) if x]
        if name in ("全部", "all"):
            games = []
        elif not games or any(x not in ("原神", "崩坏:星穹铁道", "绝区零", "崩坏3") for x in games):
            return [Reply(text="请指定原神、崩铁、绝区零或全部。")]
        with session_scope() as s:
            group = s.query(QqGroup).filter_by(group_openid=openid).first()
            if group:
                group.games = games
        return [Reply(text="已订阅：" + ("、".join(games) or "全部游戏"))]
    text_mode = "文字版" in content or "用文字" in content
    state = ctx.get("state") or {}
    if text_mode and state.get("last_message_ids") and re.fullmatch(r"(?:改成|换成|用|发|给我)?文字版[。！!]?", content.strip()):
        from ..message_query import by_ids
        return [Reply(text=_fmt_row(row)) for row in by_ids(state["last_message_ids"])][:MAX_REPLIES]
    if content.strip() in ("上一条", "上一条爆料") and state.get("last_message_ids"):
        replies = [_row_reply(row) for row in by_ids(state["last_message_ids"][:1])]
    elif re.fullmatch(r"(?:继续|接着看|再来)\s*(?:[1-5一二两三四五]\s*条)?[。！!]?", content.strip()) and state.get("last_message_ids"):
        from ..message_query import query, project, by_ids
        previous = by_ids(state["last_message_ids"])
        last = min(previous, key=lambda r: (r["published_at"] or datetime.min, r["id"])) if previous else None
        with session_scope() as s:
            q = query(s, game=state.get("game") or "")
            if last:
                from sqlalchemy import or_, and_
                q = q.filter(or_(MonitorMessage.published_at < last["published_at"],
                    and_(MonitorMessage.published_at == last["published_at"], MonitorMessage.id < last["id"])))
            count = max(1, min(_requested_count(content, state.get("last_query_count") or 3), LATEST_MAX))
            replies = [_row_reply(project(s, m)) for m in q.order_by(
                MonitorMessage.published_at.desc(), MonitorMessage.id.desc()).limit(count)] or [Reply(text="没有更早的消息了。")]
    else:
        cleaned = content.replace("文字版", "").replace("用文字", "").strip()
        latest_request = _natural_latest_request(cleaned)
        if latest_request:
            inferred, count = latest_request
            replies = (_cmd_game(f"/game {inferred} {count}") if inferred
                       else _cmd_latest(f"/latest {count}", openid))
        else:
            replies = await _dispatch_raw(cleaned if text_mode else content, openid, in_group, member_openid)
    mids = list(dict.fromkeys(mid for r in replies for mid in r.story_ids))
    if not mids:
        return replies
    from ..message_query import by_ids
    rows = by_ids(mids)
    games = list(dict.fromkeys(r["game"] for r in rows if r["game"]))
    memory.update_state(openid, member_openid, last_message_ids=mids,
                        last_query_count=len(mids),
                        game=games[0] if len(games) == 1 else "")
    if text_mode:
        return [Reply(text=_fmt_row(row), story_ids=[row["id"]]) for row in rows][:MAX_REPLIES]
    from ..sharing import create_bundle, bundle_cards
    sid = 0
    try:
        sid = await asyncio.to_thread(create_bundle, mids)
        paths, url, _items = await asyncio.to_thread(bundle_cards, sid)
        out = [Reply(kind="image", thumb_path=path, text=url if i == 0 else "",
                     bundle_id=sid, story_ids=mids) for i, path in enumerate(paths[:4])]
        if len(paths) > 4:
            out.append(Reply(text=url, bundle_id=sid, story_ids=mids))
        return out
    except Exception as exc:
        logger.warning("生成爆料长图失败: %s", exc)
        if sid:
            from ..sharing import bundle_url
            from ..models import ShareToken
            with session_scope() as s:
                try:
                    return [Reply(text=bundle_url(s.get(ShareToken, sid)), bundle_id=sid, story_ids=mids)]
                except ValueError:
                    pass
        return [Reply(text="长图暂时无法生成，请稍后重试。")]


def _record_inbound(event_type: str, group_openid: str, member_openid: str,
                    content: str, msg_id: str, reply_text: str | None) -> None:
    """入站留痕。失败只记日志 —— 留痕挂了不该影响回复。"""
    try:
        with session_scope() as s:
            s.add(QqInbound(event_type=event_type,
                            group_openid=group_openid or "",
                            member_openid=member_openid or "",
                            content=(content or "")[:2000],
                            msg_id=msg_id or None,
                            reply=(reply_text or "")[:2000] or None))
    except Exception as e:
        logger.warning("入站留痕失败: %s", e)


async def handle_group_message(d: dict) -> tuple[list[Reply], str]:
    """群 @ 消息 → (回复列表, 被动回复用 msg_id)。"""
    openid = str(d.get("group_openid") or "")
    member = str((d.get("author") or {}).get("member_openid") or "")
    msg_id = str(d.get("id") or "")
    content = _clean_content(str(d.get("content") or ""))
    from . import events
    key, fresh, cached = events.claim("group", openid, msg_id)
    if not fresh:
        return [Reply(**row) for row in cached], msg_id
    try:
        replies = await asyncio.wait_for(
            _dispatch(content, openid, in_group=True, member_openid=member), timeout=25)
    except asyncio.TimeoutError:
        replies = [Reply(text="处理超时了，缩小问题范围或稍后再试。")]
    except Exception:
        logger.exception("群消息处理失败")
        replies = [Reply(text="暂时无法处理这条消息，请稍后重试。")]
    events.finish(key, replies)
    _record_conversation(openid, member, content, replies, msg_id)
    _record_inbound("GROUP_AT_MESSAGE_CREATE", openid, member,
                    content, msg_id, replies_text(replies))
    return replies, msg_id


async def handle_c2c_message(d: dict) -> tuple[list[Reply], str]:
    """C2C 私聊消息 → (回复列表, 被动回复用 msg_id)。"""
    openid = str((d.get("author") or {}).get("user_openid") or "")
    msg_id = str(d.get("id") or "")
    content = _clean_content(str(d.get("content") or ""))
    from . import events
    key, fresh, cached = events.claim("c2c", openid, msg_id)
    if not fresh:
        return [Reply(**row) for row in cached], msg_id
    try:
        replies = await asyncio.wait_for(
            _dispatch(content, "", in_group=False, member_openid=openid), timeout=25)
    except asyncio.TimeoutError:
        replies = [Reply(text="处理超时了，稍后再试。")]
    except Exception:
        logger.exception("私聊消息处理失败")
        replies = [Reply(text="暂时无法处理这条消息，请稍后重试。")]
    events.finish(key, replies)
    _record_conversation("", openid, content, replies, msg_id)
    _record_inbound("C2C_MESSAGE_CREATE", "", openid, content, msg_id,
                    replies_text(replies))
    return replies, msg_id
