"""后台登录态、任务下发、模板环境。"""
from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .. import settings
from ..crypto import _load_key
from ..db import session_scope
from ..models import Task, WorkerState
from ..util import ago, fmt_local, human_bytes, truncate

logger = logging.getLogger(__name__)

COOKIE_NAME = "tgmon_session"
SESSION_MAX_AGE = 7 * 24 * 3600

# ---------------- 登录暴力破解防护 ----------------
# 后台是公网可达的，且能改凭据、能看全部消息。scrypt 让每次尝试有成本
# （约 0.1s + 32MB 内存），但挡不住持续爆破。这里按 IP 记失败次数并递增延迟。
# 存进程内存不进 DB：admin 只有一个进程，重启清零可以接受 —— 攻击者拿不到
# 重启时机，而合法用户也不该被永久锁在门外。
_LOGIN_FAILS: dict[str, list[float]] = {}
_FAIL_WINDOW = 900.0       # 15 分钟内的失败才算
_FAIL_FREE = 5             # 前 5 次不惩罚，手滑不该被拖慢
_FAIL_MAX_DELAY = 8.0      # 单次最多拖 8 秒


def client_ip(request: Request) -> str:
    """取真实客户端 IP。Caddy 在前面，所以优先看 X-Forwarded-For 的第一跳。"""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


def login_delay_for(ip: str) -> float:
    """返回这次登录该先睡多久。指数退避，封顶 8s。"""
    now = time.time()
    fails = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < _FAIL_WINDOW]
    _LOGIN_FAILS[ip] = fails
    over = len(fails) - _FAIL_FREE
    if over <= 0:
        return 0.0
    return min(_FAIL_MAX_DELAY, 0.5 * (2 ** min(over, 5)))


def record_login_fail(ip: str) -> int:
    now = time.time()
    fails = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < _FAIL_WINDOW]
    fails.append(now)
    _LOGIN_FAILS[ip] = fails
    # 只保留窗口内的，且给单 IP 上限，避免被灌爆内存
    if len(fails) > 200:
        _LOGIN_FAILS[ip] = fails[-200:]
    # 顺手清理其他 IP 的过期记录，别让字典无限长
    if len(_LOGIN_FAILS) > 500:
        for k in [k for k, v in _LOGIN_FAILS.items()
                  if not any(now - t < _FAIL_WINDOW for t in v)]:
            _LOGIN_FAILS.pop(k, None)
    return len(fails)


def clear_login_fails(ip: str) -> None:
    _LOGIN_FAILS.pop(ip, None)

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
templates.env.filters["fmt_local"] = fmt_local
templates.env.filters["ago"] = ago
templates.env.filters["human_bytes"] = human_bytes
templates.env.filters["truncate_text"] = truncate

_serializer = URLSafeTimedSerializer(_load_key().decode("ascii"), salt="tgmon-admin")


def issue_cookie(response, username: str, role: str = "admin",
                 secure: bool = True) -> None:
    """下发会话 cookie。载荷带 role，之后不用每请求查 DB。

    secure 默认开：Caddy 把所有 HTTP 都跳到 HTTPS，不存在明文访问的正常路径，
    自签证书也照样算 secure 上下文。留参数是为了本地不带 TLS 调试时能关掉。
    """
    token = _serializer.dumps({"u": username, "r": role})
    response.set_cookie(
        COOKIE_NAME, token, max_age=SESSION_MAX_AGE, httponly=True,
        samesite="lax", secure=secure,
    )


def clear_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME)


def _load_session(request: Request) -> dict | None:
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return None
    try:
        data = _serializer.loads(raw, max_age=SESSION_MAX_AGE)
        return data if isinstance(data, dict) else None
    except (BadSignature, Exception):
        return None


def current_user(request: Request) -> str | None:
    data = _load_session(request)
    return data.get("u") if data else None


def current_role(request: Request) -> str:
    """当前登录角色。旧 cookie（无 r 字段）一律视为 admin ——
    角色功能上线前签发的全是管理员会话，不能要求全员重登。"""
    data = _load_session(request)
    if not data:
        return ""
    return data.get("r") or "admin"


def require_login(request: Request) -> str:
    """页面用。未登录抛 303 跳登录页。admin 与 guest 都算已登录。"""
    user = current_user(request)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
            detail="需要登录",
        )
    return user


def require_admin(request: Request) -> str:
    """管理操作 / 管理页面用。访客（guest）一律 403。"""
    user = require_login(request)
    if current_role(request) != "admin":
        raise HTTPException(status_code=403, detail="此操作需要管理员账号")
    return user


def require_admin_api(request: Request) -> str:
    """HTMX 片段用的管理版：未登录 401（前端刷新到登录页），
    已登录但非 admin 403。"""
    user = require_login_api(request)
    if current_role(request) != "admin":
        raise HTTPException(status_code=403, detail="此操作需要管理员账号")
    return user


def require_login_api(request: Request) -> str:
    """HTMX 片段用。未登录返回 401，前端会整页刷新到登录页。"""
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="需要登录")
    return user


# ---------------- 访客限流 ----------------
# guest 账号全站 30 req/min（按 用户名+IP）。内存令牌桶即可：
# admin 单进程、访客量级是个位数，不值得引 Redis。admin 不限。

GUEST_RATE_PER_MIN = 30


class RateLimiter:
    """滑动窗口计数器。check() 通过返回 True，超限返回 False。"""

    def __init__(self, limit_per_min: int, window: float = 60.0):
        self.limit = limit_per_min
        self.window = window
        self._hits: dict[str, list[float]] = {}

    def check(self, key: str, now: float | None = None) -> bool:
        import time as _time
        now = now if now is not None else _time.time()
        hits = [t for t in self._hits.get(key, []) if now - t < self.window]
        if len(hits) >= self.limit:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        # key 过多时清理过期桶，防内存无限涨
        if len(self._hits) > 10000:
            self._hits = {
                k: v for k, v in self._hits.items()
                if v and now - v[-1] < self.window
            }
        return True


_GUEST_LIMITER = RateLimiter(GUEST_RATE_PER_MIN)


def guest_rate_limit(request: Request) -> None:
    """依赖：guest 角色限流，admin 放行。挂在 guest 可访问的路由上。"""
    if current_role(request) != "guest":
        return
    key = f"{current_user(request)}:{client_ip(request)}"
    if not _GUEST_LIMITER.check(key):
        raise HTTPException(status_code=429,
                            detail="访客请求过于频繁（每分钟 30 次），稍后再试")


def render(request: Request, name: str, ctx: dict[str, Any] | None = None):
    base = {
        "request": request,
        "user": current_user(request),
        "role": current_role(request),
        "worker": worker_status(),
        "nav_active": "",
    }
    base.update(ctx or {})
    return templates.TemplateResponse(name, base)


def worker_status() -> dict:
    """worker 是否在线。心跳超过 40s 视为掉线。"""
    with session_scope() as s:
        row = s.get(WorkerState, 1)
        if row is None:
            return {"online": False, "status": "unknown", "detail": None,
                    "tg_user": None, "queue_depth": 0, "heartbeat_at": None,
                    "current_action": ""}
        stale = (row.heartbeat_at is None
                 or (datetime.utcnow() - row.heartbeat_at).total_seconds() > 40)
        return {
            "online": not stale,
            "status": row.status,
            "detail": row.detail,
            "tg_user": row.tg_user,
            "queue_depth": row.queue_depth,
            "heartbeat_at": row.heartbeat_at,
            "started_at": row.started_at,
            "current_action": row.current_action or "",
        }


# ---------------- 任务下发 ----------------

def submit_task(kind: str, payload: dict | None = None) -> int:
    with session_scope() as s:
        t = Task(kind=kind, payload=payload or {})
        s.add(t)
        s.flush()
        return t.id


def get_task(task_id: int) -> dict | None:
    with session_scope() as s:
        t = s.get(Task, task_id)
        if t is None:
            return None
        return {"id": t.id, "kind": t.kind, "status": t.status,
                "result": t.result, "error": t.error,
                "created_at": t.created_at, "finished_at": t.finished_at}


def format_task_view(t: dict) -> dict:
    """将 Task 转换为前端统一可视化进度卡片所需的数据格式。"""
    kind = t.get("kind") or ""
    status = t.get("status") or "pending"
    r = t.get("result") or {}
    err = t.get("error")
    is_finished = status in ("done", "failed")
    is_success = status == "done"

    kind_titles = {
        "sync_dialogs": "同步 Telegram 频道",
        "catchup_round": "频道时间窗口对齐",
        "catchup_now": "触发全频道对齐",
        "backfill": "历史消息拉取入库",
        "kb_smart_sync": "知识库智能同步",
        "kb_import": "官方本地化数据导入",
        "kb_import_gachabase": "知识库补充抓取",
        "changelog_poll": "数据版本变更监控",
        "media_cleanup": "媒体与消息清理",
        "retranslate": "重新翻译",
        "repush": "重新推送",
    }
    title = kind_titles.get(kind, f"任务 {kind}")

    percent: int | None = None
    detail = ""

    if kind == "backfill":
        done = r.get("done", 0)
        total = r.get("total", 0)
        ingested = r.get("ingested", 0)
        if total > 0:
            percent = max(0, min(100, int(done / total * 100)))
        if is_finished:
            detail = f"拉取完成：入库 {ingested} 条新消息" if is_success else f"拉取失败: {err}"
        else:
            detail = f"正在拉取与翻译：{done}/{total} 批次 · 已入库 {ingested} 条"

    elif kind == "catchup_round":
        done = r.get("done", 0)
        total = r.get("total", 0)
        curr = r.get("current", "")
        if total > 0:
            percent = max(0, min(100, int(done / total * 100)))
        if is_finished:
            aligned = r.get("aligned", 0)
            skipped = r.get("skipped", 0)
            detail = f"对齐完成：已处理 {done}/{total} 频道（补拉 {aligned} 个，已同步 {skipped} 个）" if is_success else f"对齐失败: {err}"
        else:
            detail = f"正在对齐: {curr} ({done}/{total} 频道)" if curr else f"对齐中：{done}/{total} 频道"

    elif kind == "catchup_now":
        if is_finished:
            percent = 100
            detail = "对齐指令已下发，后台对齐轮正在推进..."
        else:
            percent = None
            detail = "正在触发 worker 对齐循环..."

    elif kind == "sync_dialogs":
        if is_finished:
            percent = 100
            seen = r.get("seen", 0)
            added = r.get("added", 0)
            updated = r.get("updated", 0)
            detail = f"同步完成：发现 {seen} 个会话，新增 {added}，更新 {updated}" if is_success else f"同步失败: {err}"
        else:
            percent = None
            detail = "正在从 Telegram 拉取对话与频道列表..."

    elif kind == "kb_smart_sync":
        done = r.get("done", 0)
        total = r.get("total", 0)
        step_name = r.get("step_name", "")
        if total > 0:
            percent = max(0, min(100, int(done / total * 100)))
        if is_finished:
            added = r.get("added", 0)
            aliases = r.get("aliases", 0)
            detail = f"智能同步完成：新增实体 {added} 个，别名 {aliases} 个" if is_success else f"同步失败: {err}"
        else:
            detail = f"{step_name} ({done}/{total} 阶段)" if step_name else f"正在同步数据 ({done}/{total})"

    elif kind in ("kb_import", "kb_import_gachabase"):
        if is_finished:
            percent = 100
            added = r.get("added", 0)
            aliases = r.get("aliases", 0)
            detail = f"导入完成：新增实体 {added} 个，别名 {aliases} 个" if is_success else f"导入失败: {err}"
        else:
            percent = None
            detail = "正在拉取上游数据并导入知识库..."

    elif kind == "changelog_poll":
        if is_finished:
            percent = 100
            checked = r.get("checked", 0)
            detail = f"检测完成：已检查数据站点版本" if is_success else f"检测失败: {err}"
        else:
            percent = None
            detail = "正在连接 gachabase.net 抓取版本信息..."

    elif kind == "media_cleanup":
        if is_finished:
            percent = 100
            m_res = r.get("messages", {})
            removed = m_res.get("removed", 0)
            freed = m_res.get("freed_mb", 0)
            detail = f"清理完成：删除 {removed} 条超期消息，释放 {freed} MB" if is_success else f"清理失败: {err}"
        else:
            percent = None
            detail = "正在清理超期媒体与文件..."

    else:
        if is_finished:
            percent = 100 if is_success else 0
            detail = "执行完成" if is_success else f"执行失败: {err}"
        else:
            percent = None
            detail = f"任务执行中（{kind}）..."

    if is_finished and is_success and percent is None:
        percent = 100

    return {
        "id": t.get("id"),
        "kind": kind,
        "status": status,
        "is_finished": is_finished,
        "is_success": is_success,
        "title": title,
        "percent": percent,
        "detail": detail,
        "error": err,
        "poll_url": f"/tasks/{t.get('id')}",
        "created_at": t.get("created_at"),
    }


def require_worker() -> None:
    """需要 worker 在线才能做的事，先给出人话解释。"""
    st = worker_status()
    if not st["online"]:
        raise HTTPException(
            status_code=503,
            detail="worker 容器不在线，这个操作需要它执行。"
                   "先看 docker compose logs worker",
        )


def touch_restart() -> None:
    """改需要重启才生效的配置后调用，worker 5 秒内自行退出并被 compose 重启。"""
    settings.set_many({"WORKER_RESTART_TOKEN": secrets.token_hex(8)})


def redirect(url: str, code: int = 303) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=code)
