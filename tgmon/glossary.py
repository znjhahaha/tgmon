"""术语注入 prompt 做强制对照，译后校验漏译。

读 GlossaryEntry + GlossaryAlias 两张表（旧 Glossary 表已弃用，见 bootstrap 搬迁）。

只把「原文里真的出现了的」术语注入 —— 整表塞进去会把 prompt 撑爆（现在光角色就
241 条），也会让模型在无关术语上分心。

翻译只用非中文别名：中文别名（卡妈、看板娘）不是译法而是同义词，注入 prompt 只会
干扰模型。它们服务实体标注，见 kb/annotate.py。
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from .db import session_scope
from .models import GlossaryAlias, GlossaryEntry
from .util import has_cjk

logger = logging.getLogger(__name__)

# 别名匹配优先级。社区外号在爆料帖里出现的概率远高于官方称号，
# 命座名基本只在正式文案里出现，所以排最后
_KIND_PRIORITY = {
    "community": 0,
    "abbrev": 1,
    "primary": 2,
    "official_title": 3,
    "constellation": 4,
}


# channel.game 是后台手填的自由文本，KB 里的规范值却带冒号（"崩坏:星穹铁道"）。
# 频道上写「崩铁」曾导致该游戏术语一条都匹配不到，而且完全静默 —— 有通用术语兜底，
# 看起来像是在工作。所以匹配前先规范化，别指望人填得和数据源一字不差。
#
# 键是「小写 + 去空格」后的形态，所以 "Star Rail" / "Zenless Zone Zero" 这类
# 带空格的官方英文名不用单独列。爆料频道的方括号标记就是这么写的
_GAME_ALIASES = {
    "崩铁": "崩坏:星穹铁道",
    "星铁": "崩坏:星穹铁道",
    "崩坏星穹铁道": "崩坏:星穹铁道",
    "崩坏：星穹铁道": "崩坏:星穹铁道",
    "hsr": "崩坏:星穹铁道",
    "starrail": "崩坏:星穹铁道",
    "honkaistarrail": "崩坏:星穹铁道",
    "genshin": "原神",
    "genshinimpact": "原神",
    "gi": "原神",
    "zzz": "绝区零",
    "zenless": "绝区零",
    "zenlesszonezero": "绝区零",
    # 崩坏3。真实数据里 Seele Leaks 会发「HI3rd 9.1」，知识库暂无该游戏的术语，
    # 但判出游戏名本身有价值：prompt 会走「保留原文不要猜译名」的分支
    "崩三": "崩坏3",
    "崩坏三": "崩坏3",
    "hi3": "崩坏3",
    "hi3rd": "崩坏3",
    "honkaiimpact": "崩坏3",
    "honkaiimpact3rd": "崩坏3",
}


def normalize_game(game: str | None) -> str:
    """把用户填的游戏名对齐到 KB 里的规范值。认不出来的原样返回。"""
    if not game:
        return ""
    g = game.strip()
    if not g:
        return ""
    return _GAME_ALIASES.get(g.lower().replace(" ", ""), g)


@dataclass
class Term:
    """一条待注入的术语。source 是原文里的写法，target 是规范中文名。"""
    entry_id: int
    alias_id: int
    source: str
    target: str
    game: str = ""                  # 该术语所属游戏，用于投票检测 game_detected
    note: str = ""
    case_sensitive: bool = False
    match_mode: str = "word"
    alias_kind: str = "primary"
    category: str = "other"
    origin: str = ""
    attrs: dict[str, Any] = field(default_factory=dict)


def _row_to_term(a: GlossaryAlias, e: GlossaryEntry) -> Term:
    return Term(entry_id=e.id, alias_id=a.id, source=a.surface,
                target=e.canonical_zh, game=e.game or "", note=e.note or "",
                case_sensitive=a.case_sensitive, match_mode=a.match_mode,
                alias_kind=a.alias_kind, category=e.category, origin=e.origin or "",
                attrs=e.attrs or {})


_TERMS_CACHE: dict[tuple, tuple[float, list[Term]]] = {}
_CACHE_TTL = 60.0  # 秒。术语改动不频繁，缓存 1 分钟可接受


def _load_terms(games: tuple[str, ...] | None,
                langs: tuple[str, ...]) -> list[Term]:
    """按 (games, langs) 取术语，带 TTL 缓存。

    games=None 表示不限游戏。每条消息都要调用（terms_all 一次 + terms_for 一次），
    不缓存的话光这个 join 就是几千行 × 每条消息。
    """
    from . import settings
    key = (games, langs, str(settings.get("KB_VERSION") or "1"))
    now = time.time()
    hit = _TERMS_CACHE.get(key)
    if hit is not None and now - hit[0] <= _CACHE_TTL:
        return hit[1]

    with session_scope() as s:
        q = (s.query(GlossaryAlias, GlossaryEntry)
             .join(GlossaryEntry, GlossaryAlias.entry_id == GlossaryEntry.id)
             .filter(GlossaryAlias.enabled.is_(True),
                     GlossaryAlias.lang.in_(langs),
                     GlossaryEntry.enabled.is_(True),
                     # pending / rejected 的一律不参与，避免未过审的术语
                     # 静默影响翻译
                     GlossaryEntry.status == "active"))
        if games is not None:
            q = q.filter(GlossaryEntry.game.in_(games))
        from .models import KnowledgeSource
        from urllib.parse import urlparse
        sources = s.query(KnowledgeSource).all()
        enabled = {(normalize_game(src.game), "wiki:" + urlparse(src.url).netloc)
                   for src in sources if src.enabled}
        terms = [_row_to_term(a, e) for a, e in q.all() if has_cjk(e.canonical_zh)
                 and (not e.origin.startswith("wiki:") or (normalize_game(e.game), e.origin) in enabled)]

    _TERMS_CACHE[key] = (now, terms)
    return terms


def invalidate_cache() -> None:
    """术语表被改动后调用，让下一次读取重新查库。"""
    _TERMS_CACHE.clear()


def touch_version() -> None:
    """Invalidate cached terminology and translations after a review/edit."""
    from . import settings
    settings.set_many({"KB_VERSION": str(time.time_ns())})
    invalidate_cache()


def terms_for(game: str | None,
              langs: tuple[str, ...] = ("en", "ja")) -> list[Term]:
    """取该 game 的术语 + 通用术语（game 为空的行）。

    langs 默认只要非中文 —— 翻译场景下中文别名没有意义。实体标注要全部别名时
    传 ("en", "ja", "zh")。
    """
    g = normalize_game(game)
    keys = ("",) if not g else ("", g)
    return _load_terms(keys, langs)


def terms_all(langs: tuple[str, ...] = ("en", "ja")) -> list[Term]:
    """取全部术语（不限游戏），用于跨游戏匹配与按实体投票判定 game。"""
    return _load_terms(None, langs)


@lru_cache(maxsize=8192)
def _compile_pattern(source: str, match_mode: str,
                     case_sensitive: bool) -> re.Pattern | None:
    """按 match_mode 编译匹配式。

    word 用 \\b 边界（ASCII 词），substring 直接子串（CJK 没有词边界），
    regex 是给「7\\.\\d 版本号」这类模式留的后门。

    **必须缓存。** 线上有 3465 个别名，而 `re` 模块内部的编译缓存只有 512 条 ——
    跨游戏匹配（terms_all）每条消息都会把它打穿，退化成每次真编译几千个 pattern。
    参数用三元组而不是 Term：Term 是可变 dataclass，不可哈希。
    """
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        if match_mode == "regex":
            return re.compile(source, flags)
        esc = re.escape(source)
        if match_mode == "word":
            return re.compile(rf"\b{esc}\b", flags)
        return re.compile(esc, flags)
    except re.error as e:
        logger.warning("术语 %r 正则无效: %s", source, e)
        return None


def _compile(term: Term) -> re.Pattern | None:
    return _compile_pattern(term.source, term.match_mode, term.case_sensitive)


def find_hits(text: str, terms: list[Term]) -> list[Term]:
    """原文里真的出现了的术语。

    同一实体命中多个别名时只留一个（优先级高的），否则对照表里会出现
    「Kafka → 卡芙卡」「卡妈 → 卡芙卡」两行说同一件事。
    """
    if not text:
        return []

    hits: list[Term] = []
    for t in terms:
        if not t.source:
            continue
        pat = _compile(t)
        if pat is not None and pat.search(text):
            hits.append(t)

    # 每个实体只留优先级最高、写法最长的那条
    best: dict[int, Term] = {}
    for t in hits:
        cur = best.get(t.entry_id)
        if cur is None or _rank(t) < _rank(cur):
            best[t.entry_id] = t

    out = list(best.values())
    # 长术语优先，避免「Boss」抢掉「Boss Rush」的位置
    out.sort(key=lambda x: len(x.source), reverse=True)
    return out


def _rank(t: Term) -> tuple[int, int]:
    return (_KIND_PRIORITY.get(t.alias_kind, 99), -len(t.source))


_PROTECTED = re.compile(
    r"```[\s\S]*?(?:```|$)|`[^`\n]*`|https?://[^\s<>「」]+|"
    r"(?<!\w)[@#][\w.-]+|[\w.+-]+@[\w.-]+\.\w+|"
    r"\b[\w./-]+\.(?:json|csv|txt|png|jpg|webp|py|js|mp4|exe)\b", re.I)


@lru_cache(maxsize=8192)
def _translation_pattern(source: str, case_sensitive: bool):
    escaped = re.escape(source)
    escaped = escaped.replace(r"\ ", r"\s+").replace(r"\-", "[-‐‑–—]")
    # ASCII boundaries still match an English name immediately followed by Chinese.
    return re.compile(r"(?<![A-Za-z0-9_])" + escaped + r"(?![A-Za-z0-9_])",
                      0 if case_sensitive else re.I)


def translation_hits(text: str, terms: list[Term], game: str | None = None) -> list[Term]:
    """All unambiguous aliases actually present, scoped to the resolved game."""
    game = normalize_game(game)
    searchable = _PROTECTED.sub(lambda m: " " * len(m.group()), text)
    candidates = []
    targets: dict[str, set[str]] = {}
    for term in terms:
        if not term.source or has_cjk(term.source) or not has_cjk(term.target):
            continue
        if game and term.game and normalize_game(term.game) != game:
            continue
        pattern = _compile(term) if term.match_mode == "regex" else _translation_pattern(term.source, term.case_sensitive)
        if pattern is None:
            continue
        spans = [m.span() for m in pattern.finditer(searchable)]
        if spans:
            candidates.append((term, spans))
            targets.setdefault(term.source.casefold(), set()).add(term.target)
    candidates.sort(key=lambda item: -len(item[0].source))
    occupied, out, seen = [], [], set()
    for term, spans in candidates:
        if len(targets[term.source.casefold()]) != 1 or (term.source.casefold(), term.target) in seen:
            continue
        if not any(not any(a <= start and end <= b for a, b in occupied) for start, end in spans):
            continue
        occupied.extend(spans)
        out.append(term)
        seen.add((term.source.casefold(), term.target))
    return out


def correct_translation(text_zh: str, hits: list[Term]) -> tuple[str, list[str]]:
    """Replace surviving reviewed source names; never guess a Chinese misspelling."""
    candidates = translation_hits(text_zh, hits)
    replacements = []
    protected = [m.span() for m in _PROTECTED.finditer(text_zh)]
    for term in candidates:
        if term.match_mode == "regex":
            continue
        for match in _translation_pattern(term.source, term.case_sensitive).finditer(text_zh):
            start, end = match.span()
            if any(start < b and end > a for a, b in protected):
                continue
            if any(start < b and end > a for a, b, _, _ in replacements):
                continue
            replacements.append((start, end, term.target, term.source))
    corrected = list(dict.fromkeys(source for _, _, _, source in replacements))
    for start, end, target, source in sorted(replacements, reverse=True):
        text_zh = text_zh[:start] + target + text_zh[end:]
    return text_zh, corrected


# 属性里值得写进对照表的字段。带上它们能消歧义 ——
# 「Kafka（5★ 虚无 雷）→ 卡芙卡」模型就不会当成作家卡夫卡
# rank 是绝区零的 S/A/B 级，和星数是两套体系（S 级不等于 5★），
# 所以单独一个键。渲染成「S级」而不是裸 S，免得 prompt 里像个代号
_ATTR_ORDER = ("rarity", "rank", "path", "element", "elementType",
               "weaponType", "region", "faction")
_ATTR_CLEAN = {
    "ELEMENT_": "", "WEAPON_": "", "FIGHT_PROP_": "",
}

# 上游给的是内部枚举名（StarRailRes 的 path=Warlock、element=Thunder），
# 往中文 prompt 里塞英文枚举很别扭，在展示层翻掉。
# 放这里而不是导入时改：attrs 保持和源一致，改错了不用重新导一遍
_ENUM_ZH = {
    # 崩铁命途
    "Warlock": "虚无", "Knight": "存护", "Rogue": "巡猎", "Mage": "智识",
    "Priest": "丰饶", "Warrior": "毁灭", "Shaman": "同谐", "Memory": "记忆",
    "Elation": "欢愉",
    # 崩铁属性
    "Thunder": "雷", "Fire": "火", "Ice": "冰", "Wind": "风",
    "Physical": "物理", "Quantum": "量子", "Imaginary": "虚数",
    # 原神元素（已去掉 ELEMENT_ 前缀后的值）
    "PYRO": "火", "HYDRO": "水", "ANEMO": "风", "ELECTRO": "雷",
    "DENDRO": "草", "CRYO": "冰", "GEO": "岩", "NONE": "",
    # 原神武器类型（已去掉 WEAPON_ 前缀）
    "SWORD_ONE_HAND": "单手剑", "CLAYMORE": "双手剑", "POLE": "长柄武器",
    "BOW": "弓", "CATALYST": "法器",
}


def _fmt_attrs(attrs: dict[str, Any]) -> str:
    if not attrs:
        return ""
    parts: list[str] = []
    for k in _ATTR_ORDER:
        v = attrs.get(k)
        if v in (None, "", []):
            continue
        sv = str(v)
        for pre, rep in _ATTR_CLEAN.items():
            if sv.startswith(pre):
                sv = sv.replace(pre, rep, 1)
        sv = _ENUM_ZH.get(sv, sv)
        if not sv:
            continue
        if k == "rarity":
            sv = f"{sv}★"
        elif k == "rank":
            sv = f"{sv}级"
        parts.append(sv)
    return " ".join(parts)


def build_table(hits: list[Term]) -> str:
    """生成注入 prompt 的对照表。"""
    if not hits:
        return ""
    lines = ["", "【术语对照表 —— 必须严格使用右侧译法】"]
    for t in hits:
        line = f"- {t.source} → {t.target}"
        extra = []
        if t.game:
            extra.append(t.game)
        a = _fmt_attrs(t.attrs)
        if a:
            extra.append(a)
        if t.note:
            extra.append(t.note)
        if extra:
            line += f"（{'，'.join(extra)}）"
        lines.append(line)
    return "\n".join(lines)


def check_translation(text_zh: str, hits: list[Term]) -> list[str]:
    """译后校验：原文命中的术语，译文里对应译法却没出现 → 记为漏译。"""
    if not text_zh:
        return []
    missed = []
    for t in hits:
        if not t.target:
            continue
        # An approved Chinese name is mandatory. Keeping its English source is a miss.
        if t.target in text_zh:
            continue
        if not has_cjk(t.target):
            continue
        missed.append(t.source)
    return missed


def bump_hits(terms: list[Term]) -> None:
    """命中计数。entry 和 alias 各记一份 —— 前者看实体热度，
    后者看哪个写法在爆料圈实际流行。"""
    if not terms:
        return
    try:
        with session_scope() as s:
            for t in terms:
                e = s.get(GlossaryEntry, t.entry_id)
                if e is not None:
                    e.hit_count += 1
                a = s.get(GlossaryAlias, t.alias_id)
                if a is not None:
                    a.hit_count += 1
    except Exception as e:
        logger.debug("术语命中计数写入失败: %s", e)
