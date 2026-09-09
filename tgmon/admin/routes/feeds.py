"""RSS 输出。条目含译文 + 原文 + 缩略图 enclosure + TG 深链。

?media=none 出纯文本版 —— 大陆访问 61 KB/s 时省掉图片请求。
"""
from __future__ import annotations

import hashlib
import logging
from datetime import timezone
from urllib.parse import quote
from xml.sax.saxutils import escape

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from feedgen.feed import FeedGenerator
from sqlalchemy import Text, or_

from ... import outputs, settings
from ...db import session_scope
from ...glossary import normalize_game
from ...kb.annotate import entity_names
from ...models import ApiKey, Channel, MessageMedia, MonitorMessage, RssFeed
from ...util import spoiler_segments
from ..deps import current_role, current_user

logger = logging.getLogger(__name__)
router = APIRouter()


def _check_access(request: Request, feed: dict, x_api_key: str | None) -> None:
    # 非 public feed：管理员登录态或 API key 二选一。访客（guest）登录态
    # 不算 —— 访客范围只有公开 feed，订阅地址不该经它泄露
    if feed["public"] or _is_admin(request):
        return
    if not x_api_key:
        raise HTTPException(401, "这个 feed 不是公开的，需要 X-API-Key")
    digest = hashlib.sha256(x_api_key.strip().encode()).hexdigest()
    with session_scope() as s:
        row = s.query(ApiKey).filter(ApiKey.key_hash == digest,
                                     ApiKey.enabled.is_(True)).first()
    if row is None:
        raise HTTPException(401, "API Key 无效")


def _is_admin(request: Request) -> bool:
    return bool(current_user(request)) and current_role(request) == "admin"


def _load_feed(slug: str) -> dict | None:
    with session_scope() as s:
        f = s.query(RssFeed).filter(RssFeed.slug == slug).first()
        if f is None:
            return None
        return {"id": f.id, "slug": f.slug, "title": f.title,
                "description": f.description or f.title,
                "channel_ids": f.channel_ids or [], "games": f.games or [],
                "keywords": f.keywords or [],
                "include_duplicates": f.include_duplicates,
                "include_original": f.include_original,
                "max_items": f.max_items, "public": f.public}


def _entries(feed: dict, entity: str = "") -> list[dict]:
    from ...message_query import effective_game, project_many, query as message_query
    with session_scope() as s:
        games = [normalize_game(g) for g in feed["games"] if normalize_game(g)]
        chan_ids = list(feed["channel_ids"])
        q = message_query(s, duplicates="all", channel_ids=chan_ids or None)
        if games:
            # A multi-game channel is filtered by the message's effective identity.
            q = q.filter(effective_game().in_(games) | MonitorMessage.channel_id.in_(chan_ids))
        if not feed["include_duplicates"]:
            from ...message_query import distinct_stories
            q = q.filter(distinct_stories())
        if feed["keywords"]:
            conds = []
            for kw in feed["keywords"]:
                like = f"%{kw}%"
                conds.extend((MonitorMessage.text_raw.ilike(like),
                              MonitorMessage.text_zh.ilike(like)))
            if conds:
                q = q.filter(or_(*conds))
        rows = q.order_by(MonitorMessage.published_at.desc(), MonitorMessage.id.desc()).limit(
            feed["max_items"] * 4 if entity else feed["max_items"]).all()
        names = {c.id: c.title for c in s.query(Channel).all()}
        out = []
        views = project_many(s, rows)
        for r, item in zip(rows, views):
            if entity and entity not in entity_names(r.entities):
                continue
            out.append({
                "id": item["id"], "channel": names.get(r.channel_id, item["title"]),
                "game": item["game"], "text_zh": r.text_zh, "text_raw": r.text_raw,
                "deeplink": r.deeplink, "published_at": r.published_at,
                "status": r.translate_status, "has_spoiler": bool(r.has_spoiler),
                "spoiler_ranges": r.spoiler_ranges or [],
                "media": [{"kind": m["kind"], "thumb": m["thumb"], "bytes": m["orig_bytes"],
                           "duration": m["duration"], "orig_bytes": m["orig_bytes"],
                           "width": m["width"], "height": m["height"]} for m in item["media"]],
            })
        return out[:feed["max_items"]]


def _title_of(e: dict) -> str:
    body = (e["text_zh"] or e["text_raw"] or "").strip()
    first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    first = " ".join(first.split())
    if not first:
        kinds = {m["kind"] for m in e["media"]}
        first = "（图片）" if "photo" in kinds else "（视频）" if "video" in kinds else "（无文本）"
    title = first if len(first) <= 80 else first[:79] + "…"
    # 剧透前缀是兜底：多数阅读器剥内联样式，黑底黑字的正文打码可能失效，
    # 标题至少还留着警告。只受 SPOILER_MASK_OUTPUT 一个开关控制
    if e.get("has_spoiler") and settings.get("SPOILER_MASK_OUTPUT"):
        title = f"[剧透] {title}"
    return f"[{e['channel']}] {title}" if e["channel"] else title


def _spoiler_masked(text: str, ranges: list) -> str:
    """按剧透区间把文本渲染成 HTML，剧透段黑底黑字。

    多数阅读器保留内联 style，选中的时候才露出来；剥样式的阅读器
    靠标题 [剧透] 前缀兜底。区间只对原文（text_raw）有效。
    """
    segs = spoiler_segments(text, ranges)
    if not segs:
        return ""
    out = []
    for seg, sp in segs:
        esc = escape(seg).replace("\n", "<br>")
        out.append(f'<span style="background:#000;color:#000">{esc}</span>'
                   if sp else esc)
    return "".join(out)


def _html_body(e: dict, include_original: bool, with_media: bool) -> str:
    parts = []
    if with_media:
        for m in e["media"]:
            url = outputs.media_url(m["thumb"])
            if not url:
                continue
            alt = "视频首帧" if m["kind"] == "video" else "图片"
            parts.append(f'<p><img src="{escape(url)}" alt="{alt}" '
                         f'style="max-width:100%"></p>')
    mask = settings.get("SPOILER_MASK_OUTPUT")
    zh = (e["text_zh"] or "").strip()
    if zh:
        # 译文与原文一致（zh_first 跳过翻译）时区间才能对齐，精确打码；
        # 否则原文直出不打码，靠标题前缀警告
        if mask and e.get("spoiler_ranges") and zh == (e["text_raw"] or "").strip():
            parts.append("<div>" + _spoiler_masked(zh, e["spoiler_ranges"]) + "</div>")
        else:
            parts.append("<div>" + escape(zh).replace("\n", "<br>") + "</div>")
    elif e["status"] in ("failed", "budget"):
        parts.append("<p><em>（翻译失败，下面是原文）</em></p>")

    meta = []
    for m in e["media"]:
        bits = [m["kind"]]
        if m["width"] and m["height"]:
            bits.append(f"{m['width']}×{m['height']}")
        if m["duration"]:
            bits.append(f"{m['duration']}s")
        if m["orig_bytes"]:
            bits.append(f"{m['orig_bytes'] / 1048576:.1f} MB")
        meta.append(" · ".join(str(b) for b in bits))
    if meta:
        parts.append("<p style='color:#888;font-size:12px'>媒体："
                     + escape("；".join(meta)) + "</p>")

    if e["deeplink"]:
        # 网站源的 deeplink 指向站点自己，写「在 Telegram 中打开」是错的
        label = ("打开原页面" if not e["deeplink"].startswith("https://t.me/")
                 else "在 Telegram 中打开（视频点这里看）")
        parts.append(f'<p><a href="{escape(e["deeplink"])}">{label}</a></p>')

    # 情报类内容必须可核对，所以译文后附原文
    raw = (e["text_raw"] or "").strip()
    if include_original and raw and raw != zh:
        if mask and e.get("spoiler_ranges"):
            parts.append("<hr><details><summary>原文</summary><div>"
                         + _spoiler_masked(raw, e["spoiler_ranges"])
                         + "</div></details>")
        else:
            parts.append("<hr><details><summary>原文</summary><div>"
                         + escape(raw).replace("\n", "<br>") + "</div></details>")
    return "".join(parts) or "（空）"


@router.get("/rss/{slug}")
async def rss(slug: str, request: Request, media: str = "", entity: str = "",
              x_api_key: str | None = Header(None, alias="X-API-Key")):
    # /rss/foo.xml 与 /rss/foo 都要认。用一条路由 + 去后缀，
    # 叠两个装饰器会让 {slug} 把 ".xml" 一起吃进去
    if slug.endswith(".xml"):
        slug = slug[:-4]
    feed = _load_feed(slug)
    if feed is None:
        raise HTTPException(404, "feed 不存在")
    _check_access(request, feed, x_api_key)

    with_media = media.lower() != "none"
    base = outputs.base_url() or str(request.base_url).rstrip("/")

    fg = FeedGenerator()
    self_url = f"{base}/rss/{slug}" + (f"?entity={quote(entity)}" if entity else "")
    fg.id(self_url)
    fg.title(feed["title"] + (f" · {entity}" if entity else ""))
    fg.description(feed["description"])
    fg.link(href=self_url, rel="self")
    fg.language("zh-CN")

    entries = _entries(feed, entity)
    # feedgen 是后进先出，倒序加保证 RSS 里新的在前
    for e in reversed(entries):
        fe = fg.add_entry()
        fe.id(f"{base}/messages#{e['id']}")
        fe.title(_title_of(e))
        fe.link(href=e["deeplink"] or f"{base}/messages")
        fe.content(_html_body(e, feed["include_original"], with_media),
                   type="CDATA")
        if e["published_at"]:
            fe.pubDate(e["published_at"].replace(tzinfo=timezone.utc))
        if with_media:
            for m in e["media"]:
                url = outputs.media_url(m["thumb"])
                if url:
                    fe.enclosure(url, str(m["bytes"] or 0), "image/webp")
                    break

    xml = fg.rss_str(pretty=False)
    return Response(content=xml, media_type="application/rss+xml; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=120"})


@router.get("/feeds")
async def feed_index(request: Request):
    """所有 feed 的清单页，方便复制订阅地址。"""
    base = outputs.base_url() or str(request.base_url).rstrip("/")
    with session_scope() as s:
        rows = s.query(RssFeed).order_by(RssFeed.id.asc()).all()
        items = "".join(
            f"<li><b>{escape(f.title)}</b> "
            f"<code>{base}/rss/{escape(f.slug)}</code>"
            + ("" if f.public else " <em>（需 API Key）</em>")
            + f" · <a href='/rss/{escape(f.slug)}'>打开</a>"
            f" · <a href='/rss/{escape(f.slug)}?media=none'>纯文本版</a></li>"
            for f in rows)
    if not items:
        items = "<li>还没建 feed。去<a href='/outputs'>输出配置</a>加一个</li>"
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><title>tgmon feeds</title>"
        "<style>body{font-family:system-ui;max-width:800px;margin:40px auto;"
        "padding:0 16px;line-height:1.7}code{background:#f4f4f5;padding:2px 6px;"
        "border-radius:4px}</style>"
        f"<h1>RSS feeds</h1><ul>{items}</ul>"
        "<p><a href='/'>回后台</a></p>")
