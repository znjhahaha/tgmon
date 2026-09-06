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

    # 唯一密钥来源：机器人密钥（AppSecret）。别在这里读别的配置项——
    # 2026-09-04 的事故就是这里改读了「机器人令牌」导致平台 13007。
    secret = qq_client.bot_secret()
    if not secret:
        # 没配密钥就没有验签依据，一律拒绝（管理端配置前不应有流量）
        logger.warning("QQ 回调到达但未配置 AppSecret，已拒绝")
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
                  f"appid头={request.headers.get('x-bot-appid', '')} "
                  f"UA={request.headers.get('user-agent', '')[:40]} "
                  f"body={body[:400]!r}")
        return JSONResponse({"error": "signature required"}, status_code=403)

    if not qq_client.verify_callback(secret, ts, body, sig):
        # 记全签名与报文 —— 若平台换了签名算法，这里留下的数据
        # 足够对拍出新算法
        log_event("warning", "qqbot",
                  f"回调验签失败(签名不匹配): op={op} t={event.get('t', '')} "
                  f"UA={request.headers.get('user-agent', '')[:40]} "
                  f"sig={sig[:64]} ts={ts} body={body[:400]!r}")
        return JSONResponse({"error": "bad signature"}, status_code=403)

    # 每笔到达且验签通过的请求都留痕 —— 排查平台校验问题时
    # 「没日志」和「没请求」必须能区分开
    etype = event.get("t") or ""
    log_event("info", "qqbot", f"回调到达: op={op} t={etype} "
                              f"appid={request.headers.get('x-bot-appid', '')}")

    result = await qqbot.handle_callback_event(event)
    await _passive_reply(result, source="webhook")

    # 官方要求 2xx 应答；op:12 是标准 ACK（官方 SDK 响应体为 {op:12, d:0}）
    return JSONResponse({"op": 12, "d": 0})


async def _passive_reply(result: dict, source: str) -> None:
    from ...qqbot import events
    from ...qqbot.sending import send_parts
    replies, target, mid = result.get("replies") or [], result.get("target"), result.get("msg_id")
    if not replies or not target or not mid:
        return
    private = result.get("channel") == "c2c"
    key = events.event_key("c2c" if private else "group", str(target), str(mid))
    await send_parts(str(target), replies, key=key, private=private, msg_id=str(mid))


            # 媒体条失败不终止后续条（比如第 2 条是图挂了，第 3 条文本还能发）


@router.post("/bridge")
async def bridge(request: Request):
    """botpy WS 事件桥的入口（内网专用，X-Bridge-Token 鉴权）。

    botpy 容器（botpy_official.py）从 QQ 平台 WS 网关收到事件后转发到
    这里，格式与平台 webhook 回调同构（{"op":0,"t":...,"d":{...}}）。
    op=14 是桥心跳，只刷新 QQ_BRIDGE_LAST_SEEN 不进业务。

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

    # 心跳：桥每 60s 一次，管理页「桥在线」靠它
    settings.set_many({
        "QQ_BRIDGE_LAST_SEEN": datetime.utcnow().isoformat()})
    if event.get("op") == 14:
        return JSONResponse({"op": 12, "d": 0})

    etype = str(event.get("t") or "")
    log_event("info", "qqbot", f"桥事件: t={etype}")

    result = await qqbot.handle_callback_event(event)
    await _passive_reply(result, source="bridge")
    return JSONResponse({"op": 12, "d": 0})
