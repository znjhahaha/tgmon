"""Anthropic 协议。官方端点在这台机器上是硬 403，所以 base_url 通常填中转。"""
from __future__ import annotations

import logging
import time

import anthropic

from .base import AIResult, BaseProvider

logger = logging.getLogger(__name__)


class AnthropicProtocolProvider(BaseProvider):
    async def complete(self, system: str, user: str, **kwargs) -> AIResult:
        t0 = time.monotonic()
        client = anthropic.AsyncAnthropic(
            api_key=self.cfg.api_key or "unused",
            base_url=self.cfg.base_url,
            timeout=float(self.cfg.timeout),
            max_retries=0,
            default_headers=self.cfg.extra_headers or None,
        )
        try:
            # Anthropic 的 system 是顶层参数，不是 messages 里的一条
            resp = await client.messages.create(
                model=self.cfg.model,
                max_tokens=self.cfg.max_tokens,
                temperature=self.cfg.temperature,
                system=system or anthropic.NOT_GIVEN,
                messages=[{"role": "user", "content": user}],
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
        finally:
            try:
                await client.close()
            except Exception:
                pass
