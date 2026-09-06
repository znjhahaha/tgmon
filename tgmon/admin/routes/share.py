"""分享短链：管理员对一条或多条消息生成免登录查看链接。

威胁模型与取舍：
- token 校验走 SHA-256（库里只有 hash，库泄露也枚举不出有效链接）；
  2026-09 起额外存一份可逆加密的 token（token_enc，secret.key 加密）——
  代价是拿到 DB+secret.key 才能还原 URL，但换来「刷新后链接仍可显示/
  复制」（用户反馈：token 只存 hash 时没复制就永远找不回）
- 分享页独立模板（无导航、无其它消息链接），持有 token 的人只能看
  授权的消息（单条或合并多条），顺藤摸瓜摸不到瓜
- 图片/视频走 token 授权的专有路由，且校验 media 属于该 token 的
  消息集合 —— 不能拿 A 消息的 token 去下 B 消息的图
- 页面 20 req/min/IP、下载 10 req/min/IP（内存限流）。CF 侧再叠一层
  边缘限流（系统页有建议规则）—— 橙云未开时这层就是唯一防线
"""
from __future__ import annotations

import hashlib
import asyncio
import io
import logging
import secrets
import zipfile
from datetime import datetime, timedelta
from html import escape as _esc

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from ... import card
from ...crypto import decrypt as _dec, encrypt as _enc
from ...db import session_scope
from ...models import Channel, MessageMedia, MonitorMessage, ShareToken
from ...paths import MEDIA_DIR, VIDEO_DIR
from ...settings import get as cfg_get
from ...util import to_local
from ..deps import (
    RateLimiter, client_ip, redirect, render, require_admin, templates,
)
from .messages import _row_view

logger = logging.getLogger(__name__)
router = APIRouter()

# 分享链接的 AI 概括 prompt（2026-09 起分享时自动生成，无需手动触发；
# 概括要求覆盖全部内容，长度不设 30 字上限）
SUMMARIZE_PROMPT = (
    "你是游戏爆料编辑。用简体中文全面概括下面这条爆料的核心内容，"
    "必须覆盖正文里的所有关键信息点（游戏、角色/版本、内容类型、"
    "数值/日期等），不要遗漏；长度不设上限，讲清楚即可，通常 50-150 字，"
    "写成一条连贯的段落，不要分点列举。"
    "直接输出概括本身，不要引号、不要前缀、不要解释、不要换行。"
)
# 合并分享（多选消息）的整体概括
BULK_SUMMARIZE_PROMPT = (
    "你是游戏爆料编辑。下面是同一批的多条爆料消息（已用分隔线隔开）。"
    "用简体中文归纳这批爆料的整体内容，逐条覆盖每一条消息的关键信息点"
    "（游戏、角色/版本、内容类型、数值/日期等），不要遗漏任何一条；"
    "长度不设上限，讲清楚即可，通常 80-250 字，写成一条连贯的段落，"
    "不要分点列举。"
    "直接输出概括本身，不要引号、不要前缀、不要解释、不要换行。"
)
# 概括存库防跑飞上限（prompt 不限字数，代码侧兜底）
MAX_SUMMARY = 400

# 限流：页面 20/min/IP，媒体下载 10/min/IP。管理员不受限（同 deps.guest_rate_limit 的分级思路）
_PAGE_LIMITER = RateLimiter(20)
_DL_LIMITER = RateLimiter(10)

_EXPIRY_CHOICES = {   # 表单值 → 小时
    "24h": 24, "48h": 48, "7d": 24 * 7, "30d": 24 * 30,
}
# 续期选项（管理页）
_EXTEND_CHOICES = {
    "24h": 24, "7d": 24 * 7, "30d": 24 * 30,
}
# 合并分享防呆上限
MAX_BULK = 50
CARD_MAX_HEIGHT = 16000


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _base_url(request: Request | None = None) -> str:
    """分享链接用绝对地址。BASE_URL 配置优先（RSS 同款），否则取请求头。"""
    cfg = str(cfg_get("BASE_URL") or "").strip().rstrip("/")
    if cfg:
        return cfg
    if request is not None:
        return str(request.base_url).rstrip("/")
    return ""


def _copy_block(summary: str, url: str, dom_id: str) -> str:
    """生成一次复制完整分享文案的片段（摘要 + URL）。"""
    parts = [str(summary or "").strip(), str(url or "").strip()]
    text = "\n\n".join(p for p in parts if p)
    if not text:
        return ""
    return (f"<div class='share-copy' style='display:flex;gap:6px;"
            f"align-items:center;flex-wrap:wrap;margin-top:6px'>"
            f"<textarea id='copy-{dom_id}' hidden>{_esc(text)}</textarea>"
            f"<button class='btn sm' type='button' "
            f"onclick=\"copyShareText('copy-{dom_id}',this)\">复制分享文案</button>"
            f"<span class='dim' style='font-size:11px'>文字和链接一起复制</span></div>")


def _card_items(msgs, channels, media_by_message) -> list[dict]:
    """ORM 消息转换成 card.render_long_cards 所需的纯字典。"""
    if msgs and isinstance(msgs[0], dict):
        return [{**m, "raw": ""} for m in msgs]
    out = []
    for m in msgs:
        ch = channels.get(m.channel_id)
        local = to_local(m.published_at) if m.published_at else None
        out.append({
            "game": m.game_detected or (ch.game if ch else "") or "",
            "channel": ch.title if ch else "",
            "date_str": local.strftime("%m-%d %H:%M") if local else "",
            "version": m.version_tag or "",
            "text": (m.text_zh or m.text_raw or "").strip(),
            "raw": (m.text_raw or "").strip(),
            "thumb_paths": [x.thumb_path for x in media_by_message.get(m.id, [])
                            if x.thumb_path],
            "deeplink": m.deeplink or "",
            "has_spoiler": bool(m.has_spoiler),
        })
    return out


async def _card_payload(sid: int, request: Request):
    """Load the complete payload used by both JPEG and ZIP endpoints."""
    with session_scope() as s:
        row = s.get(ShareToken, sid)
        if row is None or row.revoked or row.expires_at <= datetime.utcnow():
            raise HTTPException(404)
        url = _share_url(row, request) or ""
        if row.snapshot_items:
            return row, list(row.snapshot_items), {}, {}, row.summary or "", url
        mids = _share_msg_ids(row)
        msgs = (s.query(MonitorMessage)
                .filter(MonitorMessage.id.in_(mids))
                .order_by(MonitorMessage.published_at.asc()).all())
        if not msgs:
            raise HTTPException(404)
        channels = {c.id: c for c in s.query(Channel).all()}
        media_rows = (s.query(MessageMedia)
                      .filter(MessageMedia.message_id.in_(mids))
                      .order_by(MessageMedia.id.asc()).all())
        media = {}
        for x in media_rows:
            # Photos and videos with a first frame are both useful in a card.
            if x.thumb_path:
                media.setdefault(x.message_id, []).append(x)
        summary = (row.summary or "").strip()
    if not summary:
        texts = [(m.text_zh or m.text_raw or "").strip() for m in msgs]
        summary = await _gen_summary(texts, single=len(msgs) == 1)
        with session_scope() as s:
            r2 = s.get(ShareToken, sid)
            if r2 is not None and not (r2.summary or "").strip():
                r2.summary = summary
    return row, msgs, channels, media, summary, url


# ---------------- 共用小工具 ----------------

def _first_line(text: str, cap: int = 80) -> str:
    """首个非空行截断（AI 不可用时的降级概括素材）。"""
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if ln:
            return ln[:cap]
    return ""


async def _ai_summary(text: str, prompt: str) -> str | None:
    """AI 概括正文。无文本可分析 / AI 全线失败 → None（兜底交给调用方）。

    模型偶尔无视「不要换行」—— 统一把空白折成空格，保证存库的是单行。
    """
    text = (text or "").strip()
    if not text:
        return None
    from ...providers import registry
    try:
        res = await registry.complete_with_failover(prompt, text[:6000])
    except Exception as e:
        logger.warning("AI 概括失败: %s", e)
        res = None
    if res is not None and res.ok and (res.text or "").strip():
        line = " ".join(res.text.strip().strip('「」『』"“”').split())
        return line[:MAX_SUMMARY] or None
    return None


def _fallback_summary(texts: list[str], single: bool) -> str:
    """AI 不可用时的兜底概括：正文首行（合并则逐条首行拼接）。"""
    heads = [_first_line(t, 40) for t in texts]
    heads = [h for h in heads if h]
    if single:
        return heads[0][:80] if heads else "（纯图爆料 · 内容见图）"
    if heads:
        return "；".join(heads)[:MAX_SUMMARY]
    return "（纯图爆料合集 · 内容见图）"


async def _gen_summary(texts: list[str], single: bool) -> str:
    """生成分享概括：AI 优先（覆盖全部内容），失败降级正文首行。

    单条与合并共用；texts 为各条消息正文（可为空，纯图爆料）。
    """
    joined = "\n----------\n".join(t for t in texts if t and t.strip())
    prompt = SUMMARIZE_PROMPT if single else BULK_SUMMARIZE_PROMPT
    got = await _ai_summary(joined, prompt)
    return got or _fallback_summary(texts, single)


def _share_msg_ids(row: ShareToken) -> list[int]:
    """token 授权的消息 id 集合。message_ids（合并）优先，否则单条。"""
    if row.message_ids:
        values = row.message_ids
        if isinstance(values, str):
            try:
                import json
                values = json.loads(values)
            except (TypeError, ValueError):
                values = []
        return [int(x) for x in (values or [])]
    return [row.message_id]


def _share_url(row: ShareToken, request: Request | None = None) -> str | None:
    """还原链接 URL。旧链接（无 token_enc）返回 None（不可恢复）。"""
    token = _dec(row.token_enc) if row.token_enc else None
    if not token:
        return None
    base = _base_url(request)
    if not base:
        return None
    return f"{base}/s/{token}"


def _share_views(rows: list[ShareToken], request: Request | None = None
                 ) -> dict[int, dict]:
    """ShareToken 行 → 管理侧展示字典（含还原 URL 与消息条数）。"""
    now = datetime.utcnow()
    out = {}
    for r in rows:
        mids = _share_msg_ids(r)
        out[r.id] = {
            "id": r.id,
            "url": _share_url(r, request),
            "n_msgs": len(mids),
            "anchor_mid": r.message_id,
            "state": ("已撤销" if r.revoked
                      else ("已过期" if r.expires_at < now else "有效")),
            "active": (not r.revoked and r.expires_at >= now),
            "expires_at": r.expires_at,
            "views": r.views,
            "revoked": bool(r.revoked),
            "summary": (r.summary or "").strip(),
            "copy_text": "\n\n".join(x for x in [((r.summary or "").strip()), _share_url(r, request) or ""] if x),
            "created_by": r.created_by,
            "created_at": r.created_at,
        }
    return out


# ---------------- 管理侧 ----------------

@router.post("/messages/{mid}/share")
async def create_share(mid: int, request: Request,
                       expires: str = Form("48h"),
                       user: str = Depends(require_admin),
                       summary: str = Form("")):
    """生成单条消息短链。返回片段：链接 + 复制/一图流按钮 + 现有链接列表。

    概括 2026-09 起自动生成（AI 覆盖全部内容，不限 30 字；AI 失败降级
    正文首行，纯图爆料给图注兜底）—— 表单不再有概括输入框和按钮。
    summary 参数仅作兼容保留：非空视为人工覆盖。
    """
    hours = _EXPIRY_CHOICES.get(expires, 48)
    # 读文本和 AI 调用放事务外 —— 不占着写锁等模型返回
    with session_scope() as s:
        msg = s.get(MonitorMessage, mid)
        if msg is None:
            return HTMLResponse("<span class='err'>消息不存在</span>")
        text = (msg.text_zh or msg.text_raw or "").strip()
    manual = summary.strip()[:MAX_SUMMARY] if isinstance(summary, str) else ""
    final = manual or await _gen_summary([text], single=True)
    from ...sharing import create_bundle
    sid = await asyncio.to_thread(create_bundle, [mid], created_by=user,
                                   hours=hours, summary=final)
    with session_scope() as s:
        row = s.get(ShareToken, sid)
    url = _share_url(row, request) or f"{_base_url(request)}/s/{_dec(row.token_enc)}"
    logger.info("生成分享链接 #%s（消息 %s，%sh，概括 %d 字，by %s）",
                sid, mid, hours, len(final), user)
    links = _links_fragment(mid)
    copy = _copy_block(final, url, f"msg-{sid}")
    sum_line = (f"<div class='dim' style='font-size:12px;margin-top:4px'>"
                f"概括（已随链接生效）：{_esc(final)}</div>" if final else "")
    return HTMLResponse(
        f"<div class='flash ok'>分享链接已生成（{hours} 小时内有效）："
        f"<input class='key' readonly value='{_esc(url)}' onclick='this.select()'>"
        f"<button class='btn sm' onclick=\"navigator.clipboard.writeText('{_esc(url)}')\">复制链接</button> "
        f"<a class='btn sm' href='/share/{sid}/card' target='_blank'>一图流</a> "
        f"<a class='btn sm ghost' href='/share/{sid}/card?dl=1' target='_blank'>下载</a>"
        f"<a class='btn sm ghost' href='/share/{sid}/card.zip'>ZIP</a>"
        f"{sum_line}{copy}</div>{links}")


@router.get("/share/{sid}/card")
async def share_card(sid: int, request: Request, dl: str = "",
                     page: int = 1,
                     user: str = Depends(require_admin)):
    """一图流卡片：概括 + 缩略图网格 + 短链二维码（JPEG）。

    单条与合并分享共用：合并时概括覆盖全部消息、收录全部可用缩略图和视频首帧，
    按消息内容自适应布局。dl=1 → 下载模式（Content-Disposition
    attachment）。图片是像素，链接/二维码印在图里不受「主动消息禁止
    URL」约束 —— 管理员保存后手动转发进 QQ 群。

    旧链接（自动概括上线前生成、summary 为空）：首次打开卡片时补生成
    并落库，之后访客页/管理页都带上 ——「链接带上最新的 AI 概括」。
    """
    _row, msgs, channels, media, summary, url = await _card_payload(sid, request)
    data = _card_items(msgs, channels, media)
    try:
        pages = card.render_long_cards(data, summary=summary, url=url,
                                       media_dir=MEDIA_DIR,
                                       max_height=CARD_MAX_HEIGHT)
    except RuntimeError as e:
        return HTMLResponse(f"<span class='err'>{e}</span>", status_code=500)
    page = max(1, min(int(page or 1), len(pages)))
    jpeg = pages[page - 1]
    headers = {}
    if dl == "1":
        headers["Content-Disposition"] = \
            f'attachment; filename="tgmon-share-{sid}-{page:02d}.jpg"'
    headers["X-Card-Pages"] = str(len(pages))
    return Response(content=jpeg, media_type="image/jpeg", headers=headers)


@router.get("/share/{sid}/card.zip")
async def share_card_zip(sid: int, request: Request,
                         user: str = Depends(require_admin)):
    """Download all pages of a long card as a ZIP archive."""
    _row, msgs, channels, media, summary, url = await _card_payload(sid, request)
    try:
        pages = card.render_long_cards(_card_items(msgs, channels, media),
                                       summary=summary, url=url,
                                       media_dir=MEDIA_DIR,
                                       max_height=CARD_MAX_HEIGHT)
    except RuntimeError as e:
        return HTMLResponse(f"<span class='err'>{e}</span>", status_code=500)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, payload in enumerate(pages, 1):
            zf.writestr(f"tgmon-share-{sid}-{i:02d}.jpg", payload)
    return Response(content=buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition":
                             f'attachment; filename="tgmon-share-{sid}.zip"'})


@router.get("/s/{token}/card")
async def public_share_card(token: str, request: Request, dl: str = "",
                            page: int = 1):
    """Public equivalent of the admin card endpoint, guarded by the share token."""
    if not _PAGE_LIMITER.check(f"ip:{client_ip(request)}"):
        raise HTTPException(429, "访问过于频繁，稍后再试")
    row = _load_share(token)
    if row is None:
        raise HTTPException(404)
    _row, msgs, channels, media, summary, url = await _card_payload(row.id, request)
    pages = card.render_long_cards(_card_items(msgs, channels, media), summary=summary,
                                   url=url, media_dir=MEDIA_DIR,
                                   max_height=CARD_MAX_HEIGHT)
    page = max(1, min(int(page or 1), len(pages)))
    headers = {"X-Card-Pages": str(len(pages))}
    if dl == "1":
        headers["Content-Disposition"] = f'attachment; filename="tgmon-share-{row.id}-{page:02d}.jpg"'
    return Response(content=pages[page - 1], media_type="image/jpeg", headers=headers)


@router.get("/s/{token}/card.zip")
async def public_share_card_zip(token: str, request: Request):
    if not _PAGE_LIMITER.check(f"ip:{client_ip(request)}"):
        raise HTTPException(429, "访问过于频繁，稍后再试")
    row = _load_share(token)
    if row is None:
        raise HTTPException(404)
    _row, msgs, channels, media, summary, url = await _card_payload(row.id, request)
    pages = card.render_long_cards(_card_items(msgs, channels, media), summary=summary,
                                   url=url, media_dir=MEDIA_DIR,
                                   max_height=CARD_MAX_HEIGHT)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, payload in enumerate(pages, 1):
            zf.writestr(f"tgmon-share-{row.id}-{i:02d}.jpg", payload)
    return Response(content=buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="tgmon-share-{row.id}.zip"'})


@router.post("/messages/bulk-share")
async def create_bulk_share(request: Request,
                            expires: str = Form("48h"),
                            user: str = Depends(require_admin)):
    """多选消息 → 合并分享链接（一个 token 看 N 条）。

    ids 以 JSON 数组提交（表单字段 ids，逗号分隔也兼容）。
    概括自动生成（覆盖全部选中消息，2026-09 起与单条分享一致）；
    结果片段直接给一图流/下载按钮 —— 多选也能一键出卡片。
    """
    import json as _json
    form = await request.form()
    raw = str(form.get("ids") or "")
    try:
        ids = [int(x) for x in _json.loads(raw)] if raw.startswith("[") \
            else [int(x) for x in raw.split(",") if x.strip()]
    except (ValueError, TypeError):
        return HTMLResponse("<span class='err'>消息 id 列表格式不对</span>")
    ids = sorted(set(ids))
    if not ids:
        return HTMLResponse("<span class='err'>没有选中消息</span>")
    if len(ids) > MAX_BULK:
        return HTMLResponse(
            f"<span class='err'>一次最多合并 {MAX_BULK} 条（选中 {len(ids)} 条）</span>")

    hours = _EXPIRY_CHOICES.get(expires, 48)
    # 读消息 + 排序 + 取文本在事务里，AI 概括在事务外
    with session_scope() as s:
        found = {i: s.get(MonitorMessage, i) for i in ids}
        missing = [i for i in ids if found.get(i) is None]
        if missing:
            return HTMLResponse(
                f"<span class='err'>消息 {', '.join(map(str, missing))} 不存在</span>")
        # 按发布时间正序排列（分享页阅读顺序）
        ordered = sorted(ids, key=lambda i: found[i].published_at)
        texts = [(found[i].text_zh or found[i].text_raw or "").strip()
                 for i in ordered]
    summary = await _gen_summary(texts, single=False)
    from ...sharing import create_bundle
    sid = await asyncio.to_thread(create_bundle, ordered, created_by=user,
                                   hours=hours, summary=summary)
    with session_scope() as s:
        row = s.get(ShareToken, sid)
    url = _share_url(row, request) or f"{_base_url(request)}/s/{_dec(row.token_enc)}"
    logger.info("生成合并分享 #%s（%d 条消息，%sh，概括 %d 字，by %s）",
                sid, len(ids), hours, len(summary), user)
    copy = _copy_block(summary, url, f"bulk-{sid}")
    sum_line = (f"<div class='dim' style='font-size:12px;margin-top:4px'>"
                f"概括（已随链接生效）：{_esc(summary)}</div>" if summary else "")
    return HTMLResponse(
        f"<div class='flash ok'>合并分享已生成（{len(ids)} 条消息，"
        f"{hours} 小时内有效）："
        f"<input class='key' readonly value='{_esc(url)}' onclick='this.select()'>"
        f"<button class='btn sm' onclick=\"navigator.clipboard.writeText('{_esc(url)}')\">复制链接</button> "
        f"<a class='btn sm' href='/share/{sid}/card' target='_blank'>一图流</a> "
        f"<a class='btn sm ghost' href='/share/{sid}/card?dl=1' target='_blank'>下载</a>"
        f"<a class='btn sm ghost' href='/share/{sid}/card.zip'>ZIP</a>"
        f"{sum_line}{copy}</div>")


@router.post("/share/{sid}/revoke")
async def revoke_share(sid: int, request: Request,
                       user: str = Depends(require_admin)):
    mid = 0
    with session_scope() as s:
        row = s.get(ShareToken, sid)
        if row is None:
            return HTMLResponse("<span class='err'>链接不存在</span>")
        row.revoked = True
        mid = row.message_id
    logger.info("撤销分享链接 #%s（by %s）", sid, user)
    return HTMLResponse(_links_fragment(mid))


def _links_fragment(mid: int) -> str:
    """该消息现有分享链接列表（含撤销按钮）。

    token_enc 存在则还原 URL 并给复制按钮；旧链接（仅 hash）标注
    URL 不可恢复。
    """
    with session_scope() as s:
        rows = (s.query(ShareToken)
                .filter(ShareToken.message_id == mid)
                .order_by(ShareToken.id.desc()).all())
        # 合并分享也挂在其锚点消息下 —— 锚点本身就在上面的查询里
        views = _share_views(rows)
    if not rows:
        return ""
    now = datetime.utcnow()
    out = ["<div style='margin-top:6px'><b style='font-size:12px'>已有分享链接</b>"
           "<table style='margin-top:4px'><tr><th>#</th><th>状态</th>"
           "<th>消息</th><th>概括</th><th>过期时间</th><th>访问</th><th>链接</th>"
           "<th>一图流</th><th></th></tr>"]
    for r in rows:
        v = views[r.id]
        url = v["url"]
        # 概括列：本链接携带的 AI/人工概括（截断展示，悬停看全文）
        sum_txt = (r.summary or "").strip()
        link_cell = (f"<input class='key' style='width:150px;font-size:11px' readonly "
                     f"value='{_esc(url)}' onclick='this.select()'> "
                     f"<button class='btn sm' onclick=\"navigator.clipboard.writeText('{_esc(url)}')\">复制链接</button>"
                     if url else "<span class='dim'>旧链接·URL 不可恢复</span>")
        if url:
            link_cell += _copy_block(sum_txt, url, f"row-{r.id}")
        n_txt = f"{v['n_msgs']} 条" if v["n_msgs"] > 1 else "单条"
        state_cls = "ok" if v["active"] else ""
        sum_cell = (f"<td class='dim' style='max-width:150px;font-size:11px;"
                    f"overflow:hidden;text-overflow:ellipsis;white-space:nowrap' "
                    f"title='{_esc(sum_txt)}'>{_esc(sum_txt[:24])}</td>"
                    if sum_txt else "<td class='dim'>—</td>")
        # 一图流卡片：有效链接才给（撤销/过期的卡片没意义）
        card_cell = ""
        if v["active"]:
            card_cell = (f"<a class='btn sm' href='/share/{r.id}/card' "
                         f"target='_blank'>一图流</a> "
                         f"<a class='btn sm ghost' href='/share/{r.id}/card?dl=1' "
                         f"target='_blank'>下载</a> "
                         f"<a class='btn sm ghost' href='/share/{r.id}/card.zip'>ZIP</a>")
        out.append(
            f"<tr><td>{r.id}</td>"
            f"<td><span class='tag {state_cls}'>{v['state']}</span></td>"
            f"<td class='dim'>{n_txt}</td>"
            f"{sum_cell}"
            f"<td class='dim'>{r.expires_at.strftime('%m-%d %H:%M')}</td>"
            f"<td>{r.views}</td>"
            f"<td>{link_cell}</td>"
            f"<td>{card_cell}</td>"
            + ("" if r.revoked else
               f"<td><button class='btn sm ghost' hx-post='/share/{r.id}/revoke' "
               f"hx-target='#share-box-{mid}' hx-swap='innerHTML'>撤销</button></td>")
            + "</tr>")
    out.append("</table></div>")
    return "".join(out)


# ---------------- 分享管理页 ----------------

@router.get("/shares")
async def shares_page(request: Request,
                      user: str = Depends(require_admin)):
    """全部分享链接集中管理：URL、状态、访问数、撤销/恢复、续期。"""
    with session_scope() as s:
        rows = (s.query(ShareToken)
                .order_by(ShareToken.id.desc()).limit(200).all())
        views = _share_views(rows, request)
        # 消息预览文本（一图流的概括优先，其次锚点消息的正文前 40 字）
        anchor_ids = {r.message_id for r in rows}
        previews = {}
        if anchor_ids:
            for m in (s.query(MonitorMessage)
                      .filter(MonitorMessage.id.in_(anchor_ids)).all()):
                previews[m.id] = (m.text_zh or m.text_raw or "（无文本）")[:40]
    items = []
    for r in rows:
        v = views[r.id]
        # 概括自动生成后普遍更长，预览放宽到 60 字（悬停有 title 全文）
        v["preview"] = ((r.summary or "").strip()[:60]
                        or previews.get(r.message_id, "（消息已清理）"))
        items.append(v)
    return render(request, "shares.html", {
        "nav_active": "shares", "items": items,
    })


@router.post("/shares/{sid}/revoke")
async def revoke_share_mgmt(sid: int, request: Request,
                            user: str = Depends(require_admin)):
    """管理页撤销（与消息页撤销同效，行为是整页跳回管理页）。"""
    with session_scope() as s:
        row = s.get(ShareToken, sid)
        if row is None:
            return HTMLResponse("<span class='err'>链接不存在</span>")
        row.revoked = True
    logger.info("撤销分享链接 #%s（管理页，by %s）", sid, user)
    return redirect("/shares")


@router.post("/shares/{sid}/restore")
async def restore_share(sid: int, request: Request,
                        user: str = Depends(require_admin)):
    """恢复已撤销的链接（过期时间不变；已过期的先续期）。"""
    with session_scope() as s:
        row = s.get(ShareToken, sid)
        if row is None:
            return HTMLResponse("<span class='err'>链接不存在</span>")
        row.revoked = False
    logger.info("恢复分享链接 #%s（by %s）", sid, user)
    return redirect("/shares")


@router.post("/shares/{sid}/extend")
async def extend_share(sid: int, request: Request,
                       hours: str = Form("7d"),
                       user: str = Depends(require_admin)):
    """续期：expires_at = max(now, 当前值) + 时长。"""
    add = _EXTEND_CHOICES.get(hours, 24 * 7)
    with session_scope() as s:
        row = s.get(ShareToken, sid)
        if row is None:
            return HTMLResponse("<span class='err'>链接不存在</span>")
        now = datetime.utcnow()
        base = max(now, row.expires_at)
        row.expires_at = base + timedelta(hours=add)
        row.revoked = False
    logger.info("续期分享链接 #%s（+%sh，by %s）", sid, add, user)
    return redirect("/shares")


# ---------------- 访客侧（免登录） ----------------

def _load_share(token: str) -> ShareToken | None:
    """校验 token：存在、未撤销、未过期。三者任一不满足都返回 None
    （对外面统一表现成 404，不泄露哪个条件不满足）。"""
    with session_scope() as s:
        row = (s.query(ShareToken)
               .filter(ShareToken.token_hash == _hash(token)).first())
        if row is None or row.revoked or row.expires_at < datetime.utcnow():
            return None
        return row


@router.get("/s/{token}")
async def share_page(token: str, request: Request):
    if not _PAGE_LIMITER.check(f"ip:{client_ip(request)}"):
        raise HTTPException(429, "访问过于频繁，稍后再试")
    with session_scope() as s:
        sh = (s.query(ShareToken)
              .filter(ShareToken.token_hash == _hash(token)).first())
        if sh is None or sh.revoked or sh.expires_at < datetime.utcnow():
            return _gone_page()
        msgs = []
        if sh.snapshot_items:
            from ...sharing import snapshot_views
            items = snapshot_views(sh.snapshot_items)
        else:
            mids = _share_msg_ids(sh)
            msgs = (s.query(MonitorMessage)
                    .filter(MonitorMessage.id.in_(mids))
                    .order_by(MonitorMessage.published_at.asc()).all())
            if not msgs:
                # 旧链接没有快照，消息被 TTL 清理后自然失效。
                return _gone_page()
            chan_names = {c.id: c.title for c in s.query(Channel).all()}
            items = []
            for msg in msgs:
                media = (s.query(MessageMedia)
                         .filter(MessageMedia.message_id == msg.id).all())
                v = _row_view(msg, media)
                v["channel"] = chan_names.get(msg.channel_id, "?")
                items.append(v)
        sh.views += 1
        expires_at = sh.expires_at
        share_id = sh.id
        summary = (sh.summary or "").strip()
    if not summary and not sh.snapshot_items:
        summary = await _gen_summary([(m.get("text_zh") or m.get("text_raw") or "") for m in items],
                                     single=len(items) == 1)
        with session_scope() as s:
            row = s.get(ShareToken, share_id)
            if row is not None and not (row.summary or "").strip():
                row.summary = summary
    share_url = f"{_base_url(request)}/s/{token}"
    # 旧版 FastAPI 签名：(name, context)，request 必须进 context
    return templates.TemplateResponse("share.html", {
        "request": request, "items": items, "m": items[0],
        "expires_at": expires_at,
        "share_token": token,
        "summary": summary,
        "share_url": share_url,
        "copy_text": "\n\n".join(x for x in [summary, share_url] if x),
        "share_id": share_id,
    })


def _gone_page() -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>链接无效 · tgmon</title>"
        "<style>body{font:14px/1.6 system-ui,sans-serif;background:#0f1115;"
        "color:#e6e8ec;display:flex;align-items:center;justify-content:center;"
        "min-height:100vh;margin:0}div{text-align:center}"
        "a{color:#4d9fff}</style></head><body><div>"
        "<h1>链接无效或已过期</h1>"
        "<p>分享链接有有效期，也可能已被撤销。</p>"
        "</div></body></html>", status_code=404)


def _serve_shared_media(token: str, media_id: int, request: Request,
                        download: bool, video: bool = False) -> FileResponse:
    """token 授权的媒体下发。校验链：token 有效 → media 存在 →
    media.message_id ∈ token 授权的消息集合（单条/合并统一）。
    """
    if not _DL_LIMITER.check(f"ip:{client_ip(request)}"):
        raise HTTPException(429, "下载过于频繁，稍后再试")
    with session_scope() as s:
        sh = (s.query(ShareToken)
              .filter(ShareToken.token_hash == _hash(token)).first())
        if sh is None or sh.revoked or sh.expires_at < datetime.utcnow():
            raise HTTPException(404)
        m = s.get(MessageMedia, media_id)
        allowed = ({int(mid) for item in sh.snapshot_items
                    for mid in item.get("source_ids", [item["id"]])}
                   if sh.snapshot_items else set(_share_msg_ids(sh)))
        allowed_media = ({int(mid) for item in sh.snapshot_items
                          for mid in item.get("media_ids", [])}
                         if sh.snapshot_items else None)
        if m is None or m.message_id not in allowed or (sh.snapshot_items and
            media_id not in allowed_media):
            raise HTTPException(404)
        if video and m.kind == "video" and m.video_path:
            src = VIDEO_DIR / m.video_path
            mt = "video/mp4"
            name = src.name
        elif m.thumb_path:
            src = MEDIA_DIR / m.thumb_path
            mt = "image/webp"
            name = src.name
        else:
            raise HTTPException(404)
    src = src.resolve()
    base = VIDEO_DIR if mt == "video/mp4" else MEDIA_DIR
    if not src.is_relative_to(base.resolve()) or not src.is_file():
        raise HTTPException(404)
    headers = {}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return FileResponse(src, media_type=mt, headers=headers)


@router.get("/s/{token}/media/{media_id}")
async def share_media(token: str, media_id: int, request: Request,
                      dl: str = ""):
    return _serve_shared_media(token, media_id, request, dl == "1")


# /s/{token}/video/{id} 与 /s/{token}/media/{id} 走同一实现 ——
# 保留两个路径是给模板语义区分（视频/图片），行为一致
@router.get("/s/{token}/video/{media_id}")
async def share_video(token: str, media_id: int, request: Request,
                      dl: str = ""):
    return _serve_shared_media(token, media_id, request, dl == "1", video=True)
