"""登录 / 登出 / 改密码。"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from ...crypto import hash_password, verify_password
from ...db import session_scope
from ...models import AdminUser
from ...util import log_event
from ..deps import (
    clear_cookie, clear_login_fails, client_ip, current_user, issue_cookie,
    login_delay_for, record_login_fail, redirect, render, require_login,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/login")
async def login_page(request: Request):
    if current_user(request):
        return redirect("/")
    with session_scope() as s:
        has_account = s.query(AdminUser).count() > 0
    return render(request, "login.html", {"has_account": has_account})


@router.post("/login")
async def login_submit(request: Request,
                       username: str = Form(...),
                       password: str = Form(...)):
    ip = client_ip(request)

    # 失败越多睡越久。放在校验之前，这样连"密码对不对"都要先等
    delay = login_delay_for(ip)
    if delay:
        logger.warning("登录限速：%s 已连续失败，延迟 %.1fs", ip, delay)
        await asyncio.sleep(delay)

    with session_scope() as s:
        row = (s.query(AdminUser)
               .filter(AdminUser.username == username.strip())
               .first())
        ok = row is not None and verify_password(password, row.password_hash)
        if ok:
            row.last_login_at = datetime.utcnow()
            role = row.role or "admin"
        has_account = s.query(AdminUser).count() > 0

    if not ok:
        n = record_login_fail(ip)
        logger.warning("登录失败: user=%s ip=%s（窗口内第 %d 次）", username, ip, n)
        if n >= 10:
            log_event("warning", "auth",
                      f"{ip} 连续 {n} 次登录失败，疑似爆破")
        return render(request, "login.html",
                      {"error": "用户名或密码不对", "has_account": has_account})

    clear_login_fails(ip)
    logger.info("登录成功: user=%s role=%s ip=%s", username, role, ip)
    resp = redirect("/" if role == "admin" else "/messages")
    # uvicorn 开了 proxy_headers，所以这里能看到 Caddy 转发的真实协议
    issue_cookie(resp, username.strip(), role=role,
                 secure=request.url.scheme == "https")
    return resp


@router.post("/logout")
async def logout():
    resp = redirect("/login")
    clear_cookie(resp)
    return resp


@router.post("/account/password")
async def change_password(request: Request,
                          old_password: str = Form(...),
                          new_password: str = Form(...),
                          user: str = Depends(require_login)):
    if len(new_password) < 8:
        return HTMLResponse("<div class='flash err'>新密码至少 8 位</div>")
    with session_scope() as s:
        row = s.query(AdminUser).filter(AdminUser.username == user).first()
        if row is None or not verify_password(old_password, row.password_hash):
            return HTMLResponse("<div class='flash err'>原密码不对</div>")
        row.password_hash = hash_password(new_password)
    return HTMLResponse("<div class='flash ok'>密码已改。下次登录用新密码</div>")
