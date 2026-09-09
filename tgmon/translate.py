"""翻译编排：缓存 → 预算闸门 → 术语注入 → 故障转移链 → 译后校验。

只翻译文本。媒体一律不过 AI（不传 images），成本和延迟都低一个量级，也避免
视觉模型把图里的东西胡乱描述。
"""
from __future__ import annotations

import hashlib
import logging
import asyncio
import copy
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from . import glossary, settings
from .db import session_scope
from .models import AiUsage, TranslationCache
from .providers import complete_with_failover, load_configs
from .util import local_day

logger = logging.getLogger(__name__)


@dataclass
class TranslateResult:
    text_zh: str = ""
    status: str = "pending"      # ok / failed / skipped / disabled / budget
    error: str | None = None
    provider_name: str | None = None
    model: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    from_cache: bool = False
    glossary_hits: list[str] = field(default_factory=list)
    glossary_miss: list[str] = field(default_factory=list)
    glossary_corrected: list[str] = field(default_factory=list)


def cache_key(text: str, prompt: str, model: str, game: str | None = None,
              strategy: str | None = None, kb_version: str | None = None) -> str:
    h = hashlib.sha256()
    h.update(text.encode("utf-8"))
    h.update(b"\x00")
    h.update(prompt.encode("utf-8"))
    h.update(b"\x00")
    h.update(model.encode("utf-8"))
    for value in (game or "", strategy or "", kb_version or ""):
        h.update(b"\x00")
        h.update(str(value).encode("utf-8"))
    return h.hexdigest()


def _cache_get(key: str) -> tuple[str, str | None, str | None] | None:
    """返回 (译文, provider, model)。返回普通元组而不是 ORM 对象 ——
    构造一个同主键的临时 ORM 实例很容易在后续 session 里引发身份冲突。"""
    with session_scope() as s:
        row = s.get(TranslationCache, key)
        if row is None:
            return None
        row.hit_count += 1
        row.last_hit_at = datetime.utcnow()
        return row.text_zh, row.provider_name, row.model


def _cache_put(key: str, text_zh: str, provider: str | None, model: str | None) -> None:
    """写入或覆盖。必须覆盖：重译产生的新译文要能被后续相同内容命中，
    否则缓存里的旧译文会一直赢。"""
    try:
        with session_scope() as s:
            row = s.get(TranslationCache, key)
            if row is None:
                s.add(TranslationCache(key=key, text_zh=text_zh,
                                       provider_name=provider, model=model))
            else:
                row.text_zh = text_zh
                row.provider_name = provider
                row.model = model
    except Exception as e:
        logger.debug("写翻译缓存失败: %s", e)


def budget_blocked() -> str | None:
    """返回 None 表示可以继续；否则返回拦截原因。"""
    call_limit = int(settings.get("AI_DAILY_CALL_LIMIT") or 0)
    cost_limit = float(settings.get("AI_DAILY_COST_LIMIT") or 0)
    if call_limit <= 0 and cost_limit <= 0:
        return None
    day = local_day()
    with session_scope() as s:
        rows = s.query(AiUsage).filter(AiUsage.day == day).all()
        calls = sum(r.calls for r in rows)
        cost = sum(r.cost for r in rows)
    if call_limit > 0 and calls >= call_limit:
        return f"今日调用已达上限 {call_limit}"
    if cost_limit > 0 and cost >= cost_limit:
        return f"今日花费已达上限 {cost_limit}"
    return None


def record_usage(provider_name: str, tokens_in: int, tokens_out: int, cost: float) -> None:
    """按天累计用量。预算闸门读的就是这张表，所以这里失败必须吼出来。"""
    day = local_day()
    try:
        with session_scope() as s:
            row = (s.query(AiUsage)
                   .filter(AiUsage.day == day, AiUsage.provider_name == provider_name)
                   .first())
            if row is None:
                # 必须显式写 0：Column(default=0) 只在 INSERT 时生效，
                # 新建对象的属性此刻是 None，直接 += 会 TypeError
                row = AiUsage(day=day, provider_name=provider_name,
                              calls=0, tokens_in=0, tokens_out=0, cost=0.0)
                s.add(row)
            row.calls += 1
            row.tokens_in += tokens_in
            row.tokens_out += tokens_out
            row.cost += cost
    except Exception as e:
        # 不能用 debug 咽掉：这张表空了预算上限就形同失效，会静默烧钱
        logger.warning("写用量统计失败，预算上限可能失效: %s", e, exc_info=True)


def _cost_for(provider_name: str, tokens_in: int, tokens_out: int) -> float:
    for cfg in load_configs(only_enabled=False):
        if cfg.name == provider_name:
            return (tokens_in * cfg.price_in + tokens_out * cfg.price_out) / 1_000_000
    return 0.0


async def translate_with_kb(text: str, game: str | None = None) -> TranslateResult:
    """Translate standalone bot input with the same reviewed KB as ingestion."""
    from . import classify, prompts
    from .kb.annotate import annotate_from_hits

    # A bot request has no channel to supply a game. Detect across the approved
    # vocabulary first, and retain cross-game terms when the user did not scope it.
    pool = glossary.terms_all()
    hits = glossary.find_hits(text, pool)
    detected = (await classify.resolve_game(text, game_hint=game, hits=hits)).game
    prompt = prompts.resolve_prompt(None, game=detected) + prompts.game_context(detected)
    return await translate(text, prompt, game=detected, hits=hits,
                           entities_data=annotate_from_hits(hits, detected),
                           bilingual_policy="always")


_translation_jobs: dict[tuple[asyncio.AbstractEventLoop, str], asyncio.Task] = {}


async def translate(text: str, prompt: str, game: str | None = None,
                    entities_data: dict | None = None,
                    use_cache: bool | None = None,
                    hits: list | None = None,
                    bilingual_policy: str = "zh_first", theme: str = "gaming") -> TranslateResult:
    """Coalesce concurrent, identical requests and isolate returned results."""
    request = {"text": text, "prompt": prompt, "game": game,
               "entities": entities_data, "use_cache": use_cache,
               "hits": [vars(hit) if hasattr(hit, "__dict__") else str(hit) for hit in hits or []],
               "policy": bilingual_policy, "theme": theme, "kb": settings.get("KB_VERSION")}
    digest = hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True,
                                       default=str).encode()).hexdigest()
    key = (asyncio.get_running_loop(), digest)
    task = _translation_jobs.get(key)
    shared = task is not None
    if task is None:
        task = asyncio.create_task(_translate_impl(text, prompt, game, entities_data,
                                                   use_cache, hits, bilingual_policy, theme))
        _translation_jobs[key] = task
        task.add_done_callback(lambda done: _translation_jobs.pop(key, None)
                               if _translation_jobs.get(key) is done else None)
    result = copy.deepcopy(await asyncio.shield(task))
    if shared and result.status == "ok":
        result.from_cache, result.tokens_in, result.tokens_out, result.cost = True, 0, 0, 0.0
    return result


async def _translate_impl(text: str, prompt: str, game: str | None = None,
                    entities_data: dict | None = None,
                    use_cache: bool | None = None,
                    hits: list | None = None,
                    bilingual_policy: str = "zh_first", theme: str = "gaming") -> TranslateResult:
    """把爆料原文译成中文。text 为空时直接 skipped（纯图消息零调用）。

    entities_data 是 kb.annotate 标注出的实体，注入后模型能拿到属性做消歧义
    （Kafka 是「5★ 虚无 雷」的角色，不是作家卡夫卡）。

    hits 是调用方已经算好的术语命中。管线里传它 —— 那边为了判定游戏本来就要
    做一次跨游戏全文匹配，不传的话这里会对着 3400+ 别名再匹配一遍。
    自己独立调用（试译）时不传，走下面的兜底。

    use_cache=False 用于重译与试译 —— 这两种场景的意图就是不要旧结果。
    做成参数而不是临时改全局配置：后者会短暂影响并发中的其他翻译。
    """
    if not text or not text.strip():
        return TranslateResult(status="skipped")

    if not settings.get("TRANSLATE_ENABLED"):
        return TranslateResult(status="disabled")

    # Classification hits may be cross-game and deduplicated by entity. Reload
    # reviewed aliases so every spelling in this translation is represented.
    pool = (glossary.terms_for(game) if game else glossary.terms_all()) if theme == "gaming" else []
    hits = glossary.translation_hits(text, [*(hits or []), *pool], game)
    full_prompt = prompt + glossary.build_table(hits)
    from .kb.service import reference_context
    import asyncio
    reference, _revision = (await asyncio.to_thread(reference_context, text, game)
                            if theme == "gaming" else ("", ""))
    if reference:
        full_prompt += "\n\n参考资料（仅解释背景，规范译名以术语表为准）：\n" + reference
    full_prompt += f"\n\n主题：{theme}；目标语言：zh；翻译协议：3。"
    full_prompt += ("\n\n本次翻译策略：完整翻译标题和正文；未知专名给中文暂译并首次附原词，"
                    "不能把暂译说成官方名称。英文排版粘连应按语义还原，保留原始数值、链接和代码。")

    # 实体上下文。和术语对照表分开注入 —— 前者是「必须这么译」，
    # 后者是「这些是游戏专有实体，别按普通词理解」
    if entities_data and entities_data.get("entities"):
        from .kb.annotate import entities_context
        resolved_game = glossary.normalize_game(game)
        entities = [entity for entity in entities_data["entities"]
                    if not resolved_game or not entity.get("game")
                    or glossary.normalize_game(entity["game"]) == resolved_game]
        full_prompt += entities_context(entities)

    hit_names = [t.source for t in hits]

    # 缓存键含 model，所以要先知道会用哪个后端；取链首即可
    configs = load_configs()
    if not configs:
        return TranslateResult(status="failed",
                               error="没有启用的翻译后端。去后台 Provider 页加一个")
    head_model = configs[0].model

    if use_cache is None:
        use_cache = bool(settings.get("TRANSLATE_CACHE_ENABLED"))
    key = cache_key(text, full_prompt, head_model, game=game,
                    strategy=bilingual_policy,
                    kb_version=str(settings.get("KB_VERSION") or "1"))
    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            zh, prov, model = cached
            miss = (glossary.check_translation(zh, hits)
                    if settings.get("GLOSSARY_CHECK_ENABLED") else [])
            zh, corrected = glossary.correct_translation(zh, hits)
            return TranslateResult(
                text_zh=zh, status="ok", from_cache=True,
                provider_name=prov, model=model,
                glossary_hits=hit_names, glossary_miss=miss, glossary_corrected=corrected,
            )

    blocked = budget_blocked()
    if blocked:
        logger.warning("预算闸门拦截：%s", blocked)
        return TranslateResult(status="budget", error=blocked,
                               glossary_hits=hit_names)

    res = await complete_with_failover(full_prompt, text)
    if not res.ok:
        return TranslateResult(status="failed", error=res.error,
                               provider_name=res.provider_name or None,
                               glossary_hits=hit_names)

    cost = _cost_for(res.provider_name, res.tokens_in, res.tokens_out)
    record_usage(res.provider_name, res.tokens_in, res.tokens_out, cost)
    miss = (glossary.check_translation(res.text, hits)
            if settings.get("GLOSSARY_CHECK_ENABLED") else [])
    corrected_text, corrected = glossary.correct_translation(res.text, hits)
    issue = validate_literals(text, corrected_text)
    if theme != "gaming" and not issue:
        from .themes import get_theme
        terms = get_theme(theme).config.get("terms", {})
        if any(str(source).casefold() in text.casefold() and str(target) not in corrected_text
               for source, target in terms.items()):
            issue = "主题术语校验失败，保留原文并等待重试"
    if issue:
        return TranslateResult(status="failed", error=issue, provider_name=res.provider_name,
                               model=res.model, tokens_in=res.tokens_in, tokens_out=res.tokens_out,
                               cost=cost, glossary_hits=hit_names)
    if settings.get("TRANSLATE_CACHE_ENABLED"):
        # 重译时不读缓存但仍写入，这样后续相同内容能命中新结果
        _cache_put(key, corrected_text, res.provider_name, res.model)
    glossary.bump_hits(hits)

    if miss:
        logger.info("术语漏译 %s；本地校正 %s", miss, corrected)

    return TranslateResult(
        text_zh=corrected_text, status="ok", provider_name=res.provider_name,
        model=res.model, tokens_in=res.tokens_in, tokens_out=res.tokens_out,
        cost=cost, glossary_hits=hit_names, glossary_miss=miss, glossary_corrected=corrected,
    )


def validate_literals(source: str, translated: str) -> str | None:
    patterns = (("数字", r"\d+(?:[.,]\d+)*"),
                ("链接", r"https?://[^\s<>\]\)]+"),
                ("代码", r"`[^`\n]+`"))
    for label, pattern in patterns:
        expected = Counter(re.findall(pattern, source))
        actual = Counter(re.findall(pattern, translated))
        if expected - actual:
            return f"译后{label}校验失败，保留原文并等待重试"
    return None
