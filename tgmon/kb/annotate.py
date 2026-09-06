"""实体标注 —— 标出消息里出现了哪些游戏实体。

服务两个场景：
1. RSS 订阅过滤 —— 「只要提到钟离或胡桃的帖子」
2. 翻译上下文 —— 在提及角色时附上属性，避免模型把游戏角色当成现实人物翻

和术语翻译的区别：
- 翻译只用非中文别名（中文别名不是译法）
- 标注用全部别名（中文社区外号命中率反而更高）
- 翻译去重保留最优先别名，标注保留所有实体

游戏判定不在这里 —— 它需要显式标记规则（「GI 7.1」这类）和跨游戏投票，
自成一块，见 tgmon/classify.py。
"""
from __future__ import annotations

import logging
from typing import Any

from ..glossary import Term, find_hits, terms_for

logger = logging.getLogger(__name__)


def annotate(text: str, game: str | None) -> dict[str, Any]:
    """标注消息，返回 {entities: [{id, name, game, category, attrs}, ...]}。

    text 里中英混杂（「卡妈 banner 下半」），所以 terms_for 要拿全部语言。
    管线里走 annotate_from_hits —— 那条路复用已经匹配好的结果，不重复匹配。
    """
    if not text:
        return {"entities": []}
    pool = terms_for(game, langs=("en", "ja", "zh"))
    return annotate_from_hits(find_hits(text, pool), game)


def annotate_from_hits(hits: list[Term], game: str | None = None) -> dict[str, Any]:
    """从已匹配好的术语命中构造实体列表。

    拆出来是为了让 pipeline 只做一次全文匹配：同一批 hits 供游戏判定、
    实体标注、翻译对照表三处复用。

    排除 jargon —— 行话是翻译术语不是实体，「卡池」几乎每条爆料都出现，
    混进实体列表会让按实体订阅完全失去筛选力。它们照样进 glossary_hits，
    后台能看见。
    """
    seen: dict[int, Term] = {}
    for h in hits:
        if h.category == "jargon":
            continue
        if h.entry_id not in seen:      # 同一实体命中多个别名只记一次
            seen[h.entry_id] = h

    entities = [
        {
            "id": t.entry_id,
            "name": t.target,
            # 带上游戏归属：多游戏频道里「同名不同游戏」要能分辨
            "game": t.game or "",
            "category": t.category,
            "attrs": t.attrs or {},
            "source": t.origin or "",
        }
        for t in seen.values()
    ]
    return {"entities": entities}


def entity_list(raw: Any) -> list[dict]:
    """从入库的 entities 列里取实体字典列表。

    列里存的是裸数组（annotate() 返回的是 {"entities": [...]}，写库的一方负责
    拆掉外层）。但 annotate() 的返回值本身也会被直接塞进这一列过，所以这里两种
    形状都认：直接 `for e in raw` 遇到字典会遍历到 key（字符串）然后在 e.get()
    上炸 AttributeError，读 entities 列的代码必须走这个函数。
    """
    if not raw:
        return []
    items = raw.get("entities") if isinstance(raw, dict) else raw
    return [e for e in (items or []) if isinstance(e, dict) and e.get("name")]


def entity_names(raw: Any) -> list[str]:
    """同上，只要实体名 —— 按实体过滤用。"""
    return [e["name"] for e in entity_list(raw)]


def entities_context(entities: list[dict]) -> str:
    """把实体列表格式化成可注入翻译 prompt 的补充上下文。

    例：
      【本条提及以下游戏实体，翻译时保留官方译名】
      - 卡芙卡（5★ 虚无 雷，角色）
      - 决胜的瞬间（4★，光锥）
    """
    if not entities:
        return ""

    from ..glossary import _fmt_attrs  # 复用属性格式化

    lines = ["", "【本条提及以下游戏实体，翻译时保留官方译名】"]
    for e in entities:
        attrs_str = _fmt_attrs(e.get("attrs") or {})
        cat = _cat_zh(e.get("category", ""))
        parts = [e["name"]]
        if attrs_str:
            parts.append(attrs_str)
        if cat:
            parts.append(cat)
        if e.get("source"):
            parts.append("来源:" + str(e["source"]))
        lines.append(f"- {parts[0]}（{'，'.join(parts[1:])}）" if len(parts) > 1 else f"- {parts[0]}")

    return "\n".join(lines)


def _cat_zh(cat: str) -> str:
    return {
        "character": "角色",
        "weapon": "武器",
        "artifact": "圣遗物",
        "enemy": "敌人",
        "lightcone": "光锥",
        "relic": "遗器",
        "wengine": "音擎",
        "bangboo": "邦布",
        "disc": "驱动盘",
        "path": "命途",
        "element": "属性",
        "region": "地区",
        "jargon": "术语",
        "npc": "NPC",
        "faction": "派系",
        "lore": "设定",
    }.get(cat, "")
