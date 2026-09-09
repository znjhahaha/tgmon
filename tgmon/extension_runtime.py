"""Opt-in plugin and MCP hooks used by the existing business paths."""
from __future__ import annotations

import asyncio
import logging

from . import settings
from .paths import EXTENSIONS_DIR
from .plugins import PluginManager

_manager = None
logger = logging.getLogger(__name__)


def manager() -> PluginManager:
    global _manager
    root = str(settings.get("PLUGIN_ROOT") or EXTENSIONS_DIR / "plugins")
    if _manager is None or str(_manager.root) != root:
        if _manager:
            _manager.close()
        _manager = PluginManager(root)
    return _manager


async def emit(event: dict) -> None:
    if not settings.get("PLUGIN_ENABLED"):
        return
    try:
        host = manager()
        await asyncio.to_thread(host.sync)
        host.emit(event)
    except Exception:
        logger.warning("Plugin event hook failed", exc_info=True)


async def command(content: str, context: dict):
    if not settings.get("PLUGIN_ENABLED") or not content.startswith("/"):
        return None
    host = manager()
    await asyncio.to_thread(host.sync)
    command = content.split()[0].lower()
    for manifest in host._manifests.values():
        if command in manifest.commands and host.is_running(manifest.name):
            return await asyncio.to_thread(host.request, manifest.name, "command",
                                           {"content": content, "context": context})
    return None


async def plugin_tools() -> list[dict]:
    if not settings.get("PLUGIN_ENABLED"):
        return []
    host = manager()
    await asyncio.to_thread(host.sync)
    return [{**tool, "name": f"plugin:{manifest.name}:{tool['name']}"}
            for manifest in host._manifests.values() if host.is_running(manifest.name)
            for tool in manifest.tools]


async def call_plugin(name: str, arguments: dict, context: dict):
    _, plugin, tool = name.split(":", 2)
    advertised = {entry["name"]: entry for entry in await plugin_tools()}
    if name not in advertised:
        raise PermissionError("Plugin tool is disabled")
    from jsonschema import validate
    validate(arguments, advertised[name].get("inputSchema") or {"type": "object"})
    return await asyncio.to_thread(manager().request, plugin, "tool",
        {"name": tool, "arguments": arguments, "context": context})


async def poll_sources() -> None:
    from .source_adapters import channel_for, checkpoint, save_page
    previous_status = None
    while True:
        try:
            if settings.get("PLUGIN_ENABLED"):
                host = manager()
                await asyncio.to_thread(host.sync)
                configuration = await asyncio.to_thread(host.configuration)
                for manifest in list(host._manifests.values()):
                    if "source" not in manifest.capabilities or not host.is_running(manifest.name):
                        continue
                    config = configuration.get(manifest.name, {}).get("config", {})
                    for source in config.get("sources", []):
                        if not isinstance(source, dict) or not source.get("id") or not source.get("enabled"):
                            continue
                        try:
                            cid = await asyncio.to_thread(channel_for, manifest.name, source)
                            cursor = await asyncio.to_thread(checkpoint, cid)
                            page = await asyncio.to_thread(host.request, manifest.name, "poll",
                                {"source": source["id"], "cursor": cursor, "limit": 100}, 5)
                            await asyncio.to_thread(save_page, cid, page)
                        except Exception as exc:
                            host._errors[manifest.name] = str(exc)[:500]
                            logger.warning("Plugin source failed: %s", manifest.name)
                status = {name: host.status(name) for name in host._manifests}
                if status != previous_status:
                    await asyncio.to_thread(settings.set_many, {"PLUGIN_WORKER_STATUS": status})
                    previous_status = status
            elif _manager:
                await asyncio.to_thread(_manager.close)
        except Exception:
            logger.exception("Plugin source loop failed")
        await asyncio.sleep(10)


async def close() -> None:
    if _manager:
        await asyncio.to_thread(_manager.close)
