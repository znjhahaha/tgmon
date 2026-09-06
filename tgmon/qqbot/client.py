"""QQ 开放平台 API 客户端：access_token 管理、群消息发送、回调验签。

文档：https://bot.q.qq.com/wiki/develop/api-v2
认证：AppID + AppSecret 换 access_token（约 7200s），请求头 QQBot <token>。
"""
from __future__ import annotations

import base64
import logging
import time

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from .. import settings

logger = logging.getLogger(__name__)

TOKEN_URL = "https://api.bot.qq.com/app/getAppAccessToken"
API_BASE = "https://api.bot.qq.com"

# token 进程级缓存。admin / worker 各自持有一份，互不干扰
_token: str | None = None
_token_exp: float = 0.0
_token_lock_pid: int = 0  # fork 场景防御（本项目用不到，保险）

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


def bot_secret() -> str:
    """Bot Secret —— 管理端「机器人密钥」(AppSecret)。

    官方文档里 Ed25519 签名种子与 API 鉴权用的是**同一个**凭证：sign.html
    的种子取自「开发者平台的 Bot Secret」，getAppAccessToken 的 clientSecret
    也是它。所以回调验签和换 token 必须读同一处，别再各读各的 ——
    2026-09-04 的回调校验失败正是因为验签那侧改读了「机器人令牌」，而那个
    值拿去换 token 会被平台判 100016 invalid appid or secret。

    判断密钥是否填对的现成办法：能换到 access_token 的那个才是签名密钥。
    """
    return str(settings.get("QQ_APP_SECRET") or "").strip()


def _credentials() -> tuple[str, str]:
    return str(settings.get("QQ_APP_ID") or "").strip(), bot_secret()


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


async def _get_token() -> str:
    """带缓存的 token 获取。无效凭证直接抛 QqApiError。"""
    global _token, _token_exp, _token_lock_pid
    import os
    if _token_lock_pid != os.getpid():
        _token, _token_exp, _token_lock_pid = None, 0.0, os.getpid()
    if _token and time.time() < _token_exp:
        return _token
    app_id, app_secret = _credentials()
    if not app_id or not app_secret:
        raise QqApiError(None, "未配置 QQ_APP_ID / QQ_APP_SECRET")
    _token, _token_exp = await _fetch_token(app_id, app_secret)
    return _token


def invalidate_token() -> None:
    """凭证改动 / token 失效时清缓存。"""
    global _token, _token_exp
    _token, _token_exp = None, 0.0


async def check_credentials() -> tuple[bool, str]:
    """自检：回调验签用的那个密钥，能不能换到 access_token。

    因为签名种子和 API 鉴权是同一个凭证，这一次请求同时证明了两件事。
    没有它的话，密钥填错只会表现为腾讯管理端一句「校验失败」，本地无从
    分辨是密钥错还是算法错 —— 2026-09-04 就是这样绕了一大圈。

    返回 (是否有效, 说明)。
    """
    app_id, secret = _credentials()
    if not app_id or not secret:
        return False, "AppID 或机器人密钥未填"
    try:
        await _fetch_token(app_id, secret)
    except QqApiError as e:
        return False, f"{e.code}: {e.message}"[:160]
    except Exception as e:  # 网络层等
        return False, f"请求失败: {e}"[:160]
    return True, "有效"


async def _post_message(path: str, body: dict) -> str:
    """群/C2C 消息发送的公共路径。成功返回消息 id。

    token 失效（11010 等）时清缓存抛出，调用方按需重试一次。
    """
    token = await _get_token()
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
            invalidate_token()
        raise QqApiError(data.get("code") if isinstance(data, dict) else None,
                         resp.text[:300])
    return str(resp.json().get("id") or "")


def _msg_body(content: str, msg_id: str | None, msg_seq: int | None = None,
              is_wakeup: bool = False) -> dict:
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
                          is_wakeup: bool = False) -> str:
    """发一条文本消息到群（msg_type=0）。成功返回消息 id。

    msg_id 非空 = 被动回复（@机器人 5 分钟内有效），否则为主动消息。
    同一 msg_id 多次回复必须递增 msg_seq（平台去重），每条 @ 最多 5 次。
    is_wakeup=True 走互动召回通道（每天每群 1 条，需当天互动过）。
    主动消息受频控：每群 20/分钟、1000/天；Bot 全局 30-60/分钟。
    平台要求主动消息发送时机器人保持 WS 网关在线（botpy 桥容器负责）。
    """
    return await _post_message(
        f"/v2/groups/{group_openid}/messages",
        _msg_body(content, msg_id, msg_seq, is_wakeup))


async def send_group_media(group_openid: str, file_info: str,
                           msg_id: str | None = None,
                           msg_seq: int | None = None,
                           content: str | None = None) -> str:
    """发一条富媒体消息到群（msg_type=7，需先上传拿 file_info）。"""
    body: dict = {"msg_type": 7, "media": {"file_info": file_info}}
    if content:
        body["content"] = content
    if msg_id:
        body["msg_id"] = msg_id
        if msg_seq is not None:
            body["msg_seq"] = msg_seq
    return await _post_message(
        f"/v2/groups/{group_openid}/messages", body)


async def upload_group_file(group_openid: str, file_type: int,
                            url: str) -> str:
    """URL 上传富媒体到群，返回 file_info（用于 send_group_media）。

    file_type：1=图片(png/jpg) 2=视频(mp4) 3=语音(silk) 4=文件。
    url 必须公网可访问（平台服务器会来拉取）。软限：图 20MB / 视频 30MB。
    """
    return await _upload_group_file(group_openid, {
        "file_type": file_type, "url": url, "srv_send_msg": False,
    })


async def upload_group_file_data(group_openid: str, file_type: int,
                                 data: bytes) -> str:
    """Upload local bytes directly. Uploading alone never sends a group message."""
    return await _upload_group_file(group_openid, {
        "file_type": file_type, "file_data": base64.b64encode(data).decode("ascii"),
        "srv_send_msg": False,
    })


async def _upload_group_file(group_openid: str, body: dict) -> str:
    return await _upload_file(group_openid, body, "groups")


async def upload_c2c_file_data(openid: str, data: bytes) -> str:
    return await _upload_file(openid, {"file_type": 1,
        "file_data": base64.b64encode(data).decode("ascii"), "srv_send_msg": False}, "users")


async def _upload_file(group_openid: str, body: dict, target_kind: str) -> str:
    token = await _get_token()
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
    return str(file_info)


async def send_c2c_text(openid: str, content: str,
                        msg_id: str | None = None,
                        msg_seq: int | None = None) -> str:
    """发一条文本到 C2C（用户私聊机器人）会话。结构与群消息一致。

    主动 C2C 消息平台配额比群更紧（用户侧月度上限），被动回复不受限。
    """
    return await _post_message(
        f"/v2/users/{openid}/messages", _msg_body(content, msg_id, msg_seq))


async def send_c2c_media(openid: str, file_info: str, msg_id: str | None = None,
                         msg_seq: int | None = None, content: str | None = None) -> str:
    body = {"msg_type": 7, "media": {"file_info": file_info}}
    if content:
        body["content"] = content
    if msg_id:
        body.update(msg_id=msg_id, msg_seq=msg_seq or 1)
    return await _post_message(f"/v2/users/{openid}/messages", body)


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
