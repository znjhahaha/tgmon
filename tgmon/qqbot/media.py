"""QQ 富媒体消息辅助 —— 图片/视频上传与发送。

图片优先使用 file_data 直接上传 JPEG，不需要外部图床。2026-09-05 线上
验证：3 图合成的 555 KB 长图直接上传约 0.7 秒；同一图片经 GitHub raw
拉取两次均报 850027 超时。直接上传失败时才使用既有 GitHub / 本站 URL 回退。

视频：归档在 media/private/（鉴权目录，不公开），同样给平台拉不动，
签名 URL 方案保留（国内部署可用），拉取失败降级给 TG 深链 ——
视频太大不适合 GitHub contents API（base64 上传，软限约 25MB）。

失败一律返回 None 让调用方降级为纯文本 —— 媒体发不出不该丢整条回复。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import logging
import os
import tempfile
import time
import uuid
from datetime import datetime

import httpx

from .. import settings
from . import client
from .client import QqApiError

logger = logging.getLogger(__name__)

# 签名链接有效期（秒）。平台拉取通常几秒内完成，30 分钟绰绰有余
SIGNED_URL_TTL = 1800

# GitHub API。contents API 单文件上限 100MB，这里图片几十~几百 KB
GITHUB_API = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"
# 中转目录前缀（仓库内），按日期分子目录方便清理
RELAY_DIR = "qq-relay"


def compose_album(thumb_paths: list[str]) -> str:
    """Render every photo in order to one bounded JPEG, without cropping.

    QQ's group media API accepts one file_info per message. A vertical contact
    sheet retains the whole Telegram album without consuming one reply per photo.
    Missing/corrupt/private files fail the whole album instead of dropping photos.
    """
    from PIL import Image, ImageOps
    from ..paths import MEDIA_DIR

    if not thumb_paths:
        raise ValueError("图片组为空")
    base = MEDIA_DIR.resolve()
    sources = []
    digest = hashlib.sha256(b"qq-album-v1")
    for relative in thumb_paths:
        path = (base / relative).resolve()
        if not path.is_relative_to(base) or path.relative_to(base).parts[0].lower() == "private":
            raise ValueError("图片不在公开媒体目录内")
        try:
            stat = path.stat()
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                width, height = image.size
                if image.getexif().get(274) in (5, 6, 7, 8):
                    width, height = height, width
                if width <= 0 or height <= 0 or width * height > 40_000_000:
                    raise ValueError("原图尺寸超限")
            sources.append((path, width, height))
            digest.update(f"{relative}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode("utf-8"))
        except Exception as exc:
            raise ValueError(f"图片无法完整读取: {relative}") from exc

    folder = base / "qq-albums" / datetime.now().strftime("%Y%m%d")
    if not folder.resolve().is_relative_to(base):
        raise ValueError("合图目录不在媒体目录内")
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / f"{digest.hexdigest()[:24]}.jpg"
    if output.is_file():
        return output.relative_to(base).as_posix()

    width = min(1200, max(w for _, w, _ in sources))
    gap = 8
    sizes = [(min(width, w), max(1, round(h * min(width, w) / w))) for _, w, h in sources]
    available = 16000 - gap * (len(sizes) - 1)
    if available < len(sizes):
        raise ValueError("图片组过大，无法完整合图")
    scale = min(1.0, available / sum(h for _, h in sizes))
    sizes = [(max(1, int(w * scale)), max(1, int(h * scale))) for w, h in sizes]
    width = max(1, int(width * scale))
    height = sum(h for _, h in sizes) + gap * (len(sizes) - 1)
    canvas = Image.new("RGB", (width, height), "white")
    y = 0
    for (path, _, _), (w, h) in zip(sources, sizes):
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image).convert("RGBA")
            image = image.resize((w, h), Image.Resampling.LANCZOS)
            canvas.paste(image, ((width - w) // 2, y), image)
        y += h + gap

    encoded = io.BytesIO()
    for quality in (90, 80, 70):
        encoded.seek(0)
        encoded.truncate()
        canvas.save(encoded, "JPEG", quality=quality, optimize=True)
        if encoded.tell() <= 18 * 1024 * 1024:
            break
    else:
        raise ValueError("完整合图超过 QQ 图片大小上限")
    # Concurrent requests/processes can render the same album. Publish atomically.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=folder, suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            handle.write(encoded.getvalue())
        os.replace(temporary, output)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
        canvas.close()
    return output.relative_to(base).as_posix()


async def upload_album(group_openid: str, thumb_paths: list[str],
                       raise_fatal: bool = False,
                       bot: dict | None = None) -> str | None:
    """Upload one media file for a whole album; keep single-photo uploads intact."""
    if not thumb_paths:
        return None
    if len(thumb_paths) == 1:
        return await upload_image(group_openid, thumb_paths[0],
                                  raise_fatal=raise_fatal, bot=bot)
    try:
        path = await asyncio.to_thread(compose_album, thumb_paths)
    except Exception as exc:
        logger.warning("图片组合成失败（未发送残缺相册）: %s", exc)
        return None
    return await upload_image(group_openid, path, raise_fatal=raise_fatal, bot=bot)


def _signing_key() -> bytes:
    """HMAC 签名密钥：复用 crypto 的主密钥（secret.key）。

    两边（生成签名 admin 进程 / 校验签名 admin 进程）读同一个文件，
    不引入新密钥。
    """
    from ..crypto import _load_key
    return _load_key()


def make_video_sig(mid: int, expires: int) -> str:
    msg = f"{mid}:{expires}".encode()
    return hmac.new(_signing_key(), msg, hashlib.sha256).hexdigest()[:32]


def video_signed_url(mid: int) -> str | None:
    """为归档视频生成限时签名 URL（供 QQ 平台拉取）。"""
    from .. import outputs
    base = outputs.base_url()
    if not base:
        logger.warning("BASE_URL 未配置，无法生成视频签名 URL")
        return None
    expires = int(time.time()) + SIGNED_URL_TTL
    sig = make_video_sig(mid, expires)
    return f"{base}/media/qq-video/{mid}/{expires}/{sig}"


def image_public_url(thumb_path: str | None) -> str | None:
    """图片的公网 URL（media/ 静态目录，Caddy 已放行）。"""
    if not thumb_path:
        return None
    from .. import outputs
    url = outputs.media_url(thumb_path)
    # media_url 依赖 BASE_URL；没配就没有公网地址可给
    if not url or not str(settings.get("BASE_URL") or ""):
        return None
    return url


# ---------------- GitHub 中转 ----------------

def _relay_conf() -> tuple[str | None, str | None]:
    """(repo "owner/name", token)。任一未配置返回 (None, None)。"""
    repo = str(settings.get("GITHUB_RELAY_REPO") or "").strip().strip("/")
    token = str(settings.get("GITHUB_RELAY_TOKEN") or "").strip()
    if not repo or not token or "/" not in repo:
        return None, None
    return repo, token


def relay_enabled() -> bool:
    return _relay_conf() != (None, None)


def _jpeg_bytes(thumb_path: str | None) -> bytes | None:
    """磁盘上的 webp（或 jpg/png）转 JPEG 字节。平台 file_type=1 只认 png/jpg。"""
    if not thumb_path:
        return None
    from ..paths import MEDIA_DIR
    p = MEDIA_DIR / thumb_path
    try:
        from PIL import Image
        with Image.open(p) as img:
            img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=88)
            return buf.getvalue()
    except Exception as e:
        logger.warning("图片读取/转换失败 %s: %s", p, e)
        return None


async def github_put(jpeg: bytes, rel_path: str) -> str | None:
    """上传字节到中转仓库，返回 raw URL。失败返回 None。

    实测（2026-09）：PUT 后 raw URL 对腾讯机房立即可拉（连跑 3/3 成功），
    偶发 850027 由 upload_image 的整链重试兜底，不做服务器侧自检
    （香港→Fastly 的 HEAD 不通，自检只会白等 6 秒）。
    """
    repo, token = _relay_conf()
    if not repo:
        return None
    body = {
        "message": f"relay {rel_path}",
        "content": base64.b64encode(jpeg).decode(),
        "branch": "main",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.put(
                f"{GITHUB_API}/repos/{repo}/contents/{rel_path}",
                json=body,
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/vnd.github+json",
                         "X-GitHub-Api-Version": "2022-11-28"})
        if r.status_code not in (200, 201):
            logger.warning("GitHub 中转上传失败 %s: HTTP %s %s",
                           rel_path, r.status_code, r.text[:150])
            return None
        return f"{RAW_BASE}/{repo}/main/{rel_path}"
    except Exception as e:
        logger.warning("GitHub 中转上传异常 %s: %s", rel_path, e)
        return None


async def github_relay_url(thumb_path: str | None) -> str | None:
    """thumb_path → GitHub raw URL（webp 转 jpg 上传）。"""
    jpeg = _jpeg_bytes(thumb_path)
    if not jpeg:
        return None
    day = datetime.now().strftime("%Y%m%d")
    rel_path = f"{RELAY_DIR}/{day}/{uuid.uuid4().hex[:12]}.jpg"
    return await github_put(jpeg, rel_path)


async def cleanup_github_relay(max_age_days: int = 1) -> int:
    """删除中转仓库里超期的图片（每天维护循环调用）。

    contents API 按文件删（列目录拿 sha → DELETE）。一天量级几十张，
    逐个删完全可接受。返回删除的文件数。
    """
    repo, token = _relay_conf()
    if not repo:
        return 0
    cutoff = datetime.utcnow().timestamp() - max_age_days * 86400
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    removed = 0
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(f"{GITHUB_API}/repos/{repo}/contents/{RELAY_DIR}",
                            headers=headers)
            if r.status_code == 404:
                return 0  # 目录还不存在（没发过图）
            if r.status_code != 200:
                logger.warning("GitHub 中转清理：列目录失败 HTTP %s", r.status_code)
                return 0
            for day_dir in r.json():  # [{name: "20260904", ...}, ...]
                name = day_dir.get("name", "")
                try:
                    day_ts = datetime.strptime(name, "%Y%m%d").timestamp()
                except ValueError:
                    continue
                if day_ts >= cutoff:
                    continue
                r2 = await c.get(day_dir.get("url", ""), headers=headers)
                if r2.status_code != 200:
                    continue
                for f in r2.json():
                    if f.get("type") != "file":
                        continue
                    r3 = await c.delete(
                        f"{GITHUB_API}/repos/{repo}/contents/{RELAY_DIR}/{name}/{f['name']}",
                        json={"message": f"cleanup {name}/{f['name']}",
                              "sha": f["sha"], "branch": "main"},
                        headers=headers)
                    if r3.status_code == 200:
                        removed += 1
    except Exception as e:
        logger.warning("GitHub 中转清理异常: %s", e)
    if removed:
        logger.info("GitHub 中转清理：删除 %d 张过期图片", removed)
    return removed


async def upload_image(group_openid: str, thumb_path: str | None,
                       raise_fatal: bool = False,
                       bot: dict | None = None) -> str | None:
    """图片 → file_info。失败返回 None（调用方降级）。

    优先 file_data 直接上传本地 JPEG。失败时使用配置的 GitHub 中转，
    未配置中转则退回本站直链。URL 拉取失败时整链重试一次。

    raise_fatal=True（主动推送用）：「不在群」「无主动权限」这类致命
    错误不降级而是原样上抛，让调用方走停群 / 终结投递逻辑。
    """
    def _fatal(e: QqApiError) -> bool:
        return (raise_fatal
                and (e.code in client.ERR_NOT_IN_GROUP
                     or e.code == client.ERR_NO_PERMISSION))

    jpeg = await asyncio.to_thread(_jpeg_bytes, thumb_path)
    if jpeg:
        try:
            return await client.upload_group_file_data(group_openid, 1, jpeg, bot=bot)
        except QqApiError as exc:
            if _fatal(exc):
                raise
            if exc.code in client.ERR_NOT_IN_GROUP or exc.code == client.ERR_NO_PERMISSION:
                return None
            logger.warning("图片直接上传失败，尝试 URL 回退: %s", exc)
        except Exception as exc:
            logger.warning("图片直接上传异常，尝试 URL 回退: %s", exc)

    if relay_enabled():
        for attempt in range(2):
            url = await github_relay_url(thumb_path)
            if not url:
                return None
            try:
                return await client.upload_group_file(group_openid, 1, url, bot=bot)
            except QqApiError as e:
                if _fatal(e):
                    raise
                if attempt == 0:
                    logger.warning("图片上传失败（GitHub 中转，重试一次）: %s", e)
                    continue
                logger.warning("图片上传失败（GitHub 中转，降级为不发图）: %s", e)
                return None
        return None
    # 无中转配置：本站直链（境外部署会 850027 超时，仅国内部署可用）
    url = image_public_url(thumb_path)
    if not url:
        return None
    try:
        return await client.upload_group_file(group_openid, 1, url, bot=bot)
    except QqApiError as e:
        if _fatal(e):
            raise
        logger.warning("图片上传失败（本站直链，降级为不发图）: %s", e)
        return None


async def upload_video(group_openid: str, mid: int,
                       bot: dict | None = None) -> str | None:
    """归档视频 → file_info（签名限时 URL）。失败返回 None。"""
    url = video_signed_url(mid)
    if not url:
        return None
    try:
        return await client.upload_group_file(group_openid, 2, url, bot=bot)
    except QqApiError as e:
        logger.warning("视频上传失败（降级为不发视频）: %s", e)
        return None
