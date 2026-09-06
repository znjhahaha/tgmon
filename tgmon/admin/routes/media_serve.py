"""归档视频的鉴权下发。

为什么不是静态目录挂载：视频在 media/private/ 下，挂 /media 的 StaticFiles
会把 private 一起公开。Caddy 已经对 /media/private/* 直接 404，唯一入口是
这条走 require_login 的路由 —— URL 与被拦的路径刻意一致，模板里
`/media/private/{video_path}` 两边通用。

/media/qq-video/*：QQ 平台拉取归档视频用的签名限时链接（免登录）。
腾讯服务器不会带我们的登录 cookie，所以用 HMAC 签名做准入：
链接由 qqbot/media.py 生成（发富媒体消息前），30 分钟有效。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from ...db import session_scope
from ...models import MessageMedia
from ...paths import VIDEO_DIR
from ..deps import guest_rate_limit, require_login

logger = logging.getLogger(__name__)

router = APIRouter()


def _video_file_for(mid: int) -> tuple[str, str] | None:
    """(绝对路径, 相对 VIDEO_DIR 路径) —— 该消息的归档视频。"""
    with session_scope() as s:
        row = (s.query(MessageMedia)
               .filter(MessageMedia.message_id == mid)
               .filter(MessageMedia.video_path.isnot(None))
               .first())
        if row is None or not row.video_path:
            return None
        return str(VIDEO_DIR / row.video_path), row.video_path


@router.get("/media/qq-video/{mid}/{expires}/{sig}")
async def serve_video_signed(mid: int, expires: int, sig: str,
                             request: Request,
                             _rl: None = Depends(guest_rate_limit)):
    """QQ 平台拉取视频（URL 上传通道）。签名校验失败一律 404 不解释。"""
    # 1. 时效
    if expires < int(time.time()):
        raise HTTPException(404)
    # 2. HMAC 签名（与 qqbot/media.py 的 make_video_sig 同算法）
    from ...qqbot import media as qq_media
    expect = qq_media.make_video_sig(mid, expires)
    if not hmac.compare_digest(expect, (sig or "").lower()):
        raise HTTPException(404)
    # 3. 该消息确实有归档视频
    got = _video_file_for(mid)
    if got is None:
        raise HTTPException(404)
    full, rel = got
    # 4. 归一化防穿越（路径来自 DB，防御性再查一次）
    from pathlib import Path
    p = Path(full).resolve()
    if not p.is_relative_to(VIDEO_DIR.resolve()) or not p.is_file():
        raise HTTPException(404)

    try:
        with session_scope() as s:
            row = (s.query(MessageMedia)
                   .filter(MessageMedia.video_path == rel).first())
            if row is not None:
                row.last_access_at = datetime.utcnow()
    except Exception:
        pass
    return FileResponse(p, media_type="video/mp4")


@router.get("/media/private/{path:path}")
async def serve_video(path: str, request: Request, dl: str = "",
                      user: str = Depends(require_login),
                      _rl: None = Depends(guest_rate_limit)):
    # 播放链接是 <a> 整页点击，未登录 303 跳登录页（不是 htmx 片段，
    # 不用 require_login_api 的 401）
    # {path:path} 会解码 %2e%2e —— 必须归一化后确认没跑出 VIDEO_DIR，
    # 否则登录用户可以读任意文件（含 session/db）
    full = (VIDEO_DIR / path).resolve()
    if not full.is_relative_to(VIDEO_DIR.resolve()):
        raise HTTPException(404)
    if not full.is_file():
        raise HTTPException(404)

    # 记录访问时间：视频总量 LRU 淘汰的依据。找不到记录（DB 行被删）
    # 照常发文件 —— 文件在就说明还没被清理
    rel = full.relative_to(VIDEO_DIR.resolve()).as_posix()
    try:
        with session_scope() as s:
            row = (s.query(MessageMedia)
                   .filter(MessageMedia.video_path == rel).first())
            if row is not None:
                row.last_access_at = datetime.utcnow()
    except Exception:
        pass

    headers = {}
    if dl == "1":
        # 下载模式：attachment + 文件名（lightbox「下载视频」按钮）
        headers["Content-Disposition"] = f'attachment; filename="{full.name}"'
    return FileResponse(full, media_type="video/mp4", headers=headers)
