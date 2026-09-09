"""QQ 群推送出站 —— 与 webhook 出站平行的第四种出口。

链路：pipeline 入库 → runner 调 push_message() → 过滤（频道/重复/日限）
→ 每个启用的 QqGroup 建投递记录 → 调腾讯 API。失败按 webhook 同款节奏
（30s/120s/600s/3600s）在维护循环里重试。

事务纪律与 outputs.py 一致：SQLite 写锁宝贵，session 里只做读写，
网络调用一律在 session 外（先拷出普通值，回写再开短 session）。

平台约束（2026-09 官方文档）：
- 主动消息权限按机器人账户反馈判断，保留本地推送预算
- 主动消息禁止 URL（错误码 40054010）→ 推送文案是纯文本摘要
- 机器人不在群（40034101/40054003）→ 标记 dropped 停用，页面提示重新拉群

内容形态：同条爆料的全部图片合成一张长图，正文作为同条媒体消息的说明；
纯图爆料只发图，纯视频等保底推一行标题。不再出现「（无文本）」占位。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from .. import settings
from ..db import session_scope
from ..models import MessageMedia, QqDelivery, QqGroup
from ..util import log_event
from . import client
from .client import QqApiError

logger = logging.getLogger(__name__)

RETRY_BACKOFF = [30, 120, 600, 3600]  # 秒，同 webhook
MAX_ATTEMPTS = 5
# content 保守上限（平台超限报 40054007）
MAX_CONTENT = 1800
_URL_RE = re.compile(r"https?://\S+")


def _format_text(payload: dict) -> str:
    """serialize() 的 payload → QQ 纯文本。不放任何链接（平台禁止）。

    正文为空时只返回标题行（纯视频等保底）；纯图爆料的图片推送由
    _push_content 决策，这里不再填「（无文本）」占位（2026-09 群反馈）。
    """
    ch = payload.get("channel") or {}
    game = payload.get("game_detected") or ch.get("game") or ""
    title = (ch.get("title") or "").strip()
    head = f"【{game}】{title}" if game else title
    ver = payload.get("version_tag")
    if ver:
        head += f"（{ver}）"

    body = _body_text(payload)
    if payload.get("has_spoiler"):
        body = f"⚠ 含剧透\n{body}" if body else "⚠ 含剧透"

    text = (head + "\n" + body).strip() if body else head
    if len(text) > MAX_CONTENT:
        text = text[:MAX_CONTENT - 1] + "…"
    return text


def _body_text(payload: dict) -> str:
    """正文（译文优先），剥掉 URL。空 = 纯图/纯视频爆料。"""
    body = (payload.get("text_zh") or payload.get("text_raw") or "").strip()
    # 去掉正文里的 URL：主动消息带链接会被整条拒绝
    return _URL_RE.sub("", body).strip()


def _today_local() -> str:
    # 容器 TZ=Asia/Shanghai（compose 已设），now() 即本地时间
    return datetime.now().strftime("%Y-%m-%d")


def _photo_paths(message_id: int) -> list[str]:
    """消息的图片缩略图相对路径（media/ 下），供 GitHub 中转上传用。"""
    with session_scope() as s:
        rows = (s.query(MessageMedia.thumb_path)
                .filter(MessageMedia.message_id == message_id,
                        MessageMedia.kind == "photo",
                        MessageMedia.thumb_path.isnot(None))
                .order_by(MessageMedia.id.asc()).all())
    return [r[0] for r in rows if r[0]]


def _push_content(payload: dict, message_id: int) -> tuple[str, list[str]]:
    """Keep captioned albums intact; pure albums have no extra text."""
    photos = _photo_paths(message_id)
    if photos and not _body_text(payload):
        return "", photos
    return _format_text(payload), photos


async def _send(openid: str, text: str,
                bot: dict | None = None) -> tuple[bool, str | None, bool, bool]:
    """纯网络层投递。返回 (成功, 错误描述, 机器人不在群, 权限类不可重试)。

    40054010（含 URL）/ 40054007（超长）做一次补救重发，其余原样上抛。
    40034105（主动消息无权限）是主体资质问题，重试必然失败 → fatal。
    """
    try:
        await client.send_group_text(openid, text, bot=bot)
        return True, None, False, False
    except QqApiError as e:
        err = f"code={e.code} {e.message}"
        if e.code == client.ERR_URL_FORBIDDEN:
            try:
                await client.send_group_text(openid, _URL_RE.sub("", text), bot=bot)
                return True, None, False, False
            except QqApiError as e2:
                err = f"code={e2.code} {e2.message}"
        elif e.code == client.ERR_MSG_TOO_LONG:
            try:
                await client.send_group_text(
                    openid, text[:MAX_CONTENT - 1] + "…", bot=bot)
                return True, None, False, False
            except QqApiError as e2:
                err = f"code={e2.code} {e2.message}"
        elif e.code in client.ERR_NOT_IN_GROUP:
            return False, err, True, False
        elif e.code == client.ERR_NO_PERMISSION:
            # 个人主体默认不开主动消息权限（平台硬限制）。群内被动
            # 回复（@ 机器人命令/AI）不受影响，只是「主动推」被拦。
            return (False,
                    err + "｜主动消息无权限：q.qq.com 管理端查看能否"
                          "申请；群内 @ 机器人 /latest 拉取不受影响",
                    False, True)
        return False, err, False, False
    except Exception as e:  # 网络层
        return False, f"{type(e).__name__}: {e}", False, False


async def _send_media(openid: str,
                      photos: list[str], text: str = "",
                      bot: dict | None = None) -> tuple[bool, str | None, bool, bool]:
    """One complete album is one outgoing message and one daily quota unit."""
    from . import media as qq_media
    try:
        fi = await qq_media.upload_album(openid, photos, raise_fatal=True, bot=bot)
        if not fi:
            return False, "整组图片上传失败（文件缺失或中转不可用）", False, False
        await client.send_group_media(openid, fi, content=text or None, bot=bot)
        return True, None, False, False
    except QqApiError as exc:
        error = f"code={exc.code} {exc.message}"
        if exc.code in client.ERR_NOT_IN_GROUP:
            return False, error, True, False
        if exc.code == client.ERR_NO_PERMISSION:
            return False, error + "｜主动消息无权限，可在群内 @ 机器人 /latest 拉取", False, True
        return False, error, False, False
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}", False, False


async def _deliver_content(openid: str, text: str,
                           photos: list[str],
                           bot: dict | None = None) -> tuple[bool, str | None, bool, bool]:
    """Caption and whole album share one media message."""
    if photos:
        return await _send_media(openid, photos, text, bot=bot)
    return await _send(openid, text, bot=bot)


def _record(did: int, ok: bool, err: str | None, attempts: int,
            openid: str, dropped: bool, fatal: bool = False) -> None:
    """短事务回写投递结果（以及「不在群」时停用群）。"""
    with session_scope() as s:
        d = s.get(QqDelivery, did)
        if d is not None:
            d.attempts = attempts
            d.error = err
            if ok:
                d.status = "done"
                d.delivered_at = datetime.utcnow()
                d.next_retry_at = None
            elif attempts >= MAX_ATTEMPTS:
                d.status = "failed"
                d.next_retry_at = None
            elif dropped:
                # 机器人不在群：群都停用了，重试必然失败，直接终结
                d.status = "failed"
                d.next_retry_at = None
            elif fatal:
                # 权限类失败（40034105 无主动消息权限）：重试无意义，
                # 直接终结，但不停用群 —— 被动回复照常可用
                d.status = "failed"
                d.next_retry_at = None
            else:
                d.status = "retry"
                delay = RETRY_BACKOFF[min(attempts - 1, len(RETRY_BACKOFF) - 1)]
                d.next_retry_at = datetime.utcnow() + timedelta(seconds=delay)
        if ok:
            g = s.query(QqGroup).filter(QqGroup.group_openid == openid).first()
            if g is not None:
                g.last_sent_at = datetime.utcnow()
        if dropped:
            g = s.query(QqGroup).filter(QqGroup.group_openid == openid).first()
            if g is not None:
                g.enabled = False
                g.dropped_at = datetime.utcnow()


def _claim_groups(n_msgs: int = 1) -> list[dict]:
    """读启用群并原子地记当日计数。超日限的群不进返回列表（熔断丢弃）。

    n_msgs：本轮投递预计产生的主动消息条数（纯图爆料 = 图片张数，
    每张图是一条独立的主动消息，都占配额）。
    计数与读取放同一个短事务：多 worker 并发时也不会双双拿到旧值
    （本项目单 worker，防御性写法）。
    """
    today = _today_local()
    limit = int(settings.get("QQ_DAILY_LIMIT") or 0)
    n_msgs = max(1, n_msgs)
    out: list[dict] = []
    with session_scope() as s:
        for g in s.query(QqGroup).filter(QqGroup.enabled.is_(True)).all():
            if g.sent_date != today:
                g.sent_date = today
                g.sent_today = 0
            if limit > 0 and (g.sent_today >= limit or g.sent_today + n_msgs > limit):
                log_event("warning", "qqbot",
                          f"群 {g.nickname or g.group_openid[:8]}… 达当日上限，本轮丢弃")
                continue
            g.sent_today += n_msgs
            out.append({"openid": g.group_openid,
                        "nickname": g.nickname or g.group_openid[:8]})
    return out


async def push_message(message_id: int, force: bool = False) -> int:
    from .batches import enqueue, flush_pending
    queued = enqueue(message_id, force=force)
    if force and queued:
        await flush_pending()
    return queued




async def retry_pending() -> int:
    from .batches import flush_pending, retry_batches
    await flush_pending()
    await retry_batches()
    """维护循环调用：重试到期的失败投递。"""
    from .. import outputs

    now = datetime.utcnow()
    with session_scope() as s:
        rows = (s.query(QqDelivery)
                .filter(QqDelivery.status == "retry",
                        QqDelivery.bundle_id.is_(None),
                        QqDelivery.next_retry_at.isnot(None),
                        QqDelivery.next_retry_at <= now)
                .limit(20).all())
        pending = [(r.id, r.group_openid, r.message_id, r.attempts)
                   for r in rows]
    if not pending:
        return 0

    n = 0
    for did, openid, mid, attempts in pending:
        # 群还在且启用才重试；顺带取归属机器人
        with session_scope() as s:
            g = s.query(QqGroup).filter(QqGroup.group_openid == openid).first()
            alive = bool(g and g.enabled)
            bot_id = g.bot_id if g is not None else None
        if not alive:
            _record(did, False, "群已停用或删除", attempts + 1, openid, False)
            # 直接标 failed，不再进重试队列
            with session_scope() as s:
                d = s.get(QqDelivery, did)
                if d is not None:
                    d.status = "failed"
                    d.next_retry_at = None
            continue
        try:
            bot = client.resolve_bot(bot_id=bot_id) if bot_id else client.resolve_bot()
        except client.QqApiError as e:
            _record(did, False, f"机器人不可用: {e.message}", attempts + 1,
                    openid, False)
            with session_scope() as s:
                d = s.get(QqDelivery, did)
                if d is not None:
                    d.status = "failed"
                    d.next_retry_at = None
            continue

        if mid:
            payload = outputs.serialize(mid, include_original=False)
            if payload is None:
                with session_scope() as s:
                    d = s.get(QqDelivery, did)
                    if d is not None:
                        d.status = "failed"
                        d.error = "消息已删除"
                        d.next_retry_at = None
                continue
            text, photos = _push_content(payload, mid)
        else:
            text, photos = "tgmon 测试消息（重试）", []
        if not text and not photos:
            with session_scope() as s:
                d = s.get(QqDelivery, did)
                if d is not None:
                    d.status = "failed"
                    d.error = "无可投递内容"
                    d.next_retry_at = None
            continue

        # 日限内才继续
        today = _today_local()
        limit = int(settings.get("QQ_DAILY_LIMIT") or 0)
        n_msgs = 1
        over = False
        with session_scope() as s:
            g = s.query(QqGroup).filter(QqGroup.group_openid == openid).first()
            if g is not None:
                if g.sent_date != today:
                    g.sent_date = today
                    g.sent_today = 0
                if limit > 0 and (g.sent_today >= limit or g.sent_today + n_msgs > limit):
                    over = True
                else:
                    g.sent_today += n_msgs
        if over:
            with session_scope() as s:
                d = s.get(QqDelivery, did)
                if d is not None:
                    d.status = "failed"
                    d.error = "达当日上限"
                    d.next_retry_at = None
            continue

        ok, err, dropped, fatal = await _deliver_content(openid, text, photos, bot=bot)
        _record(did, ok, err, attempts + 1, openid, dropped, fatal)
        if ok:
            n += 1
    return n


async def test_message(openid: str) -> dict:
    """设置页「发测试消息」按钮。绕过 QQ_ENABLED 开关（不然没法在关闭
    状态下验证配置），但正常走投递记录与日限。"""
    text = "tgmon QQ 推送测试：链路通了。此后新爆料会自动发到本群。"
    with session_scope() as s:
        g = s.query(QqGroup).filter(QqGroup.group_openid == openid).first()
        if g is None:
            return {"ok": False, "error": "群不存在"}
        nickname = g.nickname or openid[:8]
        bot_id = g.bot_id
        d = QqDelivery(group_openid=openid, message_id=None)
        s.add(d)
        s.flush()
        did = d.id
    try:
        bot = client.resolve_bot(bot_id=bot_id) if bot_id else client.resolve_bot()
    except client.QqApiError as e:
        _record(did, False, f"机器人不可用: {e.message}", 1, openid, False)
        return {"ok": False, "error": e.message[:200], "nickname": nickname}
    ok, err, dropped, fatal = await _send(openid, text, bot=bot)
    _record(did, ok, err, 1, openid, dropped, fatal)
    if ok:
        return {"ok": True, "nickname": nickname}
    return {"ok": False, "error": (err or "失败")[:200], "nickname": nickname}


async def handle_callback_event(event: dict, bot: dict | None = None) -> dict:
    """处理腾讯事件（admin 的 /qqbot/callback 与 /qqbot/bridge 都走这里）。

    bot：事件归属的机器人上下文（client.resolve_bot 的返回值）。多机器人
    时由入口按 X-Bot-Appid 头 / 桥报文解析后传入；进群事件据此记录群归属。

    关心四件事：
    - GROUP_ADD_ROBOT     机器人进群 → 记录 group_openid 与归属机器人
    - GROUP_DEL_ROBOT     机器人被移出 → 停用该群
    - GROUP_AT_MESSAGE_CREATE  被 @ → 命令路由 / AI 对话（commands.py）
    - C2C_MESSAGE_CREATE  私聊 → AI 对话
    返回 {"handled": 类型, "replies": list[Reply] or [], "msg_id": 被动回复用,
          "channel": "group"/"c2c", "target": 回复目标 openid, "bot": 机器人上下文}
    """
    from . import commands

    etype = str(event.get("type") or event.get("t") or "")
    d = event.get("d") or {}
    openid = str(d.get("group_openid") or "")
    owner = (bot or {}).get("id") or None

    if etype == "GROUP_ADD_ROBOT" and openid:
        with session_scope() as s:
            g = s.query(QqGroup).filter(QqGroup.group_openid == openid).first()
            if g is None:
                s.add(QqGroup(group_openid=openid, bot_id=owner))
                log_event("info", "qqbot", f"机器人进群（openid {openid[:8]}…），"
                                           "去后台「QQ 机器人」页给它标个群名")
            else:
                g.enabled = True
                g.dropped_at = None
                if owner:
                    g.bot_id = owner
        return {"handled": "add", "replies": [], "msg_id": None, "bot": bot}

    if etype == "GROUP_DEL_ROBOT" and openid:
        with session_scope() as s:
            g = s.query(QqGroup).filter(QqGroup.group_openid == openid).first()
            if g is not None:
                g.enabled = False
                g.dropped_at = datetime.utcnow()
        log_event("info", "qqbot", f"机器人被移出群（openid {openid[:8]}…），已停用")
        return {"handled": "del", "replies": [], "msg_id": None, "bot": bot}

    if etype == "GROUP_AT_MESSAGE_CREATE" and openid:
        with session_scope() as s:
            g = s.query(QqGroup).filter(QqGroup.group_openid == openid).first()
            if g is None:
                # 进群回调漏了（比如换接入方式之前拉的群）也能被 @ 激活补录
                s.add(QqGroup(group_openid=openid, bot_id=owner))
                log_event("info", "qqbot",
                          f"被 @ 时发现群（openid {openid[:8]}…）没有记录，已补录")
        replies, msg_id = await commands.handle_group_message(d, bot=bot)
        return {"handled": "at", "replies": replies, "msg_id": msg_id,
                "channel": "group", "target": openid, "bot": bot,
                "conversation": {"group": openid,
                    "member": str((d.get("author") or {}).get("member_openid") or ""),
                    "content": commands._clean_content(str(d.get("content") or ""))}}

    if etype == "C2C_MESSAGE_CREATE":
        replies, msg_id = await commands.handle_c2c_message(d, bot=bot)
        return {"handled": "c2c", "replies": replies, "msg_id": msg_id,
                "channel": "c2c",
                "target": str((d.get("author") or {}).get("user_openid") or ""),
                "bot": bot,
                "conversation": {"group": "",
                    "member": str((d.get("author") or {}).get("user_openid") or ""),
                    "content": commands._clean_content(str(d.get("content") or ""))}}

    return {"handled": None, "replies": [], "msg_id": None, "bot": bot}
