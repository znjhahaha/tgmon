"""FastAPI 应用装配。

为大陆访问做的取舍：服务端渲染 + gzip/zstd + 缩略图长缓存。没有 SPA bundle ——
从大陆只有 61 KB/s，几百 KB 的 JS 首屏要等十几秒。
"""
from __future__ import annotations

import logging
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import PlainTextResponse

from ..bootstrap import init_all
from ..paths import MEDIA_DIR, ensure_dirs
from .routes import (
    account, api, auth, channels, dedupe, feeds, glossary_ui, media_serve,
    messages, outputs_cfg, overview, plugins, prompts_ui, providers, qqbot_cb,
    qqbot_ui, share, system,
)

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent / "static"


class _NoPrivateStatic(StaticFiles):
    """media 静态挂载：private/（归档视频）一概 404。

    正常流量到不了这里 —— Caddy 已经 respond 404，鉴权路由也先于本挂载
    注册。这层只防「compose 网络内直连 admin:8000」的内网误用。
    """

    async def get_response(self, path: str, scope):
        if path == "private" or path.startswith("private/"):
            return PlainTextResponse("not found", status_code=404)
        return await super().get_response(path, scope)


def create_app() -> FastAPI:
    ensure_dirs()
    init_all()

    @asynccontextmanager
    async def lifespan(app):
        from ..qqbot.runtime import run_queue
        tasks = [asyncio.create_task(run_queue(queue)) for queue in
                 ("conversation", "conversation", "conversation", "qq_cards")]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            from ..extension_runtime import close
            await close()

    app = FastAPI(title="tgmon 后台", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    # 大陆访问的关键：文本响应压缩后通常只有几 KB
    app.add_middleware(GZipMiddleware, minimum_size=500)

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # 归档视频的鉴权路由必须先于 /media 挂载注册：Starlette 按注册顺序
    # 匹配，mount 在前会把 /media/private/* 也交给 StaticFiles —— 等于
    # 把私有视频对任何拿到 URL 的人公开
    app.include_router(media_serve.router)

    # 缩略图。Caddy 会给它加长 Cache-Control
    app.mount("/media", _NoPrivateStatic(directory=str(MEDIA_DIR)), name="media")

    app.include_router(auth.router)
    app.include_router(overview.router)
    app.include_router(account.router)
    app.include_router(channels.router)
    app.include_router(providers.router)
    app.include_router(glossary_ui.router)
    app.include_router(prompts_ui.router)
    app.include_router(messages.router)
    app.include_router(dedupe.router)
    app.include_router(outputs_cfg.router)
    # QQ 机器人：qqbot_cb 是腾讯事件回调（POST /qqbot/callback，免登录+验签），
    # qqbot_ui 是管理页。两者共用 /qqbot 前缀但路径不重叠
    app.include_router(qqbot_cb.router)
    app.include_router(qqbot_ui.router)
    app.include_router(plugins.router)
    app.include_router(system.router)
    app.include_router(api.router)
    app.include_router(feeds.router)
    # 分享短链放最后注册：/s/{token} 是免登录面，与其它路由无前缀冲突，
    # 但 /messages/{mid}/share 必须在 messages.router 之后仍可匹配
    # （FastAPI 按注册顺序匹配，/messages/{mid}/share 不会被
    # messages.router 的 /messages/{mid}/detail 抢走 —— 路径段不同）
    app.include_router(share.router)

    @app.exception_handler(403)
    async def _403(request: Request, exc):
        # 访客（guest）点进管理页：给人话提示而不是裸 JSON
        return HTMLResponse(
            "<h1>403 · 需要管理员账号</h1>"
            "<p>当前登录的是访客账号，只能查看消息浏览与 RSS 清单。</p>"
            "<p><a href='/messages'>回消息浏览</a> · <a href='/logout'>退出换账号</a></p>",
            status_code=403)

    @app.exception_handler(404)
    async def _404(request: Request, exc):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "not found"}, status_code=404)
        return HTMLResponse(
            "<h1>404</h1><p><a href='/'>回后台首页</a></p>", status_code=404)

    # 这里曾经有个「把 303 重建成 RedirectResponse」的中间件。它会连带丢掉
    # Set-Cookie，导致登录成功但 cookie 发不出去。FastAPI 的 HTTPException
    # 处理器本来就会保留我传的 Location 头，那个中间件既多余又有害，已删除。

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True}

    return app


app = create_app()
