"""媒体处理：默认只产出缩略图；视频归档（下载 + 转码）是显式开启的可选路径。

磁盘只剩 16 G、回大陆单连接 61 KB/s —— 默认策略视频只取 thumb（几十 KB），
推送时给缩略图 + TG 原链接。归档是频道级开关 keep_video + 全局开关
VIDEO_ARCHIVE_ENABLED 双闸门，开了才会真的下载原片并全片转码。
"""
from __future__ import annotations

import asyncio
import io
import logging
import subprocess
import tempfile
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from telethon.tl.types import (
    Document, DocumentAttributeFilename, DocumentAttributeVideo,
)

from . import imghash, settings
from .db import session_scope
from .models import WorkerState
from .paths import MEDIA_DIR, VIDEO_DIR

logger = logging.getLogger(__name__)

# 转码串行：3.9G 内存的机器并发跑两个 ffmpeg 会 OOM。全局唯一信号量
_VIDEO_SEM = asyncio.Semaphore(1)

# 单条视频转码最长等这么久（秒）。180s 的 720p veryfast 通常 1-3 分钟
_FFMPEG_TIMEOUT = 900


def _set_action(text: str) -> None:
    """把当前动作写进 worker_state.current_action，概览页可见。"""
    try:
        with session_scope() as s:
            row = s.get(WorkerState, 1)
            if row is not None:
                row.current_action = text or ""
    except Exception:
        pass


@dataclass
class MediaOut:
    kind: str
    source_message_id: int | None = None
    source_identity: str | None = None
    status: str = "ready"
    error: str | None = None
    sha256: str | None = None
    original_path: str | None = None
    original_bytes: int = 0
    thumb_path: str | None = None   # 相对 MEDIA_DIR
    thumb_bytes: int = 0
    width: int | None = None
    height: int | None = None
    duration: int | None = None
    orig_bytes: int = 0
    mime: str | None = None
    phash: str | None = None
    dhash: str | None = None
    # 剧透标记（TG 的 media spoiler：点开前模糊的封面）
    has_spoiler: bool = False
    # 归档视频。video_path 相对 VIDEO_DIR；None = 没归档
    video_path: str | None = None
    video_bytes: int = 0
    video_status: str | None = None   # ok / skipped_* / failed / None(未尝试)


def describe(message) -> MediaOut | None:
    photo = getattr(message, "photo", None)
    doc = getattr(message, "document", None)
    if photo is None and doc is None:
        return None
    item = photo or doc
    mime = getattr(doc, "mime_type", "") or ""
    kind = "photo" if photo is not None else "video" if (
        getattr(message, "video", None) is not None or mime.startswith("video/")) else "document"
    attrs = _video_attrs(doc) if doc is not None else None
    return MediaOut(kind=kind, source_message_id=message.id,
        source_identity=f"{kind}:{getattr(item, 'id', '')}",
        orig_bytes=int(getattr(item, "size", 0) or 0), mime=mime or None,
        width=getattr(attrs, "w", None), height=getattr(attrs, "h", None),
        duration=int(getattr(attrs, "duration", 0) or 0) or None,
        has_spoiler=_media_spoiled(message), status="queued",
        video_status="queued" if kind == "video" else None)


def _target_dir(channel_id: int) -> Path:
    d = MEDIA_DIR / str(channel_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_webp(raw: bytes, dest: Path, max_width: int) -> tuple[int, int, int] | None:
    """存 WebP 并下采样。返回 (字节数, 宽, 高)。"""
    try:
        with Image.open(io.BytesIO(raw)) as img:
            img.load()
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGB")
            w, h = img.size
            if max_width and w > max_width:
                h = max(1, int(h * max_width / w))
                w = max_width
                img = img.resize((w, h), Image.LANCZOS)
            quality = int(settings.get("WEBP_QUALITY") or 80)
            temp = dest.with_suffix(".partial")
            img.save(temp, "WEBP", quality=quality, method=4)
            temp.replace(dest)
        return dest.stat().st_size, w, h
    except Exception as e:
        logger.warning("保存 WebP 失败 %s: %s", dest, e)
        dest.with_suffix(".partial").unlink(missing_ok=True)
        return None


def _video_attrs(doc: Document) -> DocumentAttributeVideo | None:
    for a in getattr(doc, "attributes", []) or []:
        if isinstance(a, DocumentAttributeVideo):
            return a
    return None


def _filename(doc: Document) -> str | None:
    for a in getattr(doc, "attributes", []) or []:
        if isinstance(a, DocumentAttributeFilename):
            return a.file_name
    return None


async def _ffmpeg_first_frame(client, doc: Document) -> bytes | None:
    """无 thumb 时的兜底：流式拉前 3 MB → ffmpeg 抽首帧 → 丢弃视频数据。

    **只对 moov atom 在文件头部的 mp4 有效**（即 faststart / web 优化过的）。
    实测：同一段视频，moov 在尾部时 ffmpeg 对前 3 MB 报 "moov atom not found"
    直接失败；加了 faststart 后只给前 1 MB 就能抽出首帧。

    TG 自己转码过的视频通常是 faststart 的；以 document 形式原样转发的可能不是。
    抽不出来就返回 None（那条消息没有缩略图），不影响入库 —— 反正还有 TG 深链。
    """
    if not settings.get("FFMPEG_FALLBACK"):
        return None
    chunk = 512 * 1024
    try:
        buf = bytearray()
        async for part in client.iter_download(doc, request_size=chunk, limit=6):
            buf.extend(part)
            if len(buf) >= 3 * 1024 * 1024:
                break
        if not buf:
            return None
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "head.bin"
            out = Path(td) / "frame.jpg"
            src.write_bytes(bytes(buf))
            proc = await asyncio.to_thread(subprocess.run,
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-i", str(src), "-frames:v", "1", "-q:v", "3", str(out)],
                capture_output=True, timeout=45,
            )
            if out.exists() and out.stat().st_size > 0:
                return out.read_bytes()
            logger.debug("ffmpeg 抽帧失败: %s", proc.stderr.decode("utf-8", "ignore")[:300])
    except Exception as e:
        logger.debug("流式抽帧失败: %s", e)
    return None


def _media_spoiled(message) -> bool:
    """TG 的媒体剧透标记（点开前模糊的封面图）。

    Telethon 1.3x 的自定义 Message 对象暴露为 `media_spoiler`；老版本 /
    其他形状兜底查 `message.media.spoiler`。字段名对不上就当没有 ——
    最多是后台少个模糊效果，不影响入库。
    """
    v = getattr(message, "media_spoiler", None)
    if v is not None:
        return bool(v)
    return bool(getattr(getattr(message, "media", None), "spoiler", False))


async def _archive_video(client, message, channel_id: int, out: MediaOut,
                         video_max_mb: int) -> None:
    """下载源视频 → ffmpeg 转码 720p mp4 → 存 VIDEO_DIR。

    只改 out.video_* 字段，任何失败都不抛 —— 归档是锦上添花，
    不能因为它挡住正常入库。闸门（顺序）：全局开关 → 频道开关 →
    大小（min(全局, 频道)）→ 时长。
    """
    out.video_status = "skipped_disabled"
    out.status = "ready"
    if not settings.get("VIDEO_ARCHIVE_ENABLED"):
        return
    # 大小上限：频道值（0=继承全局）与全局取小
    global_mb = int(settings.get("VIDEO_MAX_MB") or 0)
    caps = [value for value in (global_mb, video_max_mb) if value > 0]
    cap_mb = min(caps) if caps else 0
    if cap_mb and out.orig_bytes > cap_mb * 1024 * 1024:
        out.video_status = "skipped_size"
        out.error = f"源文件超过 {cap_mb} MB 归档限制"
        return
    duration_limit = int(settings.get("VIDEO_MAX_SECONDS") or 0)
    if duration_limit and out.duration and out.duration > duration_limit:
        out.video_status = "skipped_duration"
        out.error = f"视频超过 {duration_limit} 秒归档限制"
        return

    async with _VIDEO_SEM:          # 同一时刻只有一路转码
        dest_dir = VIDEO_DIR / str(channel_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        source_identity = getattr(getattr(message, "document", None), "id", message.id)
        src = dest_dir / f"{message.id}-{source_identity}.original.mp4"
        dest = dest_dir / f"{message.id}-{source_identity}.mp4"
        temp = dest.with_suffix(".partial.mp4")
        try:
            _set_action(f"下载视频 {channel_id}/{message.id}")
            if not src.exists():
                if not await asyncio.to_thread(_has_space, out.orig_bytes):
                    out.video_status, out.status, out.error = "waiting_space", "waiting_space", "磁盘空间或媒体配额不足"
                    return
                await _download_source(client, message, src, out.orig_bytes)
            if out.orig_bytes and src.stat().st_size != out.orig_bytes:
                raise RuntimeError("源文件大小校验失败")
            out.original_path = src.relative_to(VIDEO_DIR).as_posix()
            out.original_bytes = src.stat().st_size
            out.sha256 = await asyncio.to_thread(_hash_file, src)
            if await asyncio.to_thread(_playable, src):
                dest = src
            else:
                _set_action(f"转码视频 {channel_id}/{message.id}.mp4")
                await asyncio.to_thread(_transcode, src, temp)
                if not temp.is_file() or not temp.stat().st_size:
                    raise RuntimeError("播放版本为空")
                temp.replace(dest)
            out.video_path = dest.relative_to(VIDEO_DIR).as_posix()
            out.video_bytes = dest.stat().st_size
            out.video_status, out.status = "ok", "ready"
        except Exception as e:
            out.video_status, out.status, out.error = "failed", "failed", str(e)[:1000]
            logger.warning("视频归档失败 %s/%s: %s", channel_id, message.id, e)
            temp.unlink(missing_ok=True)
        finally:
            _set_action("")


def _has_space(size: int) -> bool:
    reserve = int(settings.get("MEDIA_FREE_RESERVE_MB") or 256) * 1048576
    if shutil.disk_usage(VIDEO_DIR).free < max(size, 1048576) * 2 + reserve:
        return False
    quota = int(settings.get("VIDEO_MAX_TOTAL_MB") or 0) * 1048576
    if quota:
        used = sum(p.stat().st_size for p in VIDEO_DIR.rglob("*") if p.is_file())
        if used + size > quota:
            return False
    return True


def _hash_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


async def _download_source(client, message, dest: Path, expected_size: int) -> None:
    partial = dest.with_suffix(dest.suffix + ".partial")
    chunk_size = 512 * 1024
    offset = (partial.stat().st_size // chunk_size * chunk_size) if partial.exists() else 0
    if hasattr(client, "iter_download"):
        with partial.open("r+b" if partial.exists() else "wb") as stream:
            stream.truncate(offset)
            stream.seek(offset)
            async for chunk in client.iter_download(message.document, offset=offset,
                                                     request_size=chunk_size):
                await asyncio.to_thread(stream.write, chunk)
            await asyncio.to_thread(stream.flush)
            await asyncio.to_thread(os.fsync, stream.fileno())
    else:
        await client.download_media(message, file=str(partial))
    size = partial.stat().st_size if partial.exists() else 0
    if not size or (expected_size and size != expected_size):
        raise RuntimeError(f"下载未完成: {size}/{expected_size} bytes")
    partial.replace(dest)


def _playable(src: Path) -> bool:
    proc = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                           "-of", "json", str(src)], capture_output=True, timeout=30, check=True)
    data = json.loads(proc.stdout)
    video = [s for s in data.get("streams", []) if s.get("codec_type") == "video"]
    audio = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    return bool(video and "mp4" in data.get("format", {}).get("format_name", "")
                and all(s.get("codec_name") == "h264" for s in video)
                and all(s.get("codec_name") in ("aac", "mp3") for s in audio))


def _transcode(src: Path, dest: Path) -> None:
    """同步转码，只在线程池里跑。

    scale=-2:min(H,ih)：高度压到 H，原片更矮时不放大（-2 保宽度为偶数）。
    min() 里的逗号在 ffmpeg filter 语法中要转义。faststart 把 moov 挪到
    文件头，浏览器边下边播不用等整个文件。
    """
    height = int(settings.get("VIDEO_HEIGHT") or 720)
    crf = int(settings.get("VIDEO_CRF") or 28)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(src),
         "-vf", f"scale=-2:min({height}\\,ih)",
         "-c:v", "libx264", "-crf", str(crf), "-preset", "veryfast",
         "-c:a", "aac", "-b:a", "64k",
         "-movflags", "+faststart",
         str(dest)],
        capture_output=True, timeout=_FFMPEG_TIMEOUT, check=True,
    )


async def process(client, message, channel_id: int, *,
                  keep_video: bool = False,
                  video_max_mb: int = 0,
                  defer_archive: bool = False) -> MediaOut | None:
    """处理一条消息的媒体。返回 None 表示这条没有需要保存的媒体。

    keep_video / video_max_mb 来自频道配置，只对视频生效。
    """
    if not getattr(message, "media", None):
        return None

    dest_dir = _target_dir(channel_id)
    identity = describe(message)
    asset = getattr(message, "photo", None) or getattr(message, "document", None)
    stem = f"{message.id}-{getattr(asset, 'id', message.id)}"
    spoiled = _media_spoiled(message)

    # ---------- 图片 ----------
    if getattr(message, "photo", None) is not None:
        raw = await client.download_media(message, file=bytes)
        if not raw:
            return MediaOut(kind="photo", source_message_id=message.id,
                            has_spoiler=spoiled, status="failed", error="图片下载为空")
        original = VIDEO_DIR / str(channel_id) / f"{message.id}-{getattr(message.photo, 'id', message.id)}.original"
        original.parent.mkdir(parents=True, exist_ok=True)
        if not await asyncio.to_thread(_has_space, len(raw)):
            return MediaOut(kind="photo", source_message_id=message.id, status="waiting_space",
                            error="磁盘空间或媒体配额不足")
        await asyncio.to_thread(_save_original, original, raw)
        dest = dest_dir / f"{stem}.webp"
        saved = await asyncio.to_thread(_save_webp, raw, dest, int(settings.get("PHOTO_WIDTH") or 1280))
        if saved is None:
            return MediaOut(kind="photo", source_message_id=message.id,
                            orig_bytes=len(raw), has_spoiler=spoiled, status="failed", error="图片校验失败")
        size, w, h = saved
        ph, dh = await asyncio.to_thread(imghash.hashes_for, dest)
        return MediaOut(kind="photo", source_message_id=message.id,
                        source_identity=identity.source_identity if identity else None,
                        thumb_path=f"{channel_id}/{dest.name}",
                        thumb_bytes=size, width=w, height=h,
                        orig_bytes=len(raw), mime="image/webp",
                        original_path=original.relative_to(VIDEO_DIR).as_posix(),
                        original_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                        phash=ph, dhash=dh, has_spoiler=spoiled)

    doc = getattr(message, "document", None)
    if doc is None:
        return None

    mime = getattr(doc, "mime_type", "") or ""
    is_video = getattr(message, "video", None) is not None or mime.startswith("video/")
    kind = "video" if is_video else "document"

    va = _video_attrs(doc) if is_video else None
    out = MediaOut(
        kind=kind,
        source_message_id=message.id,
        source_identity=identity.source_identity if identity else None,
        orig_bytes=getattr(doc, "size", 0) or 0,
        mime=mime or None,
        duration=int(va.duration) if va and va.duration else None,
        width=va.w if va else None,
        height=va.h if va else None,
        has_spoiler=spoiled,
    )

    # Previews are independent from source-file archiving.
    thumbs = getattr(doc, "thumbs", None)
    raw = None
    if thumbs:
        try:
            # thumb=-1 取最大那张，通常几十 KB
            raw = await client.download_media(message, file=bytes, thumb=-1)
        except Exception as e:
            logger.debug("下载 thumb 失败: %s", e)

    if not raw and is_video:
        raw = await _ffmpeg_first_frame(client, doc)

    if raw:
        dest = dest_dir / f"{stem}_thumb.webp"
        saved = await asyncio.to_thread(_save_webp, raw, dest, int(settings.get("THUMB_WIDTH") or 640))
        if saved is not None:
            size, w, h = saved
            out.thumb_path = f"{channel_id}/{dest.name}"
            out.thumb_bytes = size
            ph, dh = await asyncio.to_thread(imghash.hashes_for, dest)
            out.phash, out.dhash = ph, dh
            # 视频的真实分辨率来自属性，别被缩略图覆盖
            if not is_video:
                out.width, out.height = w, h

    # Compatibility callers may request archiving; workers use a separate queue.
    if is_video and keep_video:
        if defer_archive:
            # The source message has to be downloaded through the worker, but
            # doing that inline would hold up ingestion and translation. The
            # pipeline persists this marker and schedules an archive task.
            out.video_status = ("queued" if settings.get("VIDEO_ARCHIVE_ENABLED")
                                else "skipped_disabled")
        else:
            await _archive_video(client, message, channel_id, out, video_max_mb)

    return out


def _save_original(dest: Path, raw: bytes) -> None:
    temp = dest.with_suffix(dest.suffix + ".partial")
    with temp.open("wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(dest)
