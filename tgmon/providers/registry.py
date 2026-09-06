"""Provider 注册表 + 故障转移链。

按 priority 升序尝试，任一后端失败（429/5xx/超时/空返回）就降级到下一个。
全部失败时返回带 error 的 AIResult —— 调用方负责把原文入库并标记失败，不丢消息。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime

from ..crypto import decrypt
from ..db import session_scope
from ..models import AIProvider
from .anthropic_protocol import AnthropicProtocolProvider
from .base import AIResult, BaseProvider, ProviderConfig
from .openai_protocol import OpenAIProtocolProvider

logger = logging.getLogger(__name__)

PROTOCOLS: dict[str, type[BaseProvider]] = {
    "openai": OpenAIProtocolProvider,
    "anthropic": AnthropicProtocolProvider,
}

# provider_id -> (并发上限, Semaphore, 最近调用时间戳队列)。按事件循环隔离。
# 记住上限本身而不是看 Semaphore._value —— 后者是「剩余可用数」，会随占用变化，
# 拿它判断配置有没有变会在高并发时误重建。
_limiters: dict[int, tuple[int, asyncio.Semaphore, deque]] = {}
_limiter_loop: asyncio.AbstractEventLoop | None = None


def _row_to_cfg(row: AIProvider) -> ProviderConfig:
    return ProviderConfig(
        id=row.id,
        name=row.name,
        protocol=row.protocol,
        base_url=(row.base_url or "").rstrip("/"),
        api_key=decrypt(row.api_key) or "",
        model=row.model,
        extra_headers=row.extra_headers or {},
        max_tokens=row.max_tokens,
        temperature=row.temperature,
        timeout=row.timeout,
        concurrency=max(1, row.concurrency),
        rpm_limit=max(0, row.rpm_limit),
        price_in=row.price_in,
        price_out=row.price_out,
    )


def load_configs(only_enabled: bool = True) -> list[ProviderConfig]:
    with session_scope() as s:
        q = s.query(AIProvider)
        if only_enabled:
            q = q.filter(AIProvider.enabled.is_(True))
        rows = q.order_by(AIProvider.priority.asc(), AIProvider.id.asc()).all()
        return [_row_to_cfg(r) for r in rows]


def load_config(provider_id: int) -> ProviderConfig | None:
    with session_scope() as s:
        row = s.get(AIProvider, provider_id)
        return _row_to_cfg(row) if row else None


def build(cfg: ProviderConfig) -> BaseProvider:
    impl = PROTOCOLS.get(cfg.protocol)
    if impl is None:
        raise ValueError(f"未知协议 {cfg.protocol}，只支持 openai / anthropic")
    return impl(cfg)


def _limiter(cfg: ProviderConfig) -> tuple[asyncio.Semaphore, deque]:
    global _limiter_loop, _limiters
    loop = asyncio.get_running_loop()
    if _limiter_loop is not loop:
        _limiters = {}
        _limiter_loop = loop
    ent = _limiters.get(cfg.id)
    if ent is None or ent[0] != cfg.concurrency:
        # 并发上限在界面上改了才重建。旧 Semaphore 上正在等待的调用会用旧上限
        # 跑完，这是可接受的 —— 下一批就走新值
        ent = (cfg.concurrency, asyncio.Semaphore(cfg.concurrency), deque())
        _limiters[cfg.id] = ent
    return ent[1], ent[2]


async def _acquire_rpm(calls: deque, rpm: int) -> None:
    """滑动窗口限流。rpm=0 表示不限。"""
    if rpm <= 0:
        return
    while True:
        now = time.monotonic()
        while calls and now - calls[0] > 60:
            calls.popleft()
        if len(calls) < rpm:
            calls.append(now)
            return
        await asyncio.sleep(max(0.05, 60 - (now - calls[0])))


async def call_one(cfg: ProviderConfig, system: str, user: str) -> AIResult:
    """单个 provider 调用，带并发上限与限流。"""
    sem, calls = _limiter(cfg)
    async with sem:
        await _acquire_rpm(calls, cfg.rpm_limit)
        provider = build(cfg)
        return await provider.complete(system, user)


def record_stats(provider_id: int, ok: bool, error: str | None = None) -> None:
    try:
        with session_scope() as s:
            row = s.get(AIProvider, provider_id)
            if row is None:
                return
            if ok:
                row.ok_count += 1
                row.last_ok_at = datetime.utcnow()
                row.last_error = None
            else:
                row.fail_count += 1
                row.last_error = (error or "")[:2000]
    except Exception as e:
        logger.warning("写 provider 统计失败: %s", e)


async def complete_with_failover(system: str, user: str) -> AIResult:
    """按优先级走完整条链。返回最后一个结果（成功即提前返回）。"""
    configs = load_configs()
    if not configs:
        return AIResult(error="没有启用的翻译后端。去后台 Provider 页加一个")

    last = AIResult(error="未知错误")
    for cfg in configs:
        try:
            res = await call_one(cfg, system, user)
        except Exception as e:  # 构造阶段就炸（协议名错等）
            res = AIResult(provider_name=cfg.name, model=cfg.model,
                           error=f"{type(e).__name__}: {e}")
        record_stats(cfg.id, res.ok, res.error)
        if res.ok:
            return res
        logger.warning("provider %s 失败，降级到下一个: %s", cfg.name, res.error)
        last = res
    return last


async def embedding_with_failover(text: str, provider_name: str | None = None,
                                  model: str | None = None) -> list[float] | None:
    """Best-effort embedding using enabled OpenAI-compatible providers."""
    for cfg in load_configs():
        if cfg.protocol != "openai" or (provider_name and cfg.name != provider_name):
            continue
        try:
            vector = await build(cfg).embedding(text, model=model)
            if vector:
                return vector
        except Exception as e:
            logger.warning("embedding provider %s 失败: %s", cfg.name, e)
    return None


async def test_provider(provider_id: int) -> dict:
    """连通性测试：发一句固定文本，回返回内容与耗时。"""
    cfg = load_config(provider_id)
    if cfg is None:
        return {"ok": False, "error": "provider 不存在"}
    if not cfg.base_url:
        return {"ok": False, "error": "base_url 为空。这台机器上官方端点是硬 403，必须填中转或 Gemini"}
    res = await call_one(cfg, "你是翻译助手，只输出译文。",
                         "Translate to Simplified Chinese: The boss spawns at wave 3.")
    record_stats(cfg.id, res.ok, res.error)
    return {
        "ok": res.ok,
        "text": res.text,
        "error": res.error,
        "elapsed_ms": int(res.elapsed * 1000),
        "tokens_in": res.tokens_in,
        "tokens_out": res.tokens_out,
        "model": res.model,
    }
