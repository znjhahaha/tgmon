"""gachabase.net 抓取 —— 内鬼数据站，补齐官方源拿不到的实体。

为什么需要它：官方 ZenlessData TextMap 里只有角色名能可靠提取（见
importer.import_zzz 的 docstring），音擎/邦布/驱动盘一条都没有。这个站的
`?lang=chs` 页面同时给出官方中文名和英文 slug，一次抓取就是完整别名对，
不需要 AI 参与翻译。

三个实测结论，改代码前先读：

1. 裸 curl 返回 403，加浏览器 User-Agent 就 200 —— 不是人机校验，只是缺 UA。
   httpx 默认 UA 会被拦，所以 _HEADERS 必须带。
2. 必须跟重定向：/w-engines 会 302 到 /w-engines/beta。
3. 站点是 SvelteKit，数据以 JS 字面量内嵌在 HTML 里，没有 JSON API
   （/api/* 全部 404），只能正则抓。记录形如：
     slug:"crimson-craving",name:{key:"Item_Weapon_S_1611_Name",text:"猩红渴望"}

_REC 那个宽松中段是踩过的坑：agents 和 light-cones 在 slug 与 name 之间插了
complete/internal_name 等字段，用紧邻匹配的正则在这两个页面抓到 0 条。
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from ..db import session_scope
from ..util import has_cjk
from .importer import _CLIENT, upsert_entity

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# game -> 子域名
_SITES = {"原神": "gi", "崩坏:星穹铁道": "hsr", "绝区零": "zzz"}

# 每次请求前的间隔（秒）。1.5s×10 页 = 15s，对一个几十秒的后台任务可以接受
_FETCH_GAP = 1.5

# slug 与 name 之间允许插任意非括号字段（agents 有 complete/internal_name）
_REC = re.compile(
    r'slug:"([a-z0-9\-_]+)"'
    r'(?:,[^{}]{0,240}?)?'
    r',name:\{key:"([^"]+)",text:"([^"]*)"'
)

# 属性只取翻译消歧用得上的，和 importer 的 attrs 口径一致。
# rarity 两种页面都叫这个名但含义不同，见 _rarity_of
_RARITY = re.compile(r'\brarity(?:_id)?:(\d+)')
_ELEMENTS = re.compile(r'element_ids:\[([\d,\s]*)\]')

# ZZZ 的 rarity_id 是枚举不是星数：实测安比/可琳/本(A级)=3，艾莲/朱鸢(S级)=4。
# 原神/崩铁那边 rarity 就是真星数（神里绫华=5，香菱=4），不能一起处理 ——
# 直接存原值会让 _fmt_attrs 把 S 级音擎渲染成「4★」，注进 prompt 就是错信息
_ZZZ_RANK = {2: "B", 3: "A", 4: "S"}


def _rarity_of(game: str, raw: int) -> dict[str, Any]:
    if game == "绝区零":
        rank = _ZZZ_RANK.get(raw)
        return {"rank": rank} if rank else {}
    # 埃洛伊上游写的是 rarity:105（联动角色的哨兵值，不是 105 星）。
    # 星数只可能 1-5，超出范围的一律不存 —— 存了 _fmt_attrs 会往 prompt 里
    # 注「105★」，比没有属性更糟
    return {"rarity": raw} if 1 <= raw <= 5 else {}

# 每个游戏抓哪些页，以及落到哪个 category。
# category 必须是 annotate._cat_zh 认得的值，否则 prompt 里的类别标注会是空
_PAGES: dict[str, list[tuple[str, str]]] = {
    "绝区零": [
        ("agents", "character"),
        ("w-engines", "wengine"),
        ("bangboo", "bangboo"),
        ("drive-discs", "disc"),
    ],
    # gi/weapons 故意不抓：那一页 ?lang=chs 也只返回英文名（实测 265 条含中文 0
    # 条，key 是纯数字 hash 而非 TextMap 语义键），抓了会把 Dull Blade 当成
    # canonical_zh 写进去。原神武器的中文名走 genshin-db 官方源，那边是全的
    "原神": [
        ("characters", "character"),
        ("artifacts", "artifact"),
    ],
    # 是 /lightcones 不是 /light-cones —— 后者 404，和其他页的连字符风格不一致
    "崩坏:星穹铁道": [
        ("characters", "character"),
        ("lightcones", "lightcone"),
        ("relics", "relic"),
    ],
}


async def fetch_page(game: str, path: str, retries: int = 3) -> str:
    """抓一个列表页的 HTML。lang=chs 拿官方中文名。

    连着抓十个页面会被限流成 403（实测：单游戏分开跑都成功，三个游戏连跑时
    后六页全 403）。所以每次请求前先隔一会儿，403/429 再退避重试 —— 全量导入
    是几十秒级的后台任务，多等这点时间换的是「一次跑完」而不是抓一半。
    """
    sub = _SITES[game]
    url = f"https://{sub}.gachabase.net/{path}?lang=chs"
    for attempt in range(retries):
        await asyncio.sleep(_FETCH_GAP if attempt == 0 else _FETCH_GAP * 4 * attempt)
        r = await _CLIENT.get(url, headers=_HEADERS)
        if r.status_code in (403, 429) and attempt < retries - 1:
            logger.warning("gachabase %s/%s 被限流(%d)，退避重试 %d/%d",
                           game, path, r.status_code, attempt + 1, retries - 1)
            continue
        r.raise_for_status()
        return r.text
    raise RuntimeError(f"gachabase {game}/{path} 重试 {retries} 次仍失败")


def parse_records(html: str, game: str = "") -> list[dict[str, Any]]:
    """从页面 HTML 抽出 [{slug, key, zh, attrs}, ...]，按 slug 去重。

    同一实体在页面里可能出现多次（列表 + 预加载数据），保留第一次。
    game 只用来判 rarity 口径，不传就按星数原样存。
    """
    out: dict[str, dict[str, Any]] = {}
    for m in _REC.finditer(html):
        slug, key, zh = m.group(1), m.group(2), m.group(3).strip()
        if not zh or slug in out:
            continue
        # 属性在记录之后的一小段里，截 300 字符够覆盖 rarity/element
        tail = html[m.end():m.end() + 300]
        attrs: dict[str, Any] = {}
        rm = _RARITY.search(tail)
        if rm:
            attrs.update(_rarity_of(game, int(rm.group(1))))
        em = _ELEMENTS.search(tail)
        if em and em.group(1).strip():
            attrs["element_ids"] = [int(x) for x in em.group(1).split(",") if x.strip()]
        out[slug] = {"slug": slug, "key": key, "zh": zh, "attrs": attrs}
    return list(out.values())


#匹配是大小写不敏感的（glossary._compile 默认带 IGNORECASE），所以这几个
# 虚词大小写不影响命中，只影响别名在 prompt 术语表里读起来像不像英文原名
_SLUG_LOWER = {"of", "the", "and", "in", "on", "at", "to", "for", "a", "an"}


def slug_to_en(slug: str) -> str:
    """crimson-craving -> Crimson Craving。

    slug 是英文名小写连字符化的结果，还原成可读英文当别名。爆料帖里写的是
    Crimson Craving 而不是 crimson-craving，所以必须还原 —— 匹配用的是这个。
    """
    words = [w for w in slug.replace("_", "-").split("-") if w]
    return " ".join(w.capitalize() if i == 0 or w not in _SLUG_LOWER else w
                    for i, w in enumerate(words))


async def import_gachabase(game: str,
                           categories: list[str] | None = None) -> dict[str, int]:
    """抓一个游戏的全部实体页并入库。

    status="active" 是用户决定的（跳过待审）。origin="gachabase" 是配套的回滚
    路径 —— 这站是同人站且 beta 名字上线前可能改，万一某个译名错了要能按 origin
    批量退回。

    merge_aliases=True：官方源已经建过的实体（比如 ZZZ 角色）要把英文写法补上去，
    直接跳过就白抓了。
    """
    if game not in _PAGES:
        raise ValueError(f"未知游戏: {game}")

    pages = _PAGES[game]
    if categories:
        pages = [(p, c) for p, c in pages if p in categories]

    added = aliases = 0
    for path, category in pages:
        try:
            html = await fetch_page(game, path)
        except Exception as e:
            logger.error("gachabase 抓取 %s/%s 失败: %s", game, path, e)
            continue

        recs = parse_records(html, game)
        if not recs:
            # 抓到 0 条通常意味着站点改版了正则失效，不是真的没数据
            logger.warning("gachabase %s/%s 解析出 0 条，正则可能失效（%d KB）",
                           game, path, len(html) // 1024)
            continue

        # canonical_zh 必须是中文。有的页 ?lang=chs 会静默回落到英文
        # （gi/weapons 整页如此），个别条目也可能缺中文名。写进去就是拿英文当
        # 规范名，后面 annotate 输出的「官方译名」全错，只能在入库前挡掉
        zh_recs = [r for r in recs if has_cjk(r["zh"])]
        if len(zh_recs) < len(recs):
            logger.warning("gachabase %s/%s: %d/%d 条无中文名，已跳过",
                           game, path, len(recs) - len(zh_recs), len(recs))
        if not zh_recs:
            continue

        with session_scope() as s:
            for r in zh_recs:
                al = [(r["zh"], "zh", "primary"),
                      (slug_to_en(r["slug"]), "en", "primary")]
                n_e, n_a = upsert_entity(
                    s, game=game, category=category, canonical_zh=r["zh"],
                    aliases=al, attrs=r["attrs"],
                    origin="gachabase", origin_ref=r["key"],
                    status="active", merge_aliases=True,
                )
                added += n_e
                aliases += n_a
        logger.info("gachabase %s/%s: %d 条记录", game, path, len(recs))

    logger.info("gachabase %s: 新增 %d 实体, %d 别名", game, added, aliases)
    return {"added": added, "aliases": aliases}
