"""Prompt 分层解析。global → game → channel，高层覆盖低层。"""
from __future__ import annotations

from typing import Any, Protocol

from .db import session_scope
from .models import PromptTemplate


class HasPromptFields(Protocol):
    """只要有这两个字段就能用 —— ORM 的 Channel 和 pipeline 的快照都满足。"""
    game: Any
    prompt_override: Any


DEFAULT_GLOBAL_PROMPT = """你是游戏行业情报翻译员。把下面的游戏爆料原文翻译成简体中文。

硬性要求：
1. 保留原始排版结构：标题装饰（如 ◆ ◇ 【】)、项目符号层级、空行、缩进一律照抄。
2. 数值、版本号、日期、代码名、文件名、ID 一律原样保留，不要换算、不要改格式。
3. 严格使用给定术语对照表的译法。结合参考资料理解专名；可靠中文未知时给中文暂译，首次用「中文名（原词，暂译）」标注，不把暂译当作官方译名。
4. 标题和正文完整译成中文。修复排版导致的英文粘连，不改动名称、数值和代码；无法理解的片段附原文并标注不确定，不补充无依据的背景。
5. 只输出译文本身。不要加评论、不要总结、不要写「以下是翻译」这类前言。
6. 原文里的链接、@提及、#标签原样保留。
7. 专有名词（角色 / 光锥 / 命途 / 地区 / 活动名）在译文里用「」括起来。
   例：惩罚时间到！让我看看是哪些坏孩子没抽「欢愉」。

特别注意：
- 游戏专有名词不只有角色名。武器、遗器、光锥、音擎、圣遗物、活动名、地区名、
  派系名、元素/属性/命途名称等，只要术语表里给出了译法，就必须严格使用。
- 这是多游戏爆料频道。一条消息可能同时提到原神、崩坏星穹铁道、绝区零等多款游戏的内容。
  注意区分不同游戏的术语体系：原神的「圣遗物」不是崩铁的「遗器」，崩铁的「命途」
  不是原神的「元素」。术语对照表会标注游戏归属，按原文实际所属游戏使用对应译法。

输出格式：直接给译文，不要包在代码块里。"""


def game_context(game: str | None) -> str:
    """按本条消息判定出的游戏拼一段动态说明，接在 prompt 尾部。

    和 glossary.build_table() 同样的注入方式 —— prompt 模板是人在后台维护的，
    不该把「本条判定为 X」这种每条都不同的内容写进模板里。

    判不出游戏时给的是「保留原文」而不是随便挑一个：猜错游戏比不猜更糟，
    会把另一款游戏的译名套上去（「沃佳尼察」就是这么来的）。
    """
    if game:
        return (f"\n\n【本条所属游戏】已判定为《{game}》。"
                f"术语一律按下方对照表，不要套用其他游戏的译名。")
    return ("\n\n【本条所属游戏】未能确定。"
            "避免套用其他游戏术语；名称仍给中文暂译并首次附原词，清楚标注暂译。")


def resolve_prompt(channel: HasPromptFields | None, game: str | None = None) -> str:
    """取该频道生效的 prompt。频道覆盖 > game 层 > 全局。"""
    if channel is not None and (channel.prompt_override or "").strip():
        return channel.prompt_override.strip()
    theme = getattr(channel, "theme", "gaming") if channel is not None else "gaming"
    if theme != "gaming":
        from .themes import get_theme
        package = get_theme(theme)
        return (str(package.config.get("prompt") or "") +
                "\n保留原始排版、数值、链接和代码。只输出简体中文译文。\n" +
                "\n".join(f"{k}: {v}" for k, v in package.config.get("terms", {}).items()))

    key = (game or (channel.game if channel else "") or "").strip()
    with session_scope() as s:
        if key:
            row = (s.query(PromptTemplate)
                   .filter(PromptTemplate.scope == "game",
                           PromptTemplate.scope_key == key)
                   .first())
            if row and (row.body or "").strip():
                return row.body.strip()
        row = (s.query(PromptTemplate)
               .filter(PromptTemplate.scope == "global")
               .first())
        if row and (row.body or "").strip():
            return row.body.strip()
    return DEFAULT_GLOBAL_PROMPT
