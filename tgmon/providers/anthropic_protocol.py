"""Anthropic 协议。官方端点在这台机器上是硬 403，所以 base_url 通常填中转。"""
from __future__ import annotations

import asyncio
import logging
import time

import anthropic

from .base import AIResult, BaseProvider

logger = logging.getLogger(__name__)

# 客户端复用（与 openai_protocol 同一套思路）：按 (provider_id, 事件循环)
# 缓存，配置变更才重建，省每次调用的握手开销。
_clients: dict[tuple, tuple[tuple, anthropic.AsyncAnthropic]] = {}


def _client_for(cfg) -> anthropic.AsyncAnthropic:
    loop = asyncio.get_running_loop()
    sig = (cfg.base_url, cfg.api_key, float(cfg.timeout or 0),
           tuple(sorted((cfg.extra_headers or {}).items())))
    ent = _clients.get((cfg.id, loop))
    if ent is not None and ent[0] == sig:
        return ent[1]
    if ent is not None:
        loop.create_task(ent[1].close())
    client = anthropic.AsyncAnthropic(
        api_key=cfg.api_key or "unused",
        base_url=cfg.base_url,
        timeout=float(cfg.timeout),
        max_retries=0,
        default_headers=cfg.extra_headers or None,
    )
    _clients[(cfg.id, loop)] = (sig, client)
    return client


class AnthropicProtocolProvider(BaseProvider):
    async def complete(self, system: str, user: str, **kwargs) -> AIResult:
        t0 = time.monotonic()
        client = _client_for(self.cfg)
        try:
            messages = [dict(item) for item in (kwargs.get("messages") or [])
                        if item.get("role") in ("user", "assistant")]
            if user:
                messages.append({"role": "user", "content": user})
            # Anthropic 的 system 是顶层参数，不是 messages 里的一条
            resp = await client.messages.create(
                model=self.cfg.model,
                max_tokens=kwargs.get("max_tokens") or self.cfg.max_tokens,
                temperature=(self.cfg.temperature if kwargs.get("temperature") is None
                             else kwargs["temperature"]),
                system=system or anthropic.NOT_GIVEN,
                messages=messages,
            )
            parts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
            text = "".join(parts).strip()
            usage = getattr(resp, "usage", None)
            return AIResult(
                text=text,
                tokens_in=getattr(usage, "input_tokens", 0) or 0,
                tokens_out=getattr(usage, "output_tokens", 0) or 0,
                provider_name=self.cfg.name,
                model=self.cfg.model,
                error=None if text else "返回内容为空",
                elapsed=time.monotonic() - t0,
            )
        except Exception as e:
            return AIResult(
                provider_name=self.cfg.name, model=self.cfg.model,
                error=f"{type(e).__name__}: {e}",
                elapsed=time.monotonic() - t0,
            )
