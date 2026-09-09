"""QQ 开放平台 API 客户端：access_token 管理、群消息发送、回调验签。

文档：https://bot.q.qq.com/wiki/develop/api-v2
认证：AppID + AppSecret 换 access_token（约 7200s），请求头 QQBot <token>。

多机器人（2026-09）：凭据存在 qq_bot 表（每行一个机器人），token 缓存与
验签按 app_id 隔离。调用链把 bot 上下文（{"id", "app_id", "app_secret"}）
从事件入口 / 推送队列一路传到这里；bot=None 时回落「单机器人」语义 ——
恰好一个启用机器人用它，零个时回落旧配置 QQ_APP_ID / QQ_APP_SECRET，
多个时抛错（防止静默用错凭据）。
"""
from __future__ import annotations

import base64
import logging
import time
from datetime import datetime

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from .. import settings

logger = logging.getLogger(__name__)

TOKEN_URL = "https://api.bot.qq.com/app/getAppAccessToken"
API_BASE = "https://api.bot.qq.com"

# token 进程级缓存，按 app_id 隔离。admin / worker 各自持有一份，互不干扰
_tokens: dict[str, tuple[str, float]] = {}
_uploads: dict[str, tuple[str, float]] = {}

# 错误码语义（官方文档「发送群聊消息」一节）
# 频控 / 可重试
ERR_RATE_LIMIT = 40034100        # 主动消息超过频控，等配额恢复
ERR_MSG_TOO_LONG = 40054007     # 消息长度超限 → 截断重发
# 机器人不在该群（被移出 / 从未加入）
ERR_NOT_IN_GROUP = (40034101, 40054003)
# 无权限（个人主体未开主动消息等）
ERR_NO_PERMISSION = 40034105
# 不允许发送 URL
ERR_URL_FORBIDDEN = 40054010
# 机器人被禁言
ERR_MUTED = 40054002


class QqApiError(Exception):
    """携带腾讯错误码的异常。code 为 None 表示网络层失败。"""

    def __init__(self, code: int | None, message: str):
        self.code = code
        self.message = message
        super().__init__(f"QQ API {code}: {message}")


# ---------------- 多机器人凭据解析 ----------------

def resolve_bot(bot_id: int | None = None, app_id: str | None = None) -> dict:
    """解析机器人凭据。返回 {"id": int, "app_id": str, "app_secret": str}。

    - 指定 bot_id 或 app_id：查 qq_bot 表对应行；不存在或已停用 → 抛错
    - 都不指定（单机器人语义）：
        * 恰好一个启用机器人 → 用它
        * 零个 → 回落旧配置 QQ_APP_ID / QQ_APP_SECRET（配了才有，否则抛错）
        * 多个 → 抛错：调用方必须带上下文
    id=0 表示「旧配置回落」（无表行），群归属列允许 NULL。
    """
    from ..db import session_scope
    from ..models import QqBot

    if bot_id or app_id:
        with session_scope() as s:
            q = s.query(QqBot)
            row = (q.filter(QqBot.id == bot_id).first() if bot_id
                   else q.filter(QqBot.app_id == str(app_id).strip()).first())
            if row is None:
                raise QqApiError(None, f"机器人不存在（bot_id={bot_id} app_id={app_id}）")
            if not row.enabled:
                raise QqApiError(None, f"机器人已停用（{row.nickname or row.app_id}）")
            return _bot_dict(row)
    with session_scope() as s:
        rows = s.query(QqBot).filter(QqBot.enabled.is_(True)).all()
        if len(rows) == 1:
            return _bot_dict(rows[0])
        if len(rows) > 1:
            raise QqApiError(None, f"启用了 {len(rows)} 个机器人，调用必须指明用哪个")
    # 零个表行：旧配置回落（settings 解密后给出）
    legacy_id = str(settings.get("QQ_APP_ID") or "").strip()
    legacy_secret = str(settings.get("QQ_APP_SECRET") or "").strip()
    if legacy_id and legacy_secret:
        return {"id": 0, "app_id": legacy_id, "app_secret": legacy_secret}
    raise QqApiError(None, "未配置任何 QQ 机器人（qq_bot 表为空且旧配置缺失）")


def _bot_dict(row) -> dict:
    from ..crypto import decrypt
    secret = ""
    if row.app_secret_enc:
        try:
            secret = decrypt(row.app_secret_enc)
        except Exception as e:
            logger.warning("机器人 %s 的密钥解密失败: %s", row.app_id, e)
    proactive_enabled = True
    try:
        from ..db import session_scope
        from ..models import QqBotCapability
        with session_scope() as s:
            cap = s.get(QqBotCapability, row.id)
            if cap is not None:
                proactive_enabled = bool(cap.proactive_enabled)
    except Exception:
        # Capability state is advisory. Credential resolution must still work
        # when an older database is being upgraded.
        logger.debug("读取 QQ 机器人能力失败", exc_info=True)
    return {"id": row.id, "app_id": row.app_id, "app_secret": secret,
            "proactive_enabled": proactive_enabled}


def enabled_bot_ids() -> set[int]:
    """启用中的机器人 id 集合（推送扇出过滤用）。"""
    from ..db import session_scope
    from ..models import QqBot
    with session_scope() as s:
        rows = s.query(QqBot).filter(QqBot.enabled.is_(True)).all()
        from ..models import QqBotCapability
        caps = {c.bot_id: c for c in s.query(QqBotCapability).all()}
        return {r.id for r in rows if caps.get(r.id) is None
                or caps[r.id].proactive_enabled}


def disable_proactive(bot_id: int | None, error: str) -> None:
    """Persist a fatal proactive-delivery capability error for one bot."""
    if not bot_id:
        return
    from ..db import session_scope
    from ..models import QqBotCapability
    with session_scope() as s:
        row = s.get(QqBotCapability, bot_id)
        if row is None:
            row = QqBotCapability(bot_id=bot_id)
            s.add(row)
        row.proactive_enabled = False
        row.proactive_error = str(error or "")[:1000]
        row.proactive_disabled_at = datetime.utcnow()


def bot_secret(bot: dict | None = None) -> str:
    """Bot Secret —— 管理端「机器人密钥」(AppSecret)。

    官方文档里 Ed25519 签名种子与 API 鉴权用的是**同一个**凭证：sign.html
    的种子取自「开发者平台的 Bot Secret」，getAppAccessToken 的 clientSecret
    也是它。所以回调验签和换 token 必须读同一处，别再各读各的 ——
    2026-09-04 的回调校验失败正是因为验签那侧改读了「机器人令牌」，而那个
    值拿去换 token 会被平台判 100016 invalid appid or secret。

    判断密钥是否填对的现成办法：能换到 access_token 的那个才是签名密钥。
    """
    bot = bot or resolve_bot()
    return str(bot.get("app_secret") or "").strip()


def _credentials(bot: dict | None = None) -> tuple[str, str]:
    bot = bot or resolve_bot()
    return str(bot.get("app_id") or "").strip(), bot_secret(bot)


async def _fetch_token(app_id: str, app_secret: str) -> tuple[str, float]:
    """换新 token。返回 (token, 过期时间戳)。

    注意参数名是 appId / clientSecret（驼峰）—— 旧 SDK 的
    grant_type/client_id 格式已被平台废弃，会报 100007 appid invalid。
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(TOKEN_URL, json={
            "appId": app_id,
            "clientSecret": app_secret,
        })
        data = resp.json()
    token = data.get("access_token")
    if not token:
        raise QqApiError(data.get("code"), str(data.get("message") or data)[:200])
    try:
        ttl = int(data.get("expires_in"))
    except (TypeError, ValueError):
        ttl = 7000
    # 提前 5 分钟过期，避免边界上拿着将失效的 token 发请求
    return token, time.time() + max(60, ttl - 300)


async def _get_token(bot: dict | None = None) -> str:
    """带缓存的 token 获取（按 app_id 隔离）。无效凭证直接抛 QqApiError。"""
    bot = bot or resolve_bot()
    app_id, app_secret = _credentials(bot)
    if not app_id or not app_secret:
        raise QqApiError(None, "机器人 AppID / AppSecret 未配置")
    cached = _tokens.get(app_id)
    if cached and time.time() < cached[1]:
        return cached[0]
    token, exp = await _fetch_token(app_id, app_secret)
    _tokens[app_id] = (token, exp)
    return token


def invalidate_token(bot: dict | None = None) -> None:
    """凭证改动 / token 失效时清缓存。不传 bot = 全部清空。"""
    if bot is None:
        _tokens.clear()
        _uploads.clear()
    else:
        _tokens.pop(str(bot.get("app_id") or ""), None)


async def check_credentials(bot: dict | None = None) -> tuple[bool, str]:
    """自检：回调验签用的那个密钥，能不能换到 access_token。

    因为签名种子和 API 鉴权是同一个凭证，这一次请求同时证明了两件事。
    没有它的话，密钥填错只会表现为腾讯管理端一句「校验失败」，本地无从
    分辨是密钥错还是算法错 —— 2026-09-04 就是这样绕了一大圈。

    返回 (是否有效, 说明)。
    """
    try:
        bot = bot or resolve_bot()
    except QqApiError as e:
        return False, e.message[:160]
    app_id, secret = _credentials(bot)
    if not app_id or not secret:
        return False, "AppID 或机器人密钥未填"
    try:
        await _fetch_token(app_id, secret)
    except QqApiError as e:
        return False, f"{e.code}: {e.message}"[:160]
    except Exception as e:  # 网络层等
        return False, f"请求失败: {e}"[:160]
    return True, "有效"


async def _post_message(path: str, body: dict, bot: dict | None = None) -> str:
    """群/C2C 消息发送的公共路径。成功返回消息 id。

    token 失效（11010 等）时清缓存抛出，调用方按需重试一次。
    """
    token = await _get_token(bot)
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{API_BASE}{path}",
            json=body,
            headers={"Authorization": f"QQBot {token}",
                     "Content-Type": "application/json"},
        )
    if resp.status_code != 200:
        try:
            data = resp.json()
        except Exception:
            data = {}
        if (isinstance(data, dict) and data.get("code") in (11010, 11253, 200)) \
                or resp.status_code == 401:
            invalidate_token(bot)
        raise QqApiError(data.get("code") if isinstance(data, dict) else None,
                         resp.text[:300])
    try:
        message_id = str(resp.json().get("id") or "")
    except ValueError as exc:
        raise httpx.TransportError("QQ returned an unparseable delivery acknowledgement") from exc
    if not message_id:
        raise httpx.TransportError("QQ delivery acknowledgement did not contain a message ID")
    return message_id


def _msg_body(content: str, msg_id: str | None, msg_seq: int | None = None,
              is_wakeup: bool = False) -> dict:
    # Keep the legacy transport flag for callers that still pass it. The
    # application no longer schedules wakeup messages; current push traffic
    # uses the existing passive or configured push paths.
    body: dict = {"msg_type": 0, "content": content}
    if msg_id:
        body["msg_id"] = msg_id
        if msg_seq is not None:
            body["msg_seq"] = msg_seq
    if is_wakeup:
        body["is_wakeup"] = True
    return body


async def send_group_text(group_openid: str, content: str,
                          msg_id: str | None = None,
                          msg_seq: int | None = None,
                          is_wakeup: bool = False,
                          bot: dict | None = None) -> str:
    """发一条文本消息到群（msg_type=0）。成功返回消息 id。

    msg_id 非空 = 被动回复（@机器人 5 分钟内有效），否则为主动消息。
    同一 msg_id 多次回复必须递增 msg_seq（平台去重），每条 @ 最多 5 次。
    is_wakeup 仅保留为旧调用方的传输兼容参数；应用自身不再调度召回摘要。
    主动消息权限及频控由该机器人账户的 API 反馈决定。
    平台要求主动消息发送时机器人保持 WS 网关在线（botpy 桥容器负责）。
    """
    return await _post_message(
        f"/v2/groups/{group_openid}/messages",
        _msg_body(content, msg_id, msg_seq, is_wakeup), bot)


async def send_group_media(group_openid: str, file_info: str,
                           msg_id: str | None = None,
                           msg_seq: int | None = None,
                           content: str | None = None,
                           bot: dict | None = None) -> str:
    """发一条富媒体消息到群（msg_type=7，需先上传拿 file_info）。"""
    body: dict = {"msg_type": 7, "media": {"file_info": file_info}}
    if content:
        body["content"] = content
    if msg_id:
        body["msg_id"] = msg_id
        if msg_seq is not None:
            body["msg_seq"] = msg_seq
    return await _post_message(
        f"/v2/groups/{group_openid}/messages", body, bot)


async def upload_group_file(group_openid: str, file_type: int,
                            url: str, bot: dict | None = None) -> str:
    """URL 上传富媒体到群，返回 file_info（用于 send_group_media）。

    file_type：1=图片(png/jpg) 2=视频(mp4) 3=语音(silk) 4=文件。
    url 必须公网可访问（平台服务器会来拉取）。软限：图 20MB / 视频 30MB。
    """
    return await _upload_group_file(group_openid, {
        "file_type": file_type, "url": url, "srv_send_msg": False,
    }, bot)


async def upload_group_file_data(group_openid: str, file_type: int,
                                 data: bytes,
                                 bot: dict | None = None) -> str:
    """Upload local bytes directly. Uploading alone never sends a group message."""
    return await _upload_group_file(group_openid, {
        "file_type": file_type,
        "file_data": base64.b64encode(data).decode("ascii"),
        "srv_send_msg": False,
    }, bot)


async def _upload_group_file(group_openid: str, body: dict,
                             bot: dict | None = None) -> str:
    return await _upload_file(group_openid, body, "groups", bot)


async def upload_c2c_file_data(openid: str, data: bytes,
                               bot: dict | None = None) -> str:
    return await _upload_file(openid, {"file_type": 1,
        "file_data": base64.b64encode(data).decode("ascii"), "srv_send_msg": False},
        "users", bot)


async def _upload_file(group_openid: str, body: dict, target_kind: str,
                        bot: dict | None = None) -> str:
    import hashlib
    import json
    token = await _get_token(bot)
    cache_key = hashlib.sha256(json.dumps([(bot or {}).get("app_id"), token,
        group_openid, target_kind, body], sort_keys=True).encode()).hexdigest()
    cached = _uploads.get(cache_key)
    if cached and cached[1] > time.time():
        return cached[0]
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{API_BASE}/v2/{target_kind}/{group_openid}/files",
            json=body,
            headers={"Authorization": f"QQBot {token}",
                     "Content-Type": "application/json"},
        )
    if resp.status_code != 200:
        try:
            data = resp.json()
        except Exception:
            data = {}
        raise QqApiError(
            data.get("code") if isinstance(data, dict) else None,
            f"上传失败: {resp.text[:200]}")
    file_info = resp.json().get("file_info")
    if not file_info:
        raise QqApiError(None, f"上传响应无 file_info: {resp.text[:200]}")
    ttl = resp.json().get("ttl")
    if isinstance(ttl, (int, float)) and ttl > 30:
        if len(_uploads) >= 512:
            _uploads.clear()
        _uploads[cache_key] = (str(file_info), time.time() + ttl - 30)
    return str(file_info)


async def send_c2c_text(openid: str, content: str,
                        msg_id: str | None = None,
                        msg_seq: int | None = None,
                        bot: dict | None = None) -> str:
    """发一条文本到 C2C（用户私聊机器人）会话。结构与群消息一致。

    主动 C2C 消息平台配额比群更紧（用户侧月度上限），被动回复不受限。
    """
    return await _post_message(
        f"/v2/users/{openid}/messages", _msg_body(content, msg_id, msg_seq),
        bot)


async def send_c2c_media(openid: str, file_info: str, msg_id: str | None = None,
                         msg_seq: int | None = None, content: str | None = None,
                         bot: dict | None = None) -> str:
    body: dict = {"msg_type": 7, "media": {"file_info": file_info}}
    if content:
        body["content"] = content
    if msg_id:
        body.update(msg_id=msg_id, msg_seq=msg_seq or 1)
    return await _post_message(f"/v2/users/{openid}/messages", body, bot)


# ---------------- 回调验签（Ed25519）----------------
# 官方算法（bot.q.qq.com/wiki .../interface-framework/sign.html）：
#   1. seed = AppSecret 按「翻倍直到 >= 32 字节」取前 32 字节
#   2. 由 seed 派生 Ed25519 公钥（腾讯持私钥签名，我们验签）
#   3. 签名体 = X-Signature-Timestamp 的字符串 + 原始 body 字节
#   4. X-Signature-Ed25519 头是签名 hex（64 字节）

def _seed_from_secret(secret: str) -> bytes:
    b = secret.encode("utf-8")
    if not b:
        return b""
    while len(b) < 32:
        b = b + b
    return b[:32]


def public_key_fingerprint(secret: str) -> str:
    """密钥派生出的 Ed25519 公钥前 8 位 hex。

    公钥是可公开的（腾讯那侧也用它验签），拿它当日志指纹既不泄密，又能让
    「密钥填错/填串」在日志里一眼可见 —— 只看密文长度是看不出来的。
    """
    seed = _seed_from_secret(secret)
    if not seed:
        return "-"
    pub = Ed25519PrivateKey.from_private_bytes(seed).public_key()
    return pub.public_bytes_raw().hex()[:8]


def verify_callback(secret: str, timestamp: str, body: bytes,
                    signature_hex: str) -> bool:
    """校验腾讯事件回调的 Ed25519 签名。任何一环缺失/不匹配都拒绝。"""
    if not secret or not timestamp or not signature_hex:
        return False
    try:
        sig = bytes.fromhex(signature_hex)
    except ValueError:
        return False
    if len(sig) != 64:
        return False
    seed = _seed_from_secret(secret)
    if not seed:
        return False
    pub = Ed25519PrivateKey.from_private_bytes(seed).public_key()
    try:
        pub.verify(sig, timestamp.encode("utf-8") + body)
        return True
    except InvalidSignature:
        return False


def sign_validation(secret: str, event_ts: str, plain_token: str) -> str:
    """op=13 回调地址验证的应答签名。

    平台在管理端保存回调地址时发 {"op":13,"d":{plain_token,event_ts}}，
    机器人须回 {"plain_token":..., "signature":...}。签名体顺序（官方
    event-emit.html 的 Go 示例）：event_ts 在前 + plain_token 在后，
    用 AppSecret 派生的 Ed25519 私钥签名后 hex 编码。

    官方 DEMO（secret=DG5g3B4j9X2KOErG）已逐字节对拍验证，测试见
    test_qqbot.py::test_op13_validation_official_demo。
    """
    seed = _seed_from_secret(secret)
    if not seed or not event_ts or not plain_token:
        raise QqApiError(None, "validation 签名参数缺失")
    priv = Ed25519PrivateKey.from_private_bytes(seed)
    return priv.sign(event_ts.encode("utf-8")
                     + plain_token.encode("utf-8")).hex()
