"""④ Provider 管理 —— 增删改 AI 后端 + 连通性测试。

base_url 无默认值、必填。这台机器上 OpenAI/Anthropic 官方端点是硬 403，
内置默认地址只会制造「配了但打不通」的困惑。
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from ...crypto import encrypt, mask
from ...db import session_scope
from ...models import AIProvider
from ...providers import PROTOCOLS
from ..deps import (
    get_task, redirect, render, require_admin, require_worker, submit_task,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/providers")


def _list() -> list[dict]:
    with session_scope() as s:
        rows = (s.query(AIProvider)
                .order_by(AIProvider.priority.asc(), AIProvider.id.asc()).all())
        out = []
        for r in rows:
            total = r.ok_count + r.fail_count
            out.append({
                "id": r.id, "name": r.name, "protocol": r.protocol,
                "base_url": r.base_url, "key_mask": mask(r.api_key),
                "model": r.model, "priority": r.priority,
                "enabled": r.enabled, "max_tokens": r.max_tokens,
                "temperature": r.temperature, "timeout": r.timeout,
                "concurrency": r.concurrency, "rpm_limit": r.rpm_limit,
                "price_in": r.price_in, "price_out": r.price_out,
                "extra_headers": json.dumps(r.extra_headers or {},
                                            ensure_ascii=False),
                "ok_count": r.ok_count, "fail_count": r.fail_count,
                "fail_rate": (r.fail_count / total * 100) if total else 0.0,
                "last_error": r.last_error, "last_ok_at": r.last_ok_at,
            })
        return out


@router.get("")
async def page(request: Request, user: str = Depends(require_admin)):
    return render(request, "providers.html", {
        "nav_active": "providers", "providers": _list(),
        "protocols": list(PROTOCOLS.keys()),
    })


def _parse_headers(raw: str) -> tuple[dict | None, str | None]:
    raw = (raw or "").strip()
    if not raw:
        return None, None
    try:
        val = json.loads(raw)
        if not isinstance(val, dict):
            return None, "extra_headers 必须是 JSON 对象"
        return {str(k): str(v) for k, v in val.items()}, None
    except json.JSONDecodeError as e:
        return None, f"extra_headers 不是合法 JSON: {e}"


@router.post("/create")
async def create(request: Request,
                 name: str = Form(...),
                 protocol: str = Form(...),
                 base_url: str = Form(...),
                 api_key: str = Form(""),
                 model: str = Form(...),
                 priority: int = Form(100),
                 max_tokens: int = Form(4096),
                 temperature: float = Form(0.3),
                 timeout: int = Form(90),
                 concurrency: int = Form(2),
                 rpm_limit: int = Form(0),
                 price_in: float = Form(0.0),
                 price_out: float = Form(0.0),
                 extra_headers: str = Form(""),
                 user: str = Depends(require_admin)):
    if protocol not in PROTOCOLS:
        return HTMLResponse(
            "<div class='flash err'>协议只能是 openai 或 anthropic</div>")
    base_url = base_url.strip()
    if not base_url.startswith(("http://", "https://")):
        return HTMLResponse(
            "<div class='flash err'>base_url 必须以 http:// 或 https:// 开头</div>")
    headers, err = _parse_headers(extra_headers)
    if err:
        return HTMLResponse(f"<div class='flash err'>{err}</div>")

    with session_scope() as s:
        if s.query(AIProvider).filter(AIProvider.name == name.strip()).first():
            return HTMLResponse("<div class='flash err'>这个名字已存在</div>")
        s.add(AIProvider(
            name=name.strip(), protocol=protocol, base_url=base_url,
            api_key=encrypt(api_key.strip()) if api_key.strip() else None,
            model=model.strip(), priority=priority, max_tokens=max_tokens,
            temperature=temperature, timeout=timeout,
            concurrency=max(1, concurrency), rpm_limit=max(0, rpm_limit),
            price_in=price_in, price_out=price_out, extra_headers=headers,
        ))
    return redirect("/providers")


@router.post("/{pid}/update")
async def update(pid: int, request: Request,
                 name: str = Form(...),
                 protocol: str = Form(...),
                 base_url: str = Form(...),
                 api_key: str = Form(""),
                 model: str = Form(...),
                 priority: int = Form(100),
                 max_tokens: int = Form(4096),
                 temperature: float = Form(0.3),
                 timeout: int = Form(90),
                 concurrency: int = Form(2),
                 rpm_limit: int = Form(0),
                 price_in: float = Form(0.0),
                 price_out: float = Form(0.0),
                 extra_headers: str = Form(""),
                 enabled: str = Form(""),
                 user: str = Depends(require_admin)):
    if protocol not in PROTOCOLS:
        return HTMLResponse("<div class='flash err'>协议不合法</div>")
    headers, err = _parse_headers(extra_headers)
    if err:
        return HTMLResponse(f"<div class='flash err'>{err}</div>")

    with session_scope() as s:
        row = s.get(AIProvider, pid)
        if row is None:
            return HTMLResponse("<div class='flash err'>provider 不存在</div>")
        row.name = name.strip()
        row.protocol = protocol
        row.base_url = base_url.strip()
        # 留空 = 不改密钥（只写不读）
        if api_key.strip():
            row.api_key = encrypt(api_key.strip())
        row.model = model.strip()
        row.priority = priority
        row.max_tokens = max_tokens
        row.temperature = temperature
        row.timeout = timeout
        row.concurrency = max(1, concurrency)
        row.rpm_limit = max(0, rpm_limit)
        row.price_in = price_in
        row.price_out = price_out
        row.extra_headers = headers
        row.enabled = enabled.strip().lower() in ("on", "true", "1")
    return HTMLResponse("<div class='flash ok'>已保存，立即生效</div>")


@router.post("/{pid}/delete")
async def delete(pid: int, request: Request, user: str = Depends(require_admin)):
    with session_scope() as s:
        row = s.get(AIProvider, pid)
        if row is not None:
            s.delete(row)
    return redirect("/providers")


@router.post("/{pid}/test")
async def test(pid: int, request: Request, user: str = Depends(require_admin)):
    """连通性测试。真正的调用在 worker 里发，这里只等结果。"""
    require_worker()
    tid = submit_task("test_provider", {"provider_id": pid})
    waited = 0.0
    while waited < 120.0:
        t = get_task(tid)
        if t and t["status"] in ("done", "failed"):
            if t["status"] == "failed":
                return HTMLResponse(
                    f"<div class='flash err'>任务出错：{t['error']}</div>")
            r = t["result"] or {}
            if r.get("ok"):
                text = (r.get("text") or "").strip()
                text = text[:200].replace("<", "&lt;")
                return HTMLResponse(
                    f"<div class='flash ok'>通了 · {r.get('elapsed_ms')} ms · "
                    f"tokens {r.get('tokens_in')}/{r.get('tokens_out')}"
                    f"<br><code>{text}</code></div>")
            err = (r.get("error") or "未知错误").replace("<", "&lt;")
            return HTMLResponse(
                f"<div class='flash err'>不通：{err}</div>"
                "<div class='hint'>官方 OpenAI/Anthropic 端点在这台机器上是硬 403，"
                "必须填中转或 Gemini 兼容地址。</div>")
        await asyncio.sleep(1.0)
        waited += 1.0
    return HTMLResponse("<div class='flash err'>测试超时</div>")
