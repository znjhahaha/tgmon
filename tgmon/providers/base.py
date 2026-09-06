"""Provider 抽象。只有两种协议实现，配置从 DB 注入而非环境变量。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProviderConfig:
    """从 ai_provider 表读出来的一行，api_key 已解密。"""
    id: int
    name: str
    protocol: str
    base_url: str
    api_key: str
    model: str
    extra_headers: dict[str, str] = field(default_factory=dict)
    max_tokens: int = 4096
    temperature: float = 0.3
    timeout: int = 90
    concurrency: int = 2
    rpm_limit: int = 0
    price_in: float = 0.0
    price_out: float = 0.0


@dataclass
class AIResult:
    text: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    provider_name: str = ""
    model: str = ""
    error: str | None = None
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.text.strip())


class BaseProvider(ABC):
    """协议实现只需要一个方法。配置在构造时注入。"""

    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg

    @abstractmethod
    async def complete(self, system: str, user: str, **kwargs: Any) -> AIResult:
        """发一次纯文本请求。本方案不传图 —— 媒体只搬运不过 AI。"""
        raise NotImplementedError

    async def embedding(self, text: str, model: str | None = None) -> list[float] | None:
        """Optional OpenAI-compatible embedding endpoint."""
        return None

    def cost_of(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in * self.cfg.price_in + tokens_out * self.cfg.price_out) / 1_000_000
