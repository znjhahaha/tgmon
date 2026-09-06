"""② 账号与凭据 —— 填 API_ID/API_HASH/手机号，并在网页里完成登录。

登录靠任务队列绕开「Telethon 需要 stdin」的限制：这里只写任务，worker 执行。
敏感字段只写不读，已存的值显示为点，可覆盖不可回显。
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from ... import settings
from ...crypto import hash_password, mask
from ...db import session_scope
from ...models import AdminUser, AppSetting
from ...paths import USER_SESSION
from ...util import log_event
from ..deps import (
    get_task, render, require_admin, require_admin_api, require_worker,
    submit_task, touch_restart, worker_status,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/account")

# 官方客户端的 api_id，从各家开源代码里扒出来的，网上到处流传。
# 用它们登录的风险不在"会不会被吊销"，而在：
#   1. 指纹对不上 —— api_id 声称是 Desktop，但 MTProto 行为、设备串、调用
#      序列全是 Telethon 的。服务端一眼能看出来
#   2. 信誉按 api_id 池化 —— 这些 id 被全世界的爬虫群发工具共用，你继承的是
#      别人几年攒下的黑历史
# 自己注册免费、两分钟。所以这里只提醒，不阻止。
PUBLIC_API_IDS: dict[str, str] = {
    "4": "Telegram Android",
    "5": "PublicStaticFinal",
    "6": "Distributed Android",
    "8": "Public iOS Beta",
    "2040": "Telegram Desktop",
    "2496": "Telegram Web",
    "10840": "Telegram Swift (macOS)",
    "11535358": "Nagram",
    "16623": "Telegram Messenger Plus",
    "21724": "Telegram X",
    "94575": "Nicegram",
    "1025907": "Telegram Web K",
}


def _public_id_warning(api_id: str) -> str:
    """填了公共 api_id 时给出的提醒文案。返回空串表示没问题。"""
    who = PUBLIC_API_IDS.get((api_id or "").strip())
    if not who:
        return ""
    return (
        f"<div class='flash err'><b>这是 {who} 的公共 api_id</b>，"
        "不是你自己注册的。<br>"
        "风险不是它会失效，而是：① 指纹对不上 —— 它声称自己是官方客户端，"
        "但发出去的是 Telethon 的行为特征；② 信誉按 api_id 池化 —— 这个 id "
        "被全世界的爬虫群发工具共用，你继承别人攒的黑历史。<br>"
        "<b>新号 + 公共 api_id + 立刻加一堆频道</b> 是最典型的封号组合。"
        "去 <a href='https://my.telegram.org' target='_blank' "
        "rel='noreferrer'>my.telegram.org</a> 自己注册一个，免费、两分钟。"
        "</div>")


def _stored(key: str) -> str:
    with session_scope() as s:
        row = s.get(AppSetting, key)
        return row.value if row else ""


def _ctx() -> dict:
    return {
        "api_id": str(settings.get("API_ID") or ""),
        "api_hash_mask": mask(_stored("API_HASH")),
        "phone_mask": mask(_stored("PHONE_NUMBER")),
        "bot_token_mask": mask(_stored("BOT_TOKEN")),
        "alert_chat_id": str(settings.get("ALERT_CHAT_ID") or ""),
        "public_id_owner": PUBLIC_API_IDS.get(
            str(settings.get("API_ID") or "").strip()),
        "session_exists": USER_SESSION.exists(),
        "worker": worker_status(),
    }


def _accounts() -> list[dict]:
    with session_scope() as s:
        rows = s.query(AdminUser).order_by(AdminUser.id.asc()).all()
        return [{"id": r.id, "username": r.username, "role": r.role or "admin",
                 "created_at": r.created_at, "last_login_at": r.last_login_at}
                for r in rows]


@router.get("")
async def page(request: Request, user: str = Depends(require_admin)):
    return render(request, "account.html",
                  {"nav_active": "account", **_ctx(), "accounts": _accounts()})


# ---------------- 访客账号管理 ----------------

@router.post("/guest/create")
async def create_guest(request: Request,
                       username: str = Form(...),
                       password: str = Form(...),
                       user: str = Depends(require_admin)):
    """创建访客账号：只能看消息浏览 + RSS 清单，管理页 403，限流 30 req/min。"""
    username = username.strip()
    if not (3 <= len(username) <= 60):
        return HTMLResponse("<div class='flash err'>用户名 3-60 个字符</div>")
    if len(password) < 8:
        return HTMLResponse("<div class='flash err'>初始密码至少 8 位</div>")
    with session_scope() as s:
        dup = (s.query(AdminUser)
               .filter(AdminUser.username == username).first())
        if dup:
            return HTMLResponse(f"<div class='flash err'>用户名 {username} 已存在</div>")
        s.add(AdminUser(username=username,
                        password_hash=hash_password(password), role="guest"))
    logger.info("创建访客账号 %s（by %s）", username, user)
    log_event("info", "auth", f"管理员 {user} 创建了访客账号 {username}")
    return HTMLResponse(
        f"<div class='flash ok'>访客账号 {username} 已创建。把用户名和初始密码"
        "交给对方，首次登录后建议自行改密（访客改密走 /account/password）</div>")


@router.post("/guest/{uid}/delete")
async def delete_guest(uid: int, request: Request,
                       user: str = Depends(require_admin)):
    """删除账号。管理员自己删不了自己（防手滑把自己锁在门外）。"""
    with session_scope() as s:
        row = s.get(AdminUser, uid)
        if row is None:
            return HTMLResponse("<div class='flash err'>账号不存在</div>")
        if row.username == user:
            return HTMLResponse("<div class='flash err'>不能删除当前登录的账号</div>")
        name, role = row.username, row.role
        s.delete(row)
    logger.info("删除账号 %s（role=%s，by %s）", name, role, user)
    return HTMLResponse(f"<div class='flash ok'>账号 {name} 已删除。"
                        "对方浏览器里的会话随之失效（签发时角色已在 cookie 里）</div>"
                        "<div class='hint'>提示：删除的是登录能力，不会踢掉仍有效的"
                        " cookie —— 其最长寿命 7 天，或点「改密码」使其失效。</div>")


@router.post("/credentials")
async def save_credentials(request: Request,
                           api_id: str = Form(""),
                           api_hash: str = Form(""),
                           phone_number: str = Form(""),
                           bot_token: str = Form(""),
                           alert_chat_id: str = Form(""),
                           user: str = Depends(require_admin)):
    api_id = api_id.strip()
    if api_id and not api_id.isdigit():
        return HTMLResponse("<div class='flash err'>API_ID 必须是纯数字</div>")

    old_id = str(settings.get("API_ID") or "")
    settings.set_many({
        "API_ID": api_id,
        "API_HASH": api_hash.strip(),
        "PHONE_NUMBER": phone_number.strip(),
        "BOT_TOKEN": bot_token.strip(),
        "ALERT_CHAT_ID": alert_chat_id.strip(),
    })

    note = ""
    if api_id and old_id and api_id != old_id:
        note = ("　API_ID 变了 —— 等于换应用，现有 session 会失效，需要重新登录。")
    elif api_hash.strip() and USER_SESSION.exists():
        note = "　改了 API_HASH，如果登录状态异常就重新登录一次。"

    warn = _public_id_warning(api_id)
    if warn:
        logger.warning("填入了公共 api_id %s（%s）—— 封号风险显著升高",
                       api_id, PUBLIC_API_IDS.get(api_id))

    touch_restart()  # worker 会重建客户端
    return HTMLResponse(
        f"<div class='flash ok'>已保存。{note}</div>" + warn
        + "<div class='hint'>敏感字段只写不读：已存的值显示为点，留空表示不改。</div>")


# ---------------- 网页登录流程 ----------------

@router.post("/send-code")
async def send_code(request: Request, user: str = Depends(require_admin)):
    require_worker()
    if not settings.get("API_ID") or not settings.get("API_HASH"):
        return HTMLResponse(
            "<div class='flash err'>先填 API_ID 和 API_HASH 再发验证码</div>")
    if not settings.get("PHONE_NUMBER"):
        return HTMLResponse("<div class='flash err'>先填手机号</div>")
    tid = submit_task("send_code", {})
    return await _await_task(request, tid, "code")


@router.post("/sign-in")
async def sign_in(request: Request, code: str = Form(...),
                  user: str = Depends(require_admin)):
    require_worker()
    tid = submit_task("sign_in", {"code": code.strip()})
    return await _await_task(request, tid, "signin")


@router.post("/sign-in-2fa")
async def sign_in_2fa(request: Request, password: str = Form(...),
                      user: str = Depends(require_admin)):
    require_worker()
    tid = submit_task("sign_in_2fa", {"password": password})
    return await _await_task(request, tid, "signin")


@router.post("/logout-tg")
async def logout_tg(request: Request, user: str = Depends(require_admin)):
    require_worker()
    tid = submit_task("logout", {})
    return await _await_task(request, tid, "logout")


async def _await_task(request: Request, task_id: int, stage: str,
                      timeout: float = 45.0):
    """等 worker 把任务跑完。登录这几步都是秒级，直接同步等更简单。"""
    waited = 0.0
    while waited < timeout:
        t = get_task(task_id)
        if t and t["status"] in ("done", "failed"):
            return _render_stage(request, t, stage)
        await asyncio.sleep(0.6)
        waited += 0.6
    return HTMLResponse(
        "<div class='flash err'>等待 worker 超时。看 docker compose logs worker</div>")


def _render_stage(request: Request, t: dict, stage: str) -> HTMLResponse:
    if t["status"] == "failed":
        return HTMLResponse(
            f"<div class='flash err'>{_esc(t['error'] or '失败')}</div>"
            + _form_for(stage))

    res = t["result"] or {}
    if stage == "code":
        return HTMLResponse(
            "<div class='flash ok'>验证码已发送 —— 注意它发到你的 Telegram "
            "客户端，不是短信</div>" + _code_form())
    if stage == "signin":
        if res.get("need_2fa"):
            return HTMLResponse(
                "<div class='flash'>这个账号开了两步验证，再填一次密码</div>"
                + _twofa_form())
        who = _esc(str(res.get("user") or ""))
        return HTMLResponse(
            f"<div class='flash ok'>登录成功：{who}。"
            "现在去<a href='/channels'>频道管理</a>点同步</div>")
    if stage == "logout":
        return HTMLResponse(
            "<div class='flash ok'>已登出并删除本地 session</div>"
            + _send_code_form())
    return HTMLResponse("<div class='flash ok'>完成</div>")


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _send_code_form() -> str:
    return ("<form hx-post='/account/send-code' hx-target='#login-flow' "
            "hx-swap='innerHTML' hx-indicator='#lf-spin'>"
            "<button class='btn'>发送验证码</button>"
            "<span id='lf-spin' class='htmx-indicator'>…</span></form>")


def _code_form() -> str:
    return ("<form hx-post='/account/sign-in' hx-target='#login-flow' "
            "hx-swap='innerHTML' hx-indicator='#lf-spin'>"
            "<label>验证码<input name='code' inputmode='numeric' "
            "autocomplete='one-time-code' required></label>"
            "<button class='btn'>登录</button>"
            "<span id='lf-spin' class='htmx-indicator'>…</span></form>")


def _twofa_form() -> str:
    return ("<form hx-post='/account/sign-in-2fa' hx-target='#login-flow' "
            "hx-swap='innerHTML' hx-indicator='#lf-spin'>"
            "<label>两步验证密码<input name='password' type='password' "
            "required></label><button class='btn'>继续</button>"
            "<span id='lf-spin' class='htmx-indicator'>…</span></form>")


def _form_for(stage: str) -> str:
    if stage == "code":
        return _send_code_form()
    if stage == "signin":
        return _code_form()
    return _send_code_form()


@router.get("/fragment/login-flow")
async def login_flow_fragment(request: Request,
                              user: str = Depends(require_admin_api)):
    st = worker_status()
    if st["status"] == "online":
        return HTMLResponse(
            f"<div class='flash ok'>已登录：{_esc(str(st['tg_user'] or ''))}</div>"
            "<form hx-post='/account/logout-tg' hx-target='#login-flow' "
            "hx-confirm='登出会删除 session，需要重新验证码登录。继续？'>"
            "<button class='btn danger'>登出</button></form>")
    if st["status"] == "need_credentials":
        return HTMLResponse(
            "<div class='flash'>还没填 API_ID / API_HASH。填完上面那栏再回来</div>")
    if not st["online"]:
        return HTMLResponse(
            "<div class='flash err'>worker 不在线，无法登录。"
            "<code>docker compose logs worker</code></div>")
    return HTMLResponse(_send_code_form())
