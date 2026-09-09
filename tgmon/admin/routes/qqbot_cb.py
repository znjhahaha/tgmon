"""腾讯事件回调端点 —— QQ 开放平台 Webhook 模式的入口。

q.qq.com 管理端配置回调地址为 <对外地址>/qqbot/callback。
免登录（腾讯不携带我们的会话 cookie），但除 op=13 握手外每笔请求都验签。

两种请求：
- op=13 回调地址验证 —— 管理端保存回调地址时的握手，回 plain_token+signature。
  官方文档的校验请求示例只带 User-Agent 与 X-Bot-Appid 头，本就不带签名头，
  所以这个分支先于验签处理（对齐官方 qqbot-nodejs SDK 的 WebhookTransport）。
- 事件推送（GROUP_ADD_ROBOT / GROUP_DEL_ROBOT / GROUP_AT_MESSAGE_CREATE 等）
  —— 必须带 X-Signature-Ed25519 / X-Signature-Timestamp 并通过 Ed25519 验签，
  缺头或验签失败一律 403。事件处理见 qqbot.handle_callback_event()；
  被动回复（@ 机器人）需要 msg_id，在 5 分钟内调 API 回复。

签名密钥只有一个来源：qqbot.client.bot_secret()（机器人密钥/AppSecret）。
官方 sign.html 写明 Ed25519 种子取自「开发者平台的 Bot Secret」，与
getAppAccessToken 的 clientSecret 是同一个凭证。

历史教训（2026-09-04）：op13 曾改用管理端「机器人令牌」签名，平台连续报
13007 校验签名失败——令牌拿去换 token 是 100016 invalid，AppSecret 换
token 成功。「能换到 access_token 的那个才是签名密钥」，后台「QQ 机器人」
页的自检按钮（POST /qqbot/selfcheck）就是按这个不变量做的。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ... import qqbot
from ... import settings
from ...qqbot import client as qq_client
from ...util import log_event

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/qqbot")

# 被动回复去重：webhook 与 botpy WS 桥双通道并存时（管理端配了回调地址
# 的过渡期），同一 @ 事件会从两条链路各到一次 —— 平台按 msg_id+msg_seq
# 去重，第二轮必被 40054005 拒。这里记 5 分钟（被动回复窗口）内已回复
# 的 msg_id，重复到达直接跳过。
_REPLIED: dict[str, float] = {}
_REPLIED_TTL = 300.0


@router.post("/callback")
@router.post("/callback2")
async def callback(request: Request):
    # /callback2 是 /callback 的别名：平台对「校验失败的回调地址」有
    # 失败缓存/冷却（2026-09-04 实测：连续失败后点保存直接返回 13007，
    # 不再发 op13）。换一个 URL 字符串可绕过缓存强制重新校验。
    body = await request.body()
    sig = request.headers.get("x-signature-ed25519", "")
    ts = request.headers.get("x-signature-timestamp", "")
    appid_hdr = request.headers.get("x-bot-appid", "").strip()

    # 多机器人路由：按 X-Bot-Appid 头解析凭据；无头时取全部启用机器人
    #（验签逐个试，单机器人部署零变化）。密钥来源统一是 qq_bot 表 /
    # 旧配置回落（client.resolve_bot）。
    bots = _route_bots(appid_hdr)
    if not bots:
        logger.warning("QQ 回调到达但无可用机器人凭据，已拒绝")
        return JSONResponse({"error": "not configured"}, status_code=404)
    bot = bots[0]
    secret = bot.get("app_secret") or ""
    if not secret:
        logger.warning("QQ 回调到达但机器人密钥为空，已拒绝")
        return JSONResponse({"error": "not configured"}, status_code=404)

    try:
        event = json.loads(body)
    except Exception:
        log_event("warning", "qqbot", f"回调 body 非 JSON: {body[:80]!r}")
        return JSONResponse({"error": "bad json"}, status_code=400)

    op = event.get("op")

    # op=13 回调地址验证：不验签，直接处理。
    # 官方文档的校验请求示例只有 User-Agent 与 X-Bot-Appid 头（无签名头），
    # qqbot-nodejs SDK 的 OP_VALIDATION 分支也位于签名校验之前
    # （"no signature check needed"）。
    if op == 13:
        etype = event.get("t") or ""
        # 完整请求头留痕：平台校验请求是否带 X-Signature-* 头、
        # Content-Encoding 等传输细节，排查 13007 时都要看得到
        hdrs = {k: v for k, v in request.headers.items()}
        log_event("info", "qqbot", f"回调到达: op=13 t={etype} "
                  f"headers={hdrs!r}")
        if len(bots) > 1:
            # 无 AppID 头却启用了多个机器人：签名身份无法确定，
            # 用哪个密钥签都是赌 —— 拒绝并留痕，等待平台补头发来
            log_event("warning", "qqbot",
                      f"op13 无 X-Bot-Appid 头且有 {len(bots)} 个机器人，"
                      "无法确定签名身份，已拒绝")
            return JSONResponse({"error": "bot appid required"},
                                status_code=400)
        d = event.get("d") or {}
        plain_token = str(d.get("plain_token") or "")
        event_ts = str(d.get("event_ts") or "")
        log_event("info", "qqbot", f"op13 请求: token={plain_token} ts={event_ts}")
        # 签名体模式：官方文档为 event_ts+plain_token（默认）。实验模式
        # ts_full_body = X-Signature-Timestamp 头+原始请求 body —— 怀疑平台
        # 验证服务复用了事件推送验签的签名体格式（2026-09-04 密钥已与平台
        # 签名服务互验通过仍被拒后，验证此假设）
        mode = str(settings.get("QQ_OP13_SIGN_BODY") or "plain_token")
        try:
            if mode == "ts_full_body":
                from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                    Ed25519PrivateKey)
                seed = qq_client._seed_from_secret(secret)
                if not seed:
                    raise qq_client.QqApiError(None, "secret 为空")
                priv = Ed25519PrivateKey.from_private_bytes(seed)
                signature = priv.sign((ts or event_ts).encode() + body).hex()
            else:
                signature = qq_client.sign_validation(secret, event_ts,
                                                      plain_token)
        except qq_client.QqApiError as e:
            logger.warning("op13 签名生成失败: %s", e)
            return JSONResponse({"error": "bad validation payload"},
                                status_code=400)
        # 应答留痕带公钥指纹（pubfp）：密钥换没换、填没填错，一眼可见
        log_event("info", "qqbot",
                  f"op13 应答: token={plain_token} ts={event_ts} mode={mode} "
                  f"appid={bot.get('app_id')} "
                  f"pubfp={qq_client.public_key_fingerprint(secret)} "
                  f"sig={signature[:16]}… 全长={len(signature)}")
        return JSONResponse({"plain_token": plain_token, "signature": signature})

    # ---- 非 op13 请求：一律先验签 ----
    if not sig or not ts:
        # 缺签名头 = 未授权。事件入口必须验签，否则任何人裸 POST 都能伪造
        # 进群/退群/被 @ 事件并触发机器人发消息。
        log_event("warning", "qqbot",
                  f"回调缺签名头已拒绝: op={op} t={event.get('t', '')} "
                  f"ip={request.client.host if request.client else '?'} "
                  f"appid头={appid_hdr} "
                  f"UA={request.headers.get('user-agent', '')[:40]} "
                  f"body={body[:400]!r}")
        return JSONResponse({"error": "signature required"}, status_code=403)

    # 候选机器人的密钥逐个验（无 AppID 头的多机器人部署也能对上号）
    verified = None
    for cand in bots:
        if cand.get("app_secret") and qq_client.verify_callback(
                cand["app_secret"], ts, body, sig):
            verified = cand
            break
    if verified is None:
        # 记全签名与报文 —— 若平台换了签名算法，这里留下的数据
        # 足够对拍出新算法
        log_event("warning", "qqbot",
                  f"回调验签失败(签名不匹配): op={op} t={event.get('t', '')} "
                  f"UA={request.headers.get('user-agent', '')[:40]} "
                  f"appid头={appid_hdr} "
                  f"sig={sig[:64]} ts={ts} body={body[:400]!r}")
        return JSONResponse({"error": "bad signature"}, status_code=403)
    bot = verified

    # 每笔到达且验签通过的请求都留痕 —— 排查平台校验问题时
    # 「没日志」和「没请求」必须能区分开
    etype = event.get("t") or ""
    log_event("info", "qqbot", f"回调到达: op={op} t={etype} "
                              f"appid={bot.get('app_id')}")

    from ...qqbot.runtime import receive
    await receive(event, bot, source="webhook")

    # 官方要求 2xx 应答；op:12 是标准 ACK（官方 SDK 响应体为 {op:12, d:0}）
    return JSONResponse({"op": 12, "d": 0})


def _route_bots(appid_hdr: str) -> list[dict]:
    """回调路由的候选机器人。

    - 带 AppID 头：精确匹配（找不到返回空 → 404）
    - 无头：全部启用机器人（供验签逐个试）；表空时回落旧配置
    """
    if appid_hdr:
        try:
            return [qq_client.resolve_bot(app_id=appid_hdr)]
        except qq_client.QqApiError:
            return []
    from ...db import session_scope
    from ...models import QqBot
    out: list[dict] = []
    with session_scope() as s:
        rows = s.query(QqBot).filter(QqBot.enabled.is_(True)).all()
        out = [qq_client._bot_dict(r) for r in rows]
    if not out:
        try:
            out = [qq_client.resolve_bot()]
        except qq_client.QqApiError:
            return []
    return out


async def _handle_and_reply(event: dict, bot: dict | None, source: str, deadline=None) -> None:
    from ...qqbot import events
    from ...conversation_scope import bot_identity
    data = event.get("d") or {}
    private = str(event.get("type") or event.get("t") or "") == "C2C_MESSAGE_CREATE"
    target = str((data.get("author") or {}).get("user_openid") or "") if private else str(data.get("group_openid") or "")
    async with events.conversation_lock("c2c" if private else "group", target, bot_identity(bot)):
        from ...conversation_scope import reply_deadline
        token = reply_deadline.set(deadline)
        try:
            result = await qqbot.handle_callback_event(event, bot=bot)
            if deadline and result.get("msg_id"):
                key = events.event_key("c2c" if private else "group", target,
                                       result["msg_id"], bot_identity(bot))
                events.set_reply_deadline(key, deadline)
            await _passive_reply(result, source=source)
        finally:
            reply_deadline.reset(token)


async def _passive_reply(result: dict, source: str) -> None:
    from ...qqbot import events
    from ...qqbot.sending import send_parts
    replies, target, mid = result.get("replies") or [], result.get("target"), result.get("msg_id")
    if not replies or not target or not mid:
        return
    private = result.get("channel") == "c2c"
    bot = result.get("bot")
    from ...conversation_scope import activate, bot_identity
    key = events.event_key("c2c" if private else "group", str(target), str(mid), bot_identity(bot))
    parts = await send_parts(str(target), replies, key=key, private=private,
                             msg_id=str(mid), bot=bot)
    conversation = result.get("conversation")
    if (conversation and parts
            and all(p.get("status") == "done" for p in parts.values())):
        from ...qqbot.commands import record_confirmed_conversation
        with activate(bot, conversation["group"], conversation["member"], str(mid)):
            record_confirmed_conversation(conversation["group"], conversation["member"],
                                          conversation["content"], replies[:5], str(mid))


            # 媒体条失败不终止后续条（比如第 2 条是图挂了，第 3 条文本还能发）


@router.post("/bridge")
async def bridge(request: Request):
    """botpy WS 事件桥的入口（内网专用，X-Bridge-Token 鉴权）。

    botpy 容器（scripts/botpy_official.py）从 QQ 平台 WS 网关收到事件后
    转发到这里，格式与平台 webhook 回调同构（{"op":0,"t":...,"d":{...}}），
    外加 bot_appid 字段标明来源机器人（多机器人各起一个桥容器）。
    op=14 是桥心跳，按机器人刷新 bridge_last_seen 不进业务。

    安全边界：该端点必须只有 docker 内网可达（Caddy 不反代 /qqbot/bridge
    之外的桥路径），令牌为第二道闸。公网流量理论上到不了这里 ——
    Caddy 只反代了 /qqbot/callback 给平台用。
    """
    token = request.headers.get("x-bridge-token", "")
    expected = str(settings.get("QQ_BRIDGE_TOKEN") or "")
    if not expected or token != expected:
        log_event("warning", "qqbot",
                  f"桥请求被拒: 令牌不匹配 ip={request.client.host if request.client else '?'}")
        return JSONResponse({"error": "forbidden"}, status_code=403)

    body = await request.body()
    try:
        event = json.loads(body)
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)

    # 按报文里的 bot_appid 路由到对应机器人；旧版桥不带该字段时回落
    # 单机器人语义（恰好一个启用机器人 / 旧配置）。多机器人部署必须
    # 用新版桥脚本，否则事件无法归属。
    bot_appid = str(event.get("bot_appid") or "").strip()
    bot = None
    try:
        bot = qq_client.resolve_bot(app_id=bot_appid) if bot_appid \
            else qq_client.resolve_bot()
    except qq_client.QqApiError as e:
        log_event("warning", "qqbot",
                  f"桥事件无法归属机器人（bot_appid={bot_appid or '未携带'}）: {e.message}")
        if event.get("op") != 14:
            return JSONResponse({"error": "unknown bot"}, status_code=403)

    # 心跳：桥每 60s 一次，管理页「桥在线」靠它（全局 + 按机器人各记一份）
    settings.set_many({
        "QQ_BRIDGE_LAST_SEEN": datetime.utcnow().isoformat()})
    if bot and bot.get("id"):
        from ...db import session_scope
        from ...models import QqBot as QqBotModel
        with session_scope() as s:
            row = s.get(QqBotModel, bot["id"])
            if row is not None:
                row.bridge_last_seen = datetime.utcnow()
    if event.get("op") == 14:
        return JSONResponse({"op": 12, "d": 0})

    etype = str(event.get("t") or "")
    log_event("info", "qqbot",
              f"桥事件: t={etype} appid={bot.get('app_id') if bot else '?'}")

    from ...qqbot.runtime import receive
    await receive(event, bot, source="bridge")
    return JSONResponse({"op": 12, "d": 0})
