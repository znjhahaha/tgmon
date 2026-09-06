"""OpenAI 协议。任何兼容 /v1/chat/completions 的中转都走这里。"""
from __future__ import annotations

import logging
import time

from openai import AsyncOpenAI

from .base import AIResult, BaseProvider

logger = logging.getLogger(__name__)


class OpenAIProtocolProvider(BaseProvider):
    async def embedding(self, text: str, model: str | None = None) -> list[float] | None:
        client = AsyncOpenAI(api_key=self.cfg.api_key or "unused", base_url=self.cfg.base_url,
                             timeout=self.cfg.timeout, max_retries=0,
                             default_headers=self.cfg.extra_headers or None)
        try:
            resp = await client.embeddings.create(model=model or self.cfg.model, input=text)
            if resp.data:
                return list(resp.data[0].embedding)
            return None
        except Exception as e:
            logger.warning("embedding %s 失败: %s", self.cfg.name, e)
            return None
        finally:
            await client.close()

    async def complete(self, system: str, user: str, **kwargs) -> AIResult:
        t0 = time.monotonic()
        client = AsyncOpenAI(
            api_key=self.cfg.api_key or "unused",
            base_url=self.cfg.base_url,
            timeout=self.cfg.timeout,
            max_retries=0,          # 重试交给 registry 的故障转移链
            default_headers=self.cfg.extra_headers or None,
        )
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        try:
            # 不用流式：需要 usage 做成本统计，流式拿不到
            resp = await client.chat.completions.create(
                model=self.cfg.model,
                messages=messages,
                max_tokens=self.cfg.max_tokens,
                temperature=self.cfg.temperature,
            )
            text = ""
            if resp.choices:
                text = (resp.choices[0].message.content or "").strip()
            usage = getattr(resp, "usage", None)
            return AIResult(
                text=text,
                tokens_in=getattr(usage, "prompt_tokens", 0) or 0,
                tokens_out=getattr(usage, "completion_tokens", 0) or 0,
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
