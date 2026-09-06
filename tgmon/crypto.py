"""DB 内敏感字段的对称加密，以及后台登录口令的哈希。

为什么加密：api_hash / api_key / HMAC 密钥都存 DB。DB 文件会进备份、会被
docker cp 出来、出错时可能被贴进聊天。加一层 Fernet 让「文件泄露」不等于
「密钥泄露」。密钥本身在 secret.key（已 gitignore）或 TGMON_SECRET_KEY。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets

from cryptography.fernet import Fernet, InvalidToken

from .paths import SECRET_KEY_FILE

logger = logging.getLogger(__name__)

_ENC_PREFIX = "enc:v1:"
_fernet: Fernet | None = None


def _load_key() -> bytes:
    env = (os.getenv("TGMON_SECRET_KEY") or "").strip()
    if env:
        # 允许直接给 Fernet key，也允许给任意口令（派生）
        try:
            Fernet(env.encode())
            return env.encode()
        except Exception:
            digest = hashlib.sha256(env.encode("utf-8")).digest()
            return base64.urlsafe_b64encode(digest)

    if SECRET_KEY_FILE.exists():
        return SECRET_KEY_FILE.read_bytes().strip()

    key = Fernet.generate_key()
    SECRET_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    SECRET_KEY_FILE.write_bytes(key)
    try:
        os.chmod(SECRET_KEY_FILE, 0o600)
    except OSError:
        pass
    logger.warning("已生成新的加密主密钥 %s —— 换机器时必须一起搬走", SECRET_KEY_FILE)
    return key


def _f() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_key())
    return _fernet


def encrypt(plain: str | None) -> str | None:
    """加密。空值原样返回，方便「留空表示不改」的表单语义。"""
    if plain is None or plain == "":
        return plain
    if plain.startswith(_ENC_PREFIX):  # 幂等，避免二次加密
        return plain
    return _ENC_PREFIX + _f().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(stored: str | None) -> str | None:
    """解密。非密文原样返回 —— 兼容手工塞进 DB 的明文。"""
    if not stored:
        return stored
    if not stored.startswith(_ENC_PREFIX):
        return stored
    try:
        return _f().decrypt(stored[len(_ENC_PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken:
        logger.error("解密失败：主密钥与写入时不一致。该字段需在后台重新填写")
        return None


def mask(stored: str | None) -> str:
    """给界面看的遮罩。只写不读：已存的值显示为点，不回显原文。"""
    return "••••••••" if stored else ""


# ---------------- 后台登录口令 ----------------

# scrypt 参数：n=2^15, r=8 需要 128*n*r = 32 MB。OpenSSL 默认 maxmem 上限正好
# 是 32 MB 且判断是「严格小于」，所以必须显式放宽，否则直接抛
# "memory limit exceeded"。给 96 MB 留足余量（4 G 内存的机器上登录不频繁，够用）。
_SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1, "dklen": 32, "maxmem": 96 * 1024 * 1024}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, dk_hex = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"),
                            salt=bytes.fromhex(salt_hex), **_SCRYPT)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False
