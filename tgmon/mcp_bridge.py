"""Optional MCP tool discovery/calls using the official Python SDK.

The dependency is imported lazily so the core monitor remains usable without
external tools. Configuration is explicit and disabled by default.
"""
from __future__ import annotations

from typing import Any
import asyncio
import json
import hashlib
import time
from contextlib import asynccontextmanager

from . import settings
from .conversation_scope import current


class McpUnavailable(RuntimeError):
    pass


@asynccontextmanager
async def session_for(config: dict):
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError as exc:
        raise McpUnavailable("MCP SDK is not installed") from exc
    async with asyncio.timeout(float(config.get("timeout") or 8)):
        if config.get("transport", "stdio") == "stdio":
            parameters = StdioServerParameters(command=config["command"], args=config.get("args") or [],
                                               env=config.get("env"))
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        elif config["transport"] == "streamable_http":
            async with streamablehttp_client(config["url"], headers=config.get("headers")) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        else:
            raise ValueError("Unsupported MCP transport")


_discovery: dict[str, tuple[float, list[dict]]] = {}


async def discover(config: dict) -> list[dict]:
    key = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    hit = _discovery.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    async with session_for(config) as session:
        result = await session.list_tools()
        tools = [{"name": item.name, "description": item.description or "",
                  "input_schema": item.inputSchema} for item in result.tools]
    _discovery[key] = (time.monotonic() + 30, tools)
    return tools


def allowed_tools() -> set[str]:
    scope = current()
    if not scope or not settings.get("MCP_ENABLED"):
        return set()
    return set((settings.get("MCP_BOT_TOOLS") or {}).get(scope.bot, []))


async def available_tools() -> list[dict]:
    allowed = allowed_tools()
    result = []
    for server in settings.get("MCP_SERVERS") or []:
        name = str(server.get("name") or "")
        if not server.get("enabled") or not any(t.startswith(f"mcp:{name}:") for t in allowed):
            continue
        try:
            for tool in await discover(server):
                full_name = f"mcp:{name}:{tool['name']}"
                if full_name in allowed:
                    result.append({**tool, "name": full_name})
        except Exception:
            from .util import log_event
            log_event("warning", "mcp", f"MCP 工具发现失败: {name}")
    return result


async def call(name: str, arguments: dict):
    if name not in allowed_tools():
        raise PermissionError("MCP tool is disabled for this bot")
    _, server_name, tool_name = name.split(":", 2)
    config = next((s for s in settings.get("MCP_SERVERS") or []
                   if s.get("name") == server_name and s.get("enabled")), None)
    if config is None:
        raise PermissionError("MCP server is disabled")
    schema = next((t["input_schema"] for t in await discover(config) if t["name"] == tool_name), None)
    if schema is None:
        raise ValueError("MCP tool no longer exists")
    from jsonschema import validate
    validate(arguments, schema)
    async with session_for(config) as session:
        result = await session.call_tool(tool_name, arguments=arguments)
        return result.model_dump(mode="json")


async def discover_stdio(command: str, args: list[str] | None = None,
                         env: dict[str, str] | None = None) -> list[dict[str, Any]]:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:
        raise McpUnavailable("MCP SDK is not installed") from exc
    params = StdioServerParameters(command=command, args=args or [], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return [{"name": tool.name, "description": tool.description or "",
                     "input_schema": tool.inputSchema}
                    for tool in result.tools]


async def call_stdio(command: str, tool_name: str, arguments: dict[str, Any],
                     args: list[str] | None = None,
                     env: dict[str, str] | None = None) -> Any:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:
        raise McpUnavailable("MCP SDK is not installed") from exc
    params = StdioServerParameters(command=command, args=args or [], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(tool_name, arguments=arguments)
