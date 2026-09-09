"""OpenAI 协议。任何兼容 /v1/chat/completions 的中转都走这里。"""
from __future__ import annotations

import asyncio
import logging
import time

from openai import AsyncOpenAI

from .base import AIResult, BaseProvider

logger = logging.getLogger(__name__)

# 客户端复用：按 (provider_id, 事件循环) 缓存，省掉每次调用重建 TCP+TLS 的
# 握手开销（并发压测下每次几百毫秒很可观）。sig 变化（界面上改了配置）才
# 重建；旧客户端异步关闭。admin / worker 各自的事件循环互不共享。
_clients: dict[tuple, tuple[tuple, AsyncOpenAI]] = {}


def _client_for(cfg) -> AsyncOpenAI:
    loop = asyncio.get_running_loop()
    sig = (cfg.base_url, cfg.api_key, float(cfg.timeout or 0),
           tuple(sorted((cfg.extra_headers or {}).items())))
    ent = _clients.get((cfg.id, loop))
    if ent is not None and ent[0] == sig:
        return ent[1]
    if ent is not None:
        loop.create_task(ent[1].aclose())
    client = AsyncOpenAI(api_key=cfg.api_key or "unused", base_url=cfg.base_url,
                         timeout=cfg.timeout, max_retries=0,
                         default_headers=cfg.extra_headers or None)
    _clients[(cfg.id, loop)] = (sig, client)
    return client


class OpenAIProtocolProvider(BaseProvider):
    capabilities = frozenset({"text", "messages", "tools", "embeddings"})
    async def embedding(self, text: str, model: str | None = None) -> list[float] | None:
        client = _client_for(self.cfg)
        try:
            resp = await client.embeddings.create(model=model or self.cfg.model, input=text)
            if resp.data:
                return list(resp.data[0].embedding)
            return None
        except Exception as e:
            logger.warning("embedding %s 失败: %s", self.cfg.name, e)
            return None

    async def complete(self, system: str, user: str, **kwargs) -> AIResult:
        t0 = time.monotonic()
        client = _client_for(self.cfg)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.extend(kwargs.get("messages") or [])
        if user:
            messages.append({"role": "user", "content": user})
        try:
            extra = {}
            if kwargs.get("tools"):
                extra["tools"] = kwargs["tools"]
            # 不用流式：需要 usage 做成本统计，流式拿不到
            resp = await client.chat.completions.create(
                model=self.cfg.model,
                messages=messages,
                max_tokens=kwargs.get("max_tokens") or self.cfg.max_tokens,
                temperature=(self.cfg.temperature if kwargs.get("temperature") is None
                             else kwargs["temperature"]),
                **extra,
            )
            text = ""
            if resp.choices:
                text = (resp.choices[0].message.content or "").strip()
            tool_calls = [item.model_dump() for item in
                          (getattr(resp.choices[0].message, "tool_calls", None) or [])] if resp.choices else []
            usage = getattr(resp, "usage", None)
            return AIResult(
                text=text,
                tokens_in=getattr(usage, "prompt_tokens", 0) or 0,
                tokens_out=getattr(usage, "completion_tokens", 0) or 0,
                provider_name=self.cfg.name,
                model=self.cfg.model,
                error=None if text or tool_calls else "返回内容为空",
                elapsed=time.monotonic() - t0,
                tool_calls=tool_calls,
            )
        except Exception as e:
            return AIResult(
                provider_name=self.cfg.name, model=self.cfg.model,
                error=f"{type(e).__name__}: {e}",
                elapsed=time.monotonic() - t0,
            )
