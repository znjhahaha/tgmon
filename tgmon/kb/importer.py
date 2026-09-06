"""知识库导入器 —— 从上游官方数据源拉取实体与译名。

每个游戏一个函数，都异步（数据源在 GitHub，从香港拉 1-5 秒）。
导入的全部落 pending 等人工过审 —— 一条错术语会静默污染之后每一条翻译。

三个源的选型是实测过的，不是搜索结果的第一条：
- Mar-7th/StarRailRes index_min/{cn,en}/ 按 ID join，nickname.json 是社区外号
- theBowja/genshin-db index/{Lang}/*.json namemap 按 slug join，aliases 是官方称号
- ZenlessData TextMap_{Lang}.json 按语义 key join，仓库 2.7 GB 所以只拉两个文件
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ..db import session_scope
from ..models import GlossaryAlias, GlossaryEntry
from ..util import alias_match_mode, has_cjk, unreliable_alias_reason

logger = logging.getLogger(__name__)

# 超时 60 秒 —— GitHub raw 从香港拉有时 3-8 秒，index 文件 2-5 MB
_CLIENT = httpx.AsyncClient(timeout=60.0, follow_redirects=True)


def upsert_entity(
    s,
    *,
    game: str,
    category: str,
    canonical_zh: str,
    aliases: list[tuple[str, str, str]],
    origin: str,
    origin_ref: str | None = None,
    attrs: dict[str, Any] | None = None,
    status: str = "pending",
    merge_aliases: bool = False,
) -> tuple[int, int]:
    """按 (game, canonical_zh) 找或建实体，再补别名。返回 (新建实体数, 新建别名数)。

    aliases 收 [(surface, lang, alias_kind), ...]。lang 传空则按 has_cjk 自动判。
    match_mode 一律走 alias_match_mode —— CJK 没有词边界，必须 substring。
    单字汉字别名是例外：两种模式都不可靠，建进来但 enabled=False，
    见 util.unreliable_alias_reason。

    merge_aliases 决定实体已存在时怎么办：
    - False（默认，三个官方源的历史行为）：整条跳过，不动已有实体
    - True：只补缺失的别名。gachabase 需要这个 —— 官方 ZZZ TextMap 只给了角色名，
      音擎/邦布的英文写法得靠它补上，直接跳过就白抓了
    """
    entry = (s.query(GlossaryEntry)
             .filter(GlossaryEntry.game == game,
                     GlossaryEntry.canonical_zh == canonical_zh)
             .first())
    from .service import origin_trusted
    approved_aliases = [surface for surface, lang, kind in aliases
                        if surface and not has_cjk(surface) and kind == "primary"
                        and unreliable_alias_reason(surface) is None]
    conflicts = bool(approved_aliases and s.query(GlossaryAlias.id).join(GlossaryEntry).filter(
        GlossaryEntry.game == game, GlossaryEntry.canonical_zh != canonical_zh,
        GlossaryEntry.status == "active", GlossaryAlias.enabled.is_(True),
        GlossaryAlias.surface.in_(approved_aliases)).first())
    if entry is None and status == "pending" and origin_trusted(origin) and has_cjk(canonical_zh) and approved_aliases and not conflicts:
        status = "active"
    if conflicts:
        status = "pending"
        attrs = {**(attrs or {}), "review_reason": "conflicting_translation"}
    added = 0
    if entry is None:
        entry = GlossaryEntry(
            game=game, category=category, canonical_zh=canonical_zh,
            attrs=attrs or {}, status=status,
            origin=origin, origin_ref=origin_ref,
        )
        s.add(entry)
        s.flush()
        added = 1
    elif not merge_aliases or entry.status in ("rejected", "disabled") or conflicts:
        return 0, 0

    n_alias = 0
    for surface, lang, kind in aliases:
        surface = (surface or "").strip()
        if not surface:
            continue
        lang = lang or ("zh" if has_cjk(surface) else "en")
        # uq_kb_alias(entry_id, surface, lang) 会挡住重复，但先查一遍免得
        # 每次导入都往事务里塞几千条注定冲突的 INSERT
        dup = (s.query(GlossaryAlias.id)
               .filter(GlossaryAlias.entry_id == entry.id,
                       GlossaryAlias.surface == surface,
                       GlossaryAlias.lang == lang)
               .first())
        if dup is not None:
            continue
        # 匹配不可靠的写法（单字汉字）建进来但停用：留着让人在知识库页看得见
        # 为什么被排除，比静默丢弃好排查。glossary 只加载 enabled 的行
        bad = unreliable_alias_reason(surface)
        s.add(GlossaryAlias(
            entry_id=entry.id, surface=surface, lang=lang,
            alias_kind=kind, match_mode=alias_match_mode(surface),
            enabled=bad is None,
        ))
        if bad:
            logger.info("别名 %r 停用：%s", surface, bad)
        n_alias += 1
    return added, n_alias


async def import_hsr(
    categories: list[str] | None = None,
    import_nicknames: bool = True,
) -> dict[str, int]:
    """崩坏:星穹铁道。Mar-7th/StarRailRes 的 index_min/{cn,en}/ 按 ID join。

    categories: 默认 ["characters", "light_cones", "relics"]。
    import_nicknames: nickname.json 是社区外号（卡妈/看板娘），作为单独一批别名。

    返回 {"added": 新建实体数, "aliases": 新建别名数}
    """
    if categories is None:
        categories = ["characters", "light_cones", "relics"]

    base = "https://raw.githubusercontent.com/Mar-7th/StarRailRes/master/index_min"
    added = 0
    aliases = 0

    # 社区外号先拉，之后每个角色 join 上去
    nicknames: dict[str, list[str]] = {}
    if import_nicknames and "characters" in categories:
        try:
            r = await _CLIENT.get(f"{base}/cn/nickname.json")
            r.raise_for_status()
            nicknames = r.json()
        except Exception as e:
            logger.warning("拉取 nickname.json 失败: %s", e)

    for cat in categories:
        try:
            cn_r = await _CLIENT.get(f"{base}/cn/{cat}.json")
            en_r = await _CLIENT.get(f"{base}/en/{cat}.json")
            cn_r.raise_for_status()
            en_r.raise_for_status()
            cn = cn_r.json()
            en = en_r.json()
        except Exception as e:
            logger.error("拉取 %s 失败: %s", cat, e)
            continue

        with session_scope() as s:
            for id_, cn_obj in cn.items():
                name_zh = (cn_obj.get("name") or "").strip()
                en_obj = en.get(id_)
                name_en = (en_obj.get("name") or "").strip() if en_obj else ""
                if not name_zh or not name_en:
                    continue

                # 属性：只取翻译或过滤用得上的
                attrs: dict[str, Any] = {}
                for k in ("rarity", "path", "element"):
                    if k in cn_obj:
                        attrs[k] = cn_obj[k]

                # 中文规范名本身也要当 primary alias —— 否则「布洛妮娅」这个正名
                # 匹配不到。社区外号来自 nickname.json {类别: {ID: [名字...]}}，
                # 按 ID 索引不是按名字；列表里可能重复规范名，去掉
                al = [(name_zh, "zh", "primary"), (name_en, "en", "primary")]
                al += [(nn, "", "community")
                       for nn in _nickname_list(nicknames, cat, id_)
                       if nn != name_zh]

                # 命途名（欢愉/Elation）辨识度高，官方中英成对、没有需要人
                # 判断的地方，直接 active 参与翻译。属性名（火/Fire）太泛 ——
                # 「fire the employee」这种句子都会命中，pending 留给知识库页
                # 人工过审，批量启用前不参与翻译
                cat_status = "active" if cat == "paths" else "pending"

                n_e, n_a = upsert_entity(
                    s, game="崩坏:星穹铁道", category=_hsr_category(cat),
                    canonical_zh=name_zh, aliases=al, attrs=attrs,
                    origin="starrailres", origin_ref=id_, status=cat_status,
                )
                added += n_e
                aliases += n_a

    logger.info("崩铁: 新增 %d 实体, %d 别名", added, aliases)
    return {"added": added, "aliases": aliases}


def _hsr_category(cat: str) -> str:
    # 崩铁的光锥/遗器不要复用原神的 weapon/artifact —— 玩家管它们叫光锥和遗器，
    # _cat_zh 也分别有对应的中文标签。混用会让提示词里出现「武器」这种
    # 崩铁根本不存在的说法，而且和 gachabase 导进来的同类条目标签打架
    m = {
        "characters": "character",
        "light_cones": "lightcone",
        "relics": "relic",
        "paths": "path",
        "elements": "element",
    }
    return m.get(cat, "other")


def _nickname_list(nicknames: dict, cat: str, id_: str) -> list[str]:
    """从 nickname.json 里取某实体的外号列表。

    结构是 {"characters": {"1005": ["卡芙卡","卡妈","妈妈"]}, "light_cones": {...}}
    —— 按 ID 索引。之前按名字索引所以一个外号都没导进来。
    """
    bucket = nicknames.get(cat)
    if not isinstance(bucket, dict):
        return []
    got = bucket.get(str(id_))
    return [x.strip() for x in got if str(x).strip()] if isinstance(got, list) else []


async def import_genshin(
    categories: list[str] | None = None,
) -> dict[str, int]:
    """原神。theBowja/genshin-db 的 index/{Lang}/*.json namemap 按 slug join。

    categories: 默认 ["characters", "weapons", "artifacts", "enemies", "geographies"]。

    genshin-db 的 aliases 是官方称号 + 命座名（白垩之子 / 白垩之子座），不是
    社区外号，所以 alias_kind 标 official_title 或 constellation 区分。
    """
    if categories is None:
        categories = ["characters", "weapons", "artifacts", "enemies", "geographies"]

    base = "https://raw.githubusercontent.com/theBowja/genshin-db/main/src/data/index"
    added = 0
    aliases = 0

    for cat in categories:
        try:
            cn_r = await _CLIENT.get(f"{base}/ChineseSimplified/{cat}.json")
            en_r = await _CLIENT.get(f"{base}/English/{cat}.json")
            cn_r.raise_for_status()
            en_r.raise_for_status()
            cn = cn_r.json()
            en = en_r.json()
        except Exception as e:
            logger.error("拉取 %s 失败: %s", cat, e)
            continue

        cn_nm = cn.get("namemap", {})
        en_nm = en.get("namemap", {})
        cn_al = cn.get("aliases", {})       # 中文别名（官方称号/命座名）-> slug
        attr_by_slug = _invert_categories(cn)

        with session_scope() as s:
            for slug, name_zh in cn_nm.items():
                name_en = en_nm.get(slug, "")
                if not name_zh or not name_en:
                    continue

                # cn_al 是官方称号/命座名 -> slug。"座" 结尾的是命座名
                al = [(name_zh, "zh", "primary"), (name_en, "en", "primary")]
                al += [(a, "", "constellation" if a.endswith("座") else "official_title")
                       for a, tgt in cn_al.items() if tgt == slug]

                n_e, n_a = upsert_entity(
                    s, game="原神", category=_genshin_category(cat),
                    canonical_zh=name_zh, aliases=al,
                    attrs=attr_by_slug.get(slug, {}),
                    origin="genshin-db", origin_ref=slug,
                )
                added += n_e
                aliases += n_a

    logger.info("原神: 新增 %d 实体, %d 别名", added, aliases)
    return {"added": added, "aliases": aliases}


def _invert_categories(index: dict) -> dict[str, dict[str, Any]]:
    """把 genshin-db 的 properties + categories 翻成 slug -> {属性: 值}。

    这两个字段的关系不直观，踩过一次：
      properties = {"rarity": [5, 4], "elementType": ["ELEMENT_GEO", ...]}
        —— 只是「这个属性有哪些可能取值」，不是每个 slug 的值
      categories  = {"5": [slug...], "ELEMENT_GEO": [slug...]}
        —— 取值到 slug 列表的反向索引
    所以要先遍历 properties 拿到「取值属于哪个属性」，再用 categories 落到 slug。
    之前直接查 properties[k][slug]，结果 attrs 全是空的。
    """
    props = index.get("properties") or {}
    cats = index.get("categories") or {}
    out: dict[str, dict[str, Any]] = {}

    for prop_name, values in props.items():
        if not isinstance(values, list):
            continue
        for val in values:
            for slug in cats.get(str(val), []) or []:
                out.setdefault(slug, {})[prop_name] = val
    return out


def _genshin_category(cat: str) -> str:
    m = {
        "characters": "character",
        "weapons": "weapon",
        "artifacts": "artifact",
        "enemies": "enemy",
        "geographies": "region",
        "elements": "element",
    }
    return m.get(cat, "other")


# 绝区零只有角色能可靠提取，原因见 import_zzz 的 docstring。
_ZZZ_BASE = "https://git.mero.moe/dimbreath/ZenlessData/raw/branch/master/TextMap"
_ZZZ_CN = "TextMapTemplateTb.json"          # 无语言后缀的就是中文
_ZZZ_EN = "TextMap_ENTemplateTb.json"
_ZZZ_KEY = "Avatar_Name"


async def import_zzz() -> dict[str, int]:
    """绝区零。只导角色 —— 这是实测后缩小的范围，不是偷懒。

    ZenlessData 的 TextMap 没有干净的实体表命名空间：411k 个 key 里角色名散落在
    对话说话人标签下（`ActivityBangbooAB_Avatar_Name11`、
    `Activity_Chat_FoodTruck_..._Name_01`），同一个名字出现几十次。实测：

    - `Avatar_Name` 键去重后能 join 出 32 个角色（可玩代理人基本齐了），够用
    - `WeaponName` 只有 8 条，且是活动道具（Charged Ammo / Long Ammo）不是音擎，
      所以武器这条路直接放弃
    - `AvatarBaseTemplate` 之类的实体表命名空间在 TextMap 里不存在

    仓库 2.7 GB，只拉两个 JSON（各约 45 MB），不 clone。
    """
    added = 0
    aliases = 0

    try:
        cn_r = await _CLIENT.get(f"{_ZZZ_BASE}/{_ZZZ_CN}")
        en_r = await _CLIENT.get(f"{_ZZZ_BASE}/{_ZZZ_EN}")
        cn_r.raise_for_status()
        en_r.raise_for_status()
        cn = cn_r.json()
        en = en_r.json()
    except Exception as e:
        logger.error("拉取 ZZZ TextMap 失败: %s", e)
        return {"added": 0, "aliases": 0}

    # 先按 EN 名去重 —— 同名在几十个 key 下重复出现
    pairs: dict[str, tuple[str, str]] = {}      # en -> (zh, 首个 key)
    for key in en:
        if _ZZZ_KEY not in key:
            continue
        name_en = (en.get(key) or "").strip()
        name_zh = (cn.get(key) or "").strip()
        if not name_en or not name_zh or name_en == name_zh:
            continue
        pairs.setdefault(name_en, (name_zh, key))

    with session_scope() as s:
        for name_en, (name_zh, key) in pairs.items():
            n_e, n_a = upsert_entity(
                s, game="绝区零", category="character", canonical_zh=name_zh,
                aliases=[(name_zh, "zh", "primary"), (name_en, "en", "primary")],
                origin="zenlessdata", origin_ref=key,
            )
            added += n_e
            aliases += n_a

    logger.info("绝区零: 新增 %d 实体, %d 别名", added, aliases)
    return {"added": added, "aliases": aliases}


async def import_all_games() -> dict[str, Any]:
    """一键导入三游戏的全部类别。后台调用这个。"""
    results = await asyncio.gather(
        import_hsr(),
        import_genshin(),
        import_zzz(),
        return_exceptions=True,
    )
    return {
        "hsr": results[0] if not isinstance(results[0], Exception) else str(results[0]),
        "genshin": results[1] if not isinstance(results[1], Exception) else str(results[1]),
        "zzz": results[2] if not isinstance(results[2], Exception) else str(results[2]),
    }
