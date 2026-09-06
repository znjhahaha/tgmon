"""⑨ 输出配置 —— RSS feed 增删、API Key 生成与吊销、Webhook 与 HMAC、限流。"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from ... import outputs, settings
from ...crypto import encrypt, mask
from ...db import session_scope
from ...models import ApiKey, Channel, RssFeed, Webhook, WebhookDelivery
from ..deps import redirect, render, require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/outputs")

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,58}$")


def _ctx() -> dict:
    with session_scope() as s:
        feeds = [{
            "id": f.id, "slug": f.slug, "title": f.title,
            "description": f.description or "",
            "channel_ids": f.channel_ids or [], "games": f.games or [],
            "keywords": f.keywords or [],
            "include_duplicates": f.include_duplicates,
            "include_original": f.include_original,
            "max_items": f.max_items, "public": f.public,
        } for f in s.query(RssFeed).order_by(RssFeed.id.asc()).all()]
        keys = [{"id": k.id, "name": k.name, "prefix": k.prefix,
                 "enabled": k.enabled, "rate_per_min": k.rate_per_min,
                 "use_count": k.use_count, "last_used_at": k.last_used_at}
                for k in s.query(ApiKey).order_by(ApiKey.id.asc()).all()]
        hooks = []
        for w in s.query(Webhook).order_by(Webhook.id.asc()).all():
            pend = (s.query(WebhookDelivery)
                    .filter(WebhookDelivery.webhook_id == w.id,
                            WebhookDelivery.status.in_(("retry", "pending")))
                    .count())
            fail = (s.query(WebhookDelivery)
                    .filter(WebhookDelivery.webhook_id == w.id,
                            WebhookDelivery.status == "failed").count())
            hooks.append({"id": w.id, "name": w.name, "url": w.url,
                          "secret_mask": mask(w.secret), "enabled": w.enabled,
                          "channel_ids": w.channel_ids or [],
                          "include_duplicates": w.include_duplicates,
                          "max_retry": w.max_retry,
                          "pending": pend, "failed": fail})
        channels = [{"id": c.id, "title": c.title, "game": c.game or ""}
                    for c in s.query(Channel)
                    .order_by(Channel.title.asc()).all()]
        games = sorted({c["game"] for c in channels if c["game"]})
    return {"feeds": feeds, "keys": keys, "hooks": hooks,
            "channels": channels, "games": games,
            "base_url": outputs.base_url(),
            "api_rate": settings.get("API_RATE_PER_MIN"),
            "webhook_enabled": settings.get("WEBHOOK_ENABLED")}


@router.get("")
async def page(request: Request, user: str = Depends(require_admin)):
    return render(request, "outputs.html", {"nav_active": "outputs", **_ctx()})


def _split(raw: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,\n，]+", raw or "") if x.strip()]


def _ints(raw: list[str] | None) -> list[int]:
    out = []
    for x in raw or []:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            pass
    return out


# ---------------- RSS feed ----------------

@router.post("/feeds/create")
async def feed_create(request: Request,
                      slug: str = Form(...),
                      title: str = Form(...),
                      description: str = Form(""),
                      games: str = Form(""),
                      keywords: str = Form(""),
                      max_items: int = Form(50),
                      include_duplicates: str = Form(""),
                      include_original: str = Form("on"),
                      public: str = Form("on"),
                      user: str = Depends(require_admin)):
    form = await request.form()
    chan_ids = _ints(form.getlist("channel_ids"))
    slug = slug.strip().lower()
    if not _SLUG_RE.match(slug):
        return HTMLResponse(
            "<div class='flash err'>slug 只能用小写字母、数字、- 和 _，2-59 位</div>")
    with session_scope() as s:
        if s.query(RssFeed).filter(RssFeed.slug == slug).first():
            return HTMLResponse("<div class='flash err'>这个 slug 已存在</div>")
        s.add(RssFeed(
            slug=slug, title=title.strip(),
            description=description.strip() or None,
            channel_ids=chan_ids or None, games=_split(games) or None,
            keywords=_split(keywords) or None,
            max_items=max(1, min(max_items, 200)),
            include_duplicates=include_duplicates.strip().lower() in ("on", "1"),
            include_original=include_original.strip().lower() in ("on", "1"),
            public=public.strip().lower() in ("on", "1"),
        ))
    return redirect("/outputs")


@router.post("/feeds/{fid}/delete")
async def feed_delete(fid: int, request: Request,
                      user: str = Depends(require_admin)):
    with session_scope() as s:
        row = s.get(RssFeed, fid)
        if row is not None:
            s.delete(row)
    return redirect("/outputs")


# ---------------- API Key ----------------

@router.post("/keys/create")
async def key_create(request: Request, name: str = Form(...),
                     rate_per_min: int = Form(120),
                     user: str = Depends(require_admin)):
    raw = "tgm_" + secrets.token_urlsafe(32)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    with session_scope() as s:
        s.add(ApiKey(name=name.strip(), key_hash=digest, prefix=raw[:12],
                     rate_per_min=max(1, rate_per_min)))
    # 明文只在这一次显示，之后只存 sha256
    return HTMLResponse(
        "<div class='flash ok'>已生成。<b>这串只显示这一次，现在复制走：</b>"
        f"<br><code class='key'>{raw}</code></div>"
        "<div class='hint'>调用方式：请求头 <code>X-API-Key: 上面这串</code></div>")


@router.post("/keys/{kid}/toggle")
async def key_toggle(kid: int, request: Request,
                     user: str = Depends(require_admin)):
    with session_scope() as s:
        row = s.get(ApiKey, kid)
        if row is None:
            return HTMLResponse("<span class='err'>不存在</span>")
        row.enabled = not row.enabled
        state = row.enabled
    label = "启用中" if state else "已吊销"
    cls = "on" if state else "off"
    return HTMLResponse(
        f"<button class='pill {cls}' "
        f"hx-post='/outputs/keys/{kid}/toggle' hx-swap='outerHTML'>{label}</button>")


@router.post("/keys/{kid}/delete")
async def key_delete(kid: int, request: Request,
                     user: str = Depends(require_admin)):
    with session_scope() as s:
        row = s.get(ApiKey, kid)
        if row is not None:
            s.delete(row)
    return redirect("/outputs")


# ---------------- Webhook ----------------

@router.post("/hooks/create")
async def hook_create(request: Request, name: str = Form(...),
                      url: str = Form(...), secret: str = Form(""),
                      max_retry: int = Form(4),
                      include_duplicates: str = Form(""),
                      user: str = Depends(require_admin)):
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return HTMLResponse(
            "<div class='flash err'>URL 必须以 http:// 或 https:// 开头</div>")
    form = await request.form()
    chan_ids = _ints(form.getlist("channel_ids"))
    with session_scope() as s:
        s.add(Webhook(
            name=name.strip(), url=url,
            secret=encrypt(secret.strip()) if secret.strip() else None,
            channel_ids=chan_ids or None,
            include_duplicates=include_duplicates.strip().lower() in ("on", "1"),
            max_retry=max(1, min(max_retry, 10)),
        ))
    return redirect("/outputs")


@router.post("/hooks/{hid}/update")
async def hook_update(hid: int, request: Request, name: str = Form(...),
                      url: str = Form(...), secret: str = Form(""),
                      max_retry: int = Form(4), enabled: str = Form(""),
                      include_duplicates: str = Form(""),
                      user: str = Depends(require_admin)):
    form = await request.form()
    chan_ids = _ints(form.getlist("channel_ids"))
    with session_scope() as s:
        row = s.get(Webhook, hid)
        if row is None:
            return HTMLResponse("<div class='flash err'>不存在</div>")
        row.name = name.strip()
        row.url = url.strip()
        if secret.strip():
            row.secret = encrypt(secret.strip())
        row.max_retry = max(1, min(max_retry, 10))
        row.enabled = enabled.strip().lower() in ("on", "1")
        row.include_duplicates = include_duplicates.strip().lower() in ("on", "1")
        row.channel_ids = chan_ids or None
    return HTMLResponse("<div class='flash ok'>已保存</div>")


@router.post("/hooks/{hid}/delete")
async def hook_delete(hid: int, request: Request,
                      user: str = Depends(require_admin)):
    with session_scope() as s:
        row = s.get(Webhook, hid)
        if row is not None:
            s.delete(row)
    return redirect("/outputs")


@router.post("/hooks/{hid}/test")
async def hook_test(hid: int, request: Request,
                    user: str = Depends(require_admin)):
    """手动触发一次推送，方便调机器人。直接在 admin 里发，不经 worker。"""
    res = await outputs.test_webhook(hid)
    if res.get("ok"):
        return HTMLResponse(
            f"<span class='ok'>已送达 HTTP {res.get('http_status')}</span>")
    err = str(res.get("error") or "失败").replace("<", "&lt;")[:300]
    return HTMLResponse(f"<span class='err'>{err}</span>")


@router.post("/config")
async def save_config(request: Request, api_rate: int = Form(120),
                      webhook_enabled: str = Form(""),
                      base_url: str = Form(""),
                      user: str = Depends(require_admin)):
    settings.set_many({
        "API_RATE_PER_MIN": max(1, api_rate),
        "WEBHOOK_ENABLED": webhook_enabled.strip().lower() in ("on", "1"),
        "BASE_URL": base_url.strip().rstrip("/"),
    })
    return HTMLResponse("<div class='flash ok'>已保存</div>")
