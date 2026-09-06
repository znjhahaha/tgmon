"""⑩ QQ 机器人 —— 官方群推送的配置与群管理。

页面能做的：总开关 / AppID+Secret / 频道过滤 / 群启停与标注 / 发测试消息 /
投递记录。进群事件由 q.qq.com 的回调自动记录（见 qqbot_cb.py），这里只管
看和调。
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from ... import outputs, settings
from ...crypto import mask
from ...db import session_scope
from ...models import Channel, QqDelivery, QqGroup, QqInbound, Conversation
from ...util import log_event
from ..deps import redirect, render, require_admin
from ... import qqbot

logger = logging.getLogger(__name__)
# 页面与操作都挂 /qqbot 下；腾讯回调占 /qqbot/callback（见 qqbot_cb.py），
# 其余路径不冲突
router = APIRouter(prefix="/qqbot")

_OPENID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def _bridge_state() -> tuple[bool, str]:
    """(在线?, 人类可读的最后心跳时间)。在线 = 3 分钟内心跳过。"""
    last_seen = str(settings.get("QQ_BRIDGE_LAST_SEEN") or "")
    if not last_seen:
        return False, "从未"
    try:
        delta = (datetime.utcnow() - datetime.fromisoformat(last_seen)) \
            .total_seconds()
    except ValueError:
        return False, "时间格式异常"
    if delta < 0:
        return True, "刚刚"
    if delta < 180:
        return True, f"{int(delta)} 秒前"
    if delta < 3600:
        return False, f"{int(delta / 60)} 分钟前"
    if delta < 86400:
        return False, f"{int(delta / 3600)} 小时前"
    return False, f"{int(delta / 86400)} 天前"


def _ctx() -> dict:
    with session_scope() as s:
        groups = []
        for g in s.query(QqGroup).order_by(QqGroup.id.asc()).all():
            pend = (s.query(QqDelivery)
                    .filter(QqDelivery.group_openid == g.group_openid,
                            QqDelivery.status == "retry").count())
            fail = (s.query(QqDelivery)
                    .filter(QqDelivery.group_openid == g.group_openid,
                            QqDelivery.status == "failed").count())
            groups.append({
                "id": g.id, "openid": g.group_openid, "nickname": g.nickname,
                "enabled": g.enabled, "dropped": g.dropped_at is not None,
                "sent_today": g.sent_today, "sent_date": g.sent_date,
                "last_sent_at": g.last_sent_at, "added_at": g.added_at,
                "pending": pend, "failed": fail,
            })
        recent = []
        for d in (s.query(QqDelivery)
                  .order_by(QqDelivery.id.desc()).limit(30).all()):
            nick = ""
            g = (s.query(QqGroup)
                 .filter(QqGroup.group_openid == d.group_openid).first())
            if g is not None:
                nick = g.nickname or d.group_openid[:8]
            recent.append({
                "id": d.id, "group": nick, "message_id": d.message_id,
                "status": d.status, "attempts": d.attempts,
                "error": d.error, "created_at": d.created_at,
                "delivered_at": d.delivered_at,
            })
        channels = [{"id": c.id, "title": c.title, "game": c.game or ""}
                    for c in s.query(Channel)
                    .order_by(Channel.title.asc()).all()]
        # 入站消息（群 @ / C2C 留痕），来源显示群名或「私聊」
        inbound = []
        for m in (s.query(QqInbound)
                  .order_by(QqInbound.id.desc()).limit(50).all()):
            src = "私聊"
            if m.group_openid:
                g = (s.query(QqGroup)
                     .filter(QqGroup.group_openid == m.group_openid).first())
                src = (g.nickname or m.group_openid[:10]) if g \
                    else m.group_openid[:10]
            inbound.append({
                "id": m.id, "event_type": m.event_type, "source": src,
                "member": (m.member_openid[:10] + "…") if m.member_openid else "",
                "content": m.content, "reply": m.reply,
                "created_at": m.created_at,
            })
    chan_ids = settings.get("QQ_CHANNEL_IDS") or []
    bridge_online, bridge_seen = _bridge_state()
    return {
        "qq_enabled": settings.get("QQ_ENABLED"),
        "app_id": str(settings.get("QQ_APP_ID") or ""),
        "secret_mask": mask(settings.get("QQ_APP_SECRET") or ""),
        "channel_ids": [int(x) for x in chan_ids],
        "include_dups": settings.get("QQ_INCLUDE_DUPS"),
        "daily_limit": settings.get("QQ_DAILY_LIMIT"),
        "groups": groups, "recent": recent, "channels": channels,
        "callback_url": (outputs.base_url() or "https://tgmon.example.com")
                        + "/qqbot/callback",
        "bridge_online": bridge_online, "bridge_seen": bridge_seen,
        "bridge_token_set": bool(str(settings.get("QQ_BRIDGE_TOKEN") or "")),
        "ai_enabled": settings.get("QQ_AI_ENABLED"),
        "ai_system": str(settings.get("QQ_AI_SYSTEM") or ""),
        "agent_system": str(settings.get("QQ_AGENT_SYSTEM") or ""),
        "ai_fallback": settings.get("QQ_AI_FALLBACK"),
        "ai_agent": settings.get("QQ_AI_AGENT_ENABLED"),
        "daily_digest": settings.get("QQ_DAILY_DIGEST_ENABLED"),
        "relay_repo": str(settings.get("GITHUB_RELAY_REPO") or ""),
        "relay_token_mask": mask(settings.get("GITHUB_RELAY_TOKEN") or ""),
        "inbound": inbound,
        "retrieval_enabled": settings.get("RETRIEVAL_ENABLED"),
        "embedding_enabled": settings.get("EMBEDDING_ENABLED"),
        "memory_enabled": settings.get("MEMORY_ENABLED"),
    }


def _ints(raw: list[str] | None) -> list[int]:
    out = []
    for x in raw or []:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            pass
    return out


@router.get("")
@router.get("/")
async def page(request: Request, user: str = Depends(require_admin)):
    return render(request, "qqbot.html", {"nav_active": "qqbot", **_ctx()})


@router.post("/config")
async def save_config(request: Request,
                      app_id: str = Form(""),
                      app_secret: str = Form(""),
                      daily_limit: int = Form(950),
                      qq_enabled: str = Form(""),
                      include_dups: str = Form(""),
                      user: str = Depends(require_admin)):
    form = await request.form()
    chan_ids = _ints(form.getlist("channel_ids"))
    settings.set_many({
        "QQ_APP_ID": app_id.strip(),
        # is_secret 字段空值 = 保留原值（settings.set_many 的表单语义）
        "QQ_APP_SECRET": app_secret.strip(),
        "QQ_ENABLED": qq_enabled.strip().lower() in ("on", "1"),
        "QQ_INCLUDE_DUPS": include_dups.strip().lower() in ("on", "1"),
        "QQ_CHANNEL_IDS": chan_ids,
        "QQ_DAILY_LIMIT": max(1, min(daily_limit, 1000)),
    })
    # 凭证可能变了，token 缓存立即失效（admin 进程这边的）
    from ...qqbot import client as qq_client
    qq_client.invalidate_token()
    return HTMLResponse("<div class='flash ok'>已保存。换过 AppSecret 的话"
                        "先点一次「自检密钥与回调」确认密钥有效。</div>")


@router.post("/selfcheck")
async def selfcheck(request: Request, user: str = Depends(require_admin)):
    """密钥自检：用当前 AppSecret 换一次 access_token。

    密钥不变量：能换到 access_token 的那个才是签名密钥 —— 回调验签与
    getAppAccessToken 共用 bot_secret()（官方 sign.html 的 Bot Secret =
    clientSecret）。换 token 失败（如 100016 invalid）说明填的是
    「机器人令牌」或别的错值，此时 op13 握手必然 13007。
    """
    from ...qqbot import client as qq_client
    app_id, secret = qq_client._credentials()
    if not app_id or not secret:
        return HTMLResponse("<div class='flash err'>AppID / AppSecret 未配置，"
                            "先在上面填好保存。</div>")
    try:
        await qq_client._fetch_token(app_id, secret)
    except qq_client.QqApiError as e:
        return HTMLResponse(
            f"<div class='flash err'>密钥无效：换 access_token 失败"
            f"（{e.code} {e.message}）。应填管理端「机器人密钥」(AppSecret)，"
            f"不是「机器人令牌」。</div>")
    except Exception as e:
        return HTMLResponse(f"<div class='flash err'>网络错误：{e}</div>")
    fp = qq_client.public_key_fingerprint(secret)
    return HTMLResponse(
        f"<div class='flash ok'>密钥有效，已换到 access_token。"
        f"公钥指纹 <code>{fp}</code>（与日志 op13 应答里的 pubfp 应一致），"
        f"回调地址 <code>{outputs.base_url() or 'https://tgmon.example.com'}"
        f"/qqbot/callback</code></div>")


@router.post("/groups/create")
async def group_create(request: Request, openid: str = Form(""),
                       nickname: str = Form(""),
                       user: str = Depends(require_admin)):
    openid = openid.strip()
    if not _OPENID_RE.match(openid):
        return HTMLResponse("<div class='flash err'>openid 格式不对。正确值"
                            "在机器人进群后自动出现；手动填一般是进群回调"
                            "丢了才需要。</div>")
    with session_scope() as s:
        if s.query(QqGroup).filter(QqGroup.group_openid == openid).first():
            return HTMLResponse("<div class='flash err'>这个 openid 已存在</div>")
        s.add(QqGroup(group_openid=openid, nickname=nickname.strip()))
    return redirect("/qqbot")


@router.post("/groups/{gid}/update")
async def group_update(gid: int, request: Request,
                       nickname: str = Form(""),
                       user: str = Depends(require_admin)):
    with session_scope() as s:
        g = s.get(QqGroup, gid)
        if g is None:
            return HTMLResponse("<div class='flash err'>不存在</div>")
        g.nickname = nickname.strip()
    return HTMLResponse("<div class='flash ok'>已保存</div>")


@router.post("/groups/{gid}/toggle")
async def group_toggle(gid: int, request: Request,
                       user: str = Depends(require_admin)):
    with session_scope() as s:
        g = s.get(QqGroup, gid)
        if g is None:
            return HTMLResponse("<span class='err'>不存在</span>")
        g.enabled = not g.enabled
        if g.enabled:
            g.dropped_at = None
        state = g.enabled
    label = "推送中" if state else "已停用"
    cls = "on" if state else "off"
    return HTMLResponse(
        f"<button class='pill {cls}' "
        f"hx-post='/qqbot/groups/{gid}/toggle' hx-swap='outerHTML'>{label}</button>")


@router.post("/groups/{gid}/delete")
async def group_delete(gid: int, request: Request,
                       user: str = Depends(require_admin)):
    with session_scope() as s:
        g = s.get(QqGroup, gid)
        if g is not None:
            s.delete(g)
            # 投递记录一并清掉，不然「最近投递」表里全是孤儿
            s.query(QqDelivery).filter(
                QqDelivery.group_openid == g.group_openid).delete()
    return redirect("/qqbot")


@router.post("/groups/{gid}/test")
async def group_test(gid: int, request: Request,
                     user: str = Depends(require_admin)):
    """直接在 admin 进程发（同 hook_test 的思路），不绕 worker。"""
    with session_scope() as s:
        g = s.get(QqGroup, gid)
        if g is None:
            return HTMLResponse("<span class='err'>群不存在</span>")
        openid = g.group_openid
    res = await qqbot.test_message(openid)
    if res.get("ok"):
        return HTMLResponse("<span class='ok'>已送达 QQ 群</span>")
    err = str(res.get("error") or "失败").replace("<", "&lt;")[:300]
    return HTMLResponse(f"<span class='err'>{err}</span>")


@router.post("/ai-config")
async def save_ai_config(request: Request,
                         ai_system: str = Form(""),
                         agent_system: str = Form(""),
                         user: str = Depends(require_admin)):
    """保存群内 AI 设置。checkbox 没勾 = 表单里没有这个字段。"""
    form = await request.form()
    ai_enabled = str(form.get("ai_enabled", "")).lower() in ("on", "1")
    ai_fallback = str(form.get("ai_fallback", "")).lower() in ("on", "1")
    ai_agent = str(form.get("ai_agent", "")).lower() in ("on", "1")
    daily_digest = str(form.get("daily_digest", "")).lower() in ("on", "1")
    settings.set_many({
        "QQ_AI_ENABLED": ai_enabled,
        "QQ_AI_FALLBACK": ai_fallback,
        "QQ_AI_AGENT_ENABLED": ai_agent,
        "QQ_DAILY_DIGEST_ENABLED": daily_digest,
        "QQ_AI_SYSTEM": ai_system.strip(),
        "QQ_AGENT_SYSTEM": agent_system.strip(),
    })
    return HTMLResponse("<div class='flash ok'>AI 设置已保存。"
                        "每日召回摘要依赖平台 is_wakeup 通道（群聊支持性"
                        "待实测，被拒会自动停用）。</div>")


@router.post("/memory/clear")
async def clear_memory(member_openid: str = Form(""),
                       user: str = Depends(require_admin)):
    from ...memory import forget
    oid = member_openid.strip()
    n = forget("user", oid) if oid else 0
    return HTMLResponse(f"<div class='flash ok'>已清理 {n} 条个人记忆</div>")


@router.post("/relay-config")
async def save_relay_config(request: Request,
                            relay_repo: str = Form(""),
                            relay_token: str = Form(""),
                            user: str = Depends(require_admin)):
    """保存图片中转（GitHub）配置。

    平台富媒体上传是腾讯机房来拉 URL，拉不动本站境外 IP（850027 超时），
    图片经 GitHub 公开仓库中转后实测可达。repo 留空 = 停用中转
    （退回本站直链，境外部署下图片发不出）。
    """
    repo = relay_repo.strip().strip("/")
    if repo and "/" not in repo:
        return HTMLResponse("<div class='flash err'>仓库格式应为 owner/name，"
                            "例如 myname/qq-media-relay</div>")
    settings.set_many({
        "GITHUB_RELAY_REPO": repo,
        # is_secret：留空 = 保留原值
        "GITHUB_RELAY_TOKEN": relay_token.strip(),
    })
    if not repo:
        return HTMLResponse("<div class='flash ok'>已停用图片中转（退回本站直链）。</div>")
    return HTMLResponse("<div class='flash ok'>已保存。可点「测试中转」用一张"
                        "真实监控图片走一遍完整链路（上传 GitHub → 平台拉取）。</div>")


@router.post("/relay-test")
async def relay_test(request: Request, user: str = Depends(require_admin)):
    """图片中转全链路自测：真实图片 → GitHub → QQ 平台拉取 → file_info。"""
    from ...qqbot import media as qq_media
    from ...db import session_scope
    from ...models import MessageMedia, QqGroup
    if not qq_media.relay_enabled():
        return HTMLResponse("<div class='flash err'>中转未配置（缺仓库或"
                            " Token），先保存上面的配置。</div>")
    with session_scope() as s:
        g = s.query(QqGroup).filter(QqGroup.enabled.is_(True)).first()
        row = (s.query(MessageMedia)
               .filter(MessageMedia.kind == "photo",
                       MessageMedia.thumb_path.isnot(None))
               .order_by(MessageMedia.id.desc()).first())
        if g is None or row is None:
            return HTMLResponse("<div class='flash err'>没有启用中的群或"
                                "库里没有图片消息，无法测试。</div>")
        openid, thumb = g.group_openid, row.thumb_path
    url = await qq_media.github_relay_url(thumb)
    if not url:
        return HTMLResponse("<div class='flash err'>上传 GitHub 失败——检查"
                            " Token 权限（需要该仓库 Contents 读写）与"
                            " 分支是否为 main。</div>")
    try:
        fi = await qq_media.client.upload_group_file(openid, 1, url)
    except Exception as e:
        return HTMLResponse(f"<div class='flash err'>GitHub 上传成功但平台"
                            f"拉取失败：{e}</div>")
    return HTMLResponse(f"<div class='flash ok'>链路全通！图片经 "
                        f"<code>{url[:70]}…</code> 中转，平台已受理"
                        f"（file_info 前 20 字符：<code>{str(fi)[:20]}…</code>）。"
                        f"群里 /latest 的图即走此通道。</div>")


@router.post("/bridge-token/renew")
async def bridge_token_renew(request: Request,
                             user: str = Depends(require_admin)):
    """重新生成 botpy 桥令牌。

    生成后需要同步更新服务器 botpy-official 容器的 BRIDGE_TOKEN
    环境变量并重启容器，否则桥会被 403 拒之门外。
    """
    token = secrets.token_hex(16)
    settings.set_many({"QQ_BRIDGE_TOKEN": token})
    log_event("info", "qqbot", "桥令牌已由管理页重新生成")
    return HTMLResponse(
        f"<div class='flash ok'>已生成新令牌（已自动保存）：<br>"
        f"<code>{token}</code><br>"
        f"需要在服务器更新 botpy-official 容器的 BRIDGE_TOKEN 环境变量"
        f"并重启容器，否则桥连不上。</div>")
