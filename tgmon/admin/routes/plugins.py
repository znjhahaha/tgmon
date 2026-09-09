"""Admin API for inspecting and controlling local process-isolated plugins."""
from __future__ import annotations

from pathlib import Path
import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ... import settings
from ...extension_runtime import manager as _get_manager
from ...paths import EXTENSIONS_DIR
from ...themes import load_themes, load_theme_file
from ..deps import require_admin, render, redirect

router = APIRouter(prefix="/plugins")
@router.get("")
async def list_plugins(request: Request, _user: str = Depends(require_admin)):
    manager = _get_manager()
    await asyncio.to_thread(manager.sync)
    plugins = [manager.status(name) for name in sorted(manager._manifests)]
    configuration = manager.configuration()
    for plugin in plugins:
        worker = (settings.get("PLUGIN_WORKER_STATUS") or {}).get(plugin["name"], {})
        if worker.get("error"):
            plugin["error"] = f"worker: {worker['error']}"
        plugin["config"] = json.dumps(configuration.get(plugin["name"], {}).get("config", {}),
                                       ensure_ascii=False, indent=2)
    from ...db import session_scope
    from ...models import ProcessingJob
    from sqlalchemy import func
    with session_scope() as s:
        queues = [dict(queue=r.queue, status=r.status, count=r.n) for r in s.query(
            ProcessingJob.queue, ProcessingJob.status, func.count().label("n"))
            .group_by(ProcessingJob.queue, ProcessingJob.status)]
        failed = [{"id": r.id, "queue": r.queue, "error": r.error}
                  for r in s.query(ProcessingJob).filter(
                      ProcessingJob.status.in_(("failed", "review", "retry"))).order_by(
                      ProcessingJob.id.desc()).limit(20)]
    return render(request, "plugins.html", {"nav_active": "plugins", "plugins": plugins,
        "themes": load_themes(EXTENSIONS_DIR / "themes"), "queues": queues, "failed": failed,
        "default_theme": settings.get("THEME_DEFAULT"),
        "mcp_enabled": settings.get("MCP_ENABLED"),
        "mcp_servers": json.dumps(settings.get("MCP_SERVERS") or [], ensure_ascii=False, indent=2),
        "mcp_tools": json.dumps(settings.get("MCP_BOT_TOOLS") or {}, ensure_ascii=False, indent=2)})


@router.post("/install")
async def install_plugin(request: Request, _user: str = Depends(require_admin)):
    try:
        is_json = "application/json" in request.headers.get("content-type", "")
        body = await request.json() if is_json else await request.form()
        source = str(body.get("source") or "").strip()
        if not source:
            raise ValueError("source is required")
        manifest = await asyncio.to_thread(_get_manager().install, source)
        return {"ok": True, "name": manifest.name, "version": manifest.version} if is_json else redirect("/plugins")
    except (OSError, ValueError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/{name}/start")
async def start_plugin(name: str, _user: str = Depends(require_admin)):
    try:
        await asyncio.to_thread(_get_manager().configure, name, enabled=True)
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return redirect("/plugins")


@router.post("/themes/default")
async def default_theme(request: Request, _user: str = Depends(require_admin)):
    form = await request.form()
    key = str(form.get("theme") or "")
    if key not in {t.key for t in load_themes(EXTENSIONS_DIR / "themes")}:
        raise HTTPException(400, "主题不存在")
    settings.set_many({"THEME_DEFAULT": key})
    return redirect("/plugins")


@router.post("/{name}/stop")
async def stop_plugin(name: str, _user: str = Depends(require_admin)):
    await asyncio.to_thread(_get_manager().configure, name, enabled=False)
    return redirect("/plugins")


@router.post("/{name}/config")
async def plugin_config(name: str, request: Request, _user: str = Depends(require_admin)):
    form = await request.form()
    try:
        config = json.loads(str(form.get("config") or "{}"))
        if not isinstance(config, dict):
            raise ValueError("configuration must be an object")
        await asyncio.to_thread(_get_manager().configure, name, enabled=False, config=config)
    except (ValueError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return redirect("/plugins")


@router.post("/themes/install")
async def install_theme(request: Request, _user: str = Depends(require_admin)):
    form = await request.form()
    import tempfile
    root = EXTENSIONS_DIR / "themes"
    root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".tmp",
                                          dir=root, delete=False) as stream:
            temp = Path(stream.name)
            stream.write(str(form.get("package") or ""))
        theme = load_theme_file(temp)
        temp.replace(root / f"{theme.key}.json")
    except (ValueError, OSError) as exc:
        if "temp" in locals():
            temp.unlink(missing_ok=True)
        raise HTTPException(400, str(exc)) from exc
    return redirect("/plugins")


@router.post("/mcp/config")
async def mcp_config(request: Request, _user: str = Depends(require_admin)):
    form = await request.form()
    try:
        servers, allowed = json.loads(str(form.get("servers") or "[]")), json.loads(str(form.get("tools") or "{}"))
        if not isinstance(servers, list) or not isinstance(allowed, dict):
            raise ValueError("Invalid MCP configuration")
        for server in servers:
            if not isinstance(server, dict) or not server.get("name"):
                raise ValueError("Every MCP server needs a name")
        settings.set_many({"MCP_ENABLED": form.get("enabled") == "on",
                           "MCP_SERVERS": servers, "MCP_BOT_TOOLS": allowed})
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return redirect("/plugins")
