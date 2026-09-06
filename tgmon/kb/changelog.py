"""内鬼站 changelog 监控 —— gachabase.net 的数据版本变更。

爆料频道是人转述，这里是数据本身：哪个角色的哪些数值在哪个 revision 动了。
两者互补，所以不另开一套展示，直接以 source_type='website' 的 Channel 写进
MonitorMessage，复用现成的消息列表 / RSS / webhook / 判重。

不用翻译：站点 ?lang=chs 给的就是官方中文名，正文是我们自己拼的中文。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import settings
from ..db import session_scope
from ..models import Channel, MonitorMessage
from .importer import _CLIENT

logger = logging.getLogger(__name__)

_SITES = {"绝区零": "zzz", "原神": "gi", "崩坏:星穹铁道": "hsr"}

# 每个游戏的桶名不一样（zzz: agents/wEngines/bangboo/driveDiscs，
# gi: characters/weapons/artifacts/materials，hsr 还有 planarOrnaments），
# 所以不硬编码，从 payload 里发现。这两个是结构字段不是内容桶，要排除
_NOT_BUCKET = {"revisions"}

_BUCKET_ZH = {
    "agents": "代理人", "wEngines": "音擎", "bangboo": "邦布",
    "driveDiscs": "驱动盘", "characters": "角色", "weapons": "武器",
    "artifacts": "圣遗物", "materials": "材料", "lightcones": "光锥",
    "relics": "遗器", "planarOrnaments": "位面饰品", "items": "道具",
}

_STATUS_ZH = {"updated": "改动", "new": "新增", "removed": "移除"}

# 每类改动正文里最多列多少个名字
_NAME_CAP = 24

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 站点是 SvelteKit，首页 HTML 里内嵌了 revisions 数据。没有独立 JSON 接口，
# 所以从这个锚点起截。不带 User-Agent 会 403 —— 是 UA 校验不是 Cloudflare
_ANCHOR = "data:{revisions:["


async def fetch_home(game: str) -> str:
    sub = _SITES.get(game)
    if sub is None:
        raise ValueError(f"未知游戏 {game}")
    r = await _CLIENT.get(f"https://{sub}.gachabase.net/?lang=chs",
                          headers={"User-Agent": _UA,
                                   "Accept-Language": "zh-CN,zh;q=0.9"})
    r.raise_for_status()
    return r.text


def _seg(html: str) -> str:
    """截出 revisions 那段。到 uses:{ 或 结尾为止，够覆盖全部 revision。"""
    i = html.find(_ANCHOR)
    if i < 0:
        return ""
    tail = html[i:]
    end = tail.find(',uses:{')
    return tail[:end] if end > 0 else tail[:200_000]


_REV_META = re.compile(
    r'branch:"([^"]*)",version:"([^"]*)",'
    r'design_revision:(\d+),design_timestamp:(\d+)')

# href 形如 /agents/1621/roxy/beta/3.2.4/18416657，末三段就是 revision 身份
_HREF_ID = re.compile(r'/([^/"?]+)/([^/"?]+)/(\d+)$')
# diff 形如 /diff/agents/1621/roxy?prev=beta_3.2.4_18409985&cur=beta_3.2.4_18416657
_DIFF_ID = re.compile(r'cur=([^_&"]+)_([^_&"]+)_(\d+)')

_F_ID = re.compile(r'\bid:(\d+)')          # \b 挡掉 rarity_id / element_ids
_F_TEXT = re.compile(r'text:"((?:[^"\\]|\\.)*)"')
_F_KEY = re.compile(r'key:"((?:[^"\\]|\\.)*)"')
_F_STATUS = re.compile(r'status:"(\w+)"')
_F_HREF = re.compile(r'href:"([^"]*)"')
_F_DIFF = re.compile(r'diff:"([^"]*)"')


def _match(text: str, i: int, open_ch: str, close_ch: str) -> int:
    """i 指向开括号后一位，返回配对闭括号下标。

    要跳过字符串内部 —— 崩铁有 text:"{NICKNAME}"（开拓者的未替换占位符），
    纯字符计数会把花括号数错，后面所有对象的边界跟着错位。
    """
    depth, n = 1, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n:
                if text[i] == '\\':
                    i += 2
                    continue
                if text[i] == '"':
                    break
                i += 1
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def _objects(inner: str) -> list[str]:
    """把数组内容切成顶层 {...} 对象文本。"""
    out, i, n = [], 0, len(inner)
    while i < n:
        if inner[i] == '{':
            end = _match(inner, i + 1, '{', '}')
            out.append(inner[i:end + 1])
            i = end + 1
        else:
            i += 1
    return out


def _rev_of(obj: str) -> tuple[str, str, int] | None:
    """从记录自身的 href / diff 里取 (branch, version, revision)。

    payload 用 devalue 回引（`revision:a` 指向数组外定义的对象），跟着数组结构
    分组会把简写的那条并进前一条 —— 实测绝区零一条 revision 报出 202 个改动、
    名字重复出现、diff 的 cur= 和自己的 revision 号对不上。每条记录的 href 和
    diff 都自带完整身份，按它分组就不依赖数组结构，也不用解回引。
    """
    m = _F_HREF.search(obj)
    if m:
        h = _HREF_ID.search(m.group(1))
        if h:
            return h.group(1), h.group(2), int(h.group(3))
    m = _F_DIFF.search(obj)
    if m:
        d = _DIFF_ID.search(m.group(1))
        if d:
            return d.group(1), d.group(2), int(d.group(3))
    return None


def _is_placeholder(name: str) -> bool:
    """站点对未公开条目印占位符：`...`、`{NICKNAME}`、`武器-单手剑`。

    这些不是实体名。留在正文里当计数没问题（「新增 6 把单手剑」本身是信息），
    但绝不能进 entities —— 按实体订阅会被这些通用词全量命中。
    """
    if not name or name in ("...", "…"):
        return True
    if "{" in name and "}" in name:
        return True
    return name.startswith(("武器-", "Weapon-"))


def parse_revisions(html: str) -> list[dict[str, Any]]:
    """从首页 HTML 抽出 [{branch, version, revision, ts, changes:[...]}, ...]。

    按记录自带的 href/diff 身份分组，不跟数组结构走 —— 见 _rev_of 的说明。
    时间戳单独收：design_revision -> design_timestamp 的映射在 payload 里出现
    过就够用，某条 revision 没有的话回退 0，写库时用当前时间。
    """
    seg = _seg(html)
    if not seg:
        return []

    ts_by_rev = {int(m.group(3)): int(m.group(4))
                 for m in _REV_META.finditer(seg)}

    # 桶名逐游戏不同（zzz: agents/wEngines/bangboo/driveDiscs，
    # gi: characters/weapons/artifacts/materials，hsr 还多 planarOrnaments），
    # 所以扫所有 `名字:[`，靠 _NOT_BUCKET 和「对象里有没有 status」筛
    grouped: dict[tuple[str, str, int], list[dict]] = {}
    for bm in re.finditer(r'(\w+):\[', seg):
        bucket = bm.group(1)
        if bucket in _NOT_BUCKET:
            continue
        end = _match(seg, bm.end(), '[', ']')
        for obj in _objects(seg[bm.end():end]):
            st = _F_STATUS.search(obj)
            if st is None:
                continue
            ident = _rev_of(obj)
            if ident is None:
                continue
            oid = _F_ID.search(obj)
            txt = _F_TEXT.search(obj)
            key = _F_KEY.search(obj)
            href = _F_HREF.search(obj)
            diff = _F_DIFF.search(obj)
            grouped.setdefault(ident, []).append({
                "bucket": bucket,
                "id": oid.group(1) if oid else "",
                "key": key.group(1) if key else "",
                "name": txt.group(1) if txt else "",
                "status": st.group(1),
                "href": href.group(1) if href else "",
                "diff": diff.group(1) if diff else "",
            })

    out = []
    for (branch, version, rev), changes in grouped.items():
        # 同一实体在一个 revision 里可能被多个桶重复列出，按 (桶, id) 去重
        seen, uniq = set(), []
        for c in changes:
            k = (c["bucket"], c["id"])
            if k in seen:
                continue
            seen.add(k)
            uniq.append(c)
        out.append({"branch": branch, "version": version, "revision": rev,
                    "ts": ts_by_rev.get(rev, 0), "changes": uniq})
    out.sort(key=lambda r: r["revision"], reverse=True)
    return out


def _body(game: str, rev: dict, base: str) -> str:
    """拼中文正文。按桶分组，同桶同状态的合并成一行。"""
    lines = [f"【{game} 数据更新】{rev['branch']} {rev['version']} "
             f"· revision {rev['revision']}"]

    by: dict[tuple[str, str], list[dict]] = {}
    for c in rev["changes"]:
        by.setdefault((c["bucket"], c["status"]), []).append(c)

    for (bucket, status), items in sorted(by.items()):
        label = _BUCKET_ZH.get(bucket, bucket)
        verb = _STATUS_ZH.get(status, status)
        real = [i["name"] for i in items if not _is_placeholder(i["name"])]
        hidden = len(items) - len(real)
        # 一次 revision 能动 160 多个角色，全列出来正文没法看。前 24 个够判断
        # 「这版动了谁」，剩下的给个数
        shown = real[:_NAME_CAP]
        tail = []
        if len(real) > _NAME_CAP:
            tail.append(f"另 {len(real) - _NAME_CAP} 个")
        if hidden:
            tail.append(f"{hidden} 个未公开")
        names = "、".join(shown)
        if tail:
            names = (names + "，" if names else "") + "、".join(tail)
        lines.append(f"{label}{verb}（{len(items)}）：{names or '—'}")

    # diff 链接是这个源最有价值的部分 —— 能直接看到改了哪些数值。
    # 只放前 8 条，一次 revision 动上百条时正文会失控
    # 有名字的排前面 —— 「洛克茜 改了什么」比「14162 改了什么」有用
    all_dif = [c for c in rev["changes"] if c["diff"]]
    all_dif.sort(key=lambda c: _is_placeholder(c["name"]))
    difs = all_dif[:8]
    if difs:
        lines.append("")
        for c in difs:
            label = c["name"] if not _is_placeholder(c["name"]) else f"#{c['id']}"
            lines.append(f"· {label} {base}{c['diff']}")
        more = len(all_dif) - len(difs)
        if more > 0:
            lines.append(f"· …另有 {more} 条改动")
    return "\n".join(lines)


def ensure_channel(game: str) -> int:
    """取或建这个游戏的 changelog 频道。

    source_type='website' 是 Channel 里本来就留的口子。tg_id 存站点域名 ——
    唯一约束是 (source_type, tg_id)，这样三个游戏各占一行不会撞。
    """
    sub = _SITES[game]
    host = f"{sub}.gachabase.net"
    with session_scope() as s:
        ch = (s.query(Channel)
              .filter(Channel.source_type == "website",
                      Channel.tg_id == host).first())
        if ch is None:
            ch = Channel(source_type="website", tg_id=host,
                         title=f"{game} 数据更新（gachabase）",
                         kind="website", game=game, enabled=True,
                         # 正文是我们自己拼的中文，没有可翻的东西
                         translate=False,
                         # 每条 revision 都是独立事件，改动列表相似度很高，
                         # 交给 simhash 判重会把连续版本误判成重复
                         force_push=True)
            s.add(ch)
            s.flush()
        return ch.id


def _watermark(known: set[str], revs: list[dict]) -> int:
    """首跑水位线：低于这个 revision 号的历史条目不发。

    首页一次给出二十多条历史 revision，而 payload 只给最新那条的 design_timestamp
    （见 parse_revisions），其余全落当前时间 —— 首次抓取会把二十多条一起怼进消息
    列表，全部显示为「刚刚」。所以新频道只发最新 N 条。

    水位线取「已存在的最小 revision 号」而不是另立一张表或加列：revision 是单调
    递增的资源号，最小值天然就是这个频道开始监控的位置。这条也让水位线在首跑之后
    继续有效 —— 只在 known 为空时判一次的话，第二次轮询 known 非空、水位线归零，
    首跑压下去的二十多条历史会立刻全部涌出来。
    """
    if known:
        nums = [int(k) for k in known if k.isdigit()]
        return min(nums) if nums else 0
    keep = int(settings.get("CHANGELOG_FIRST_RUN_KEEP") or 0)
    # revs 已按 revision 降序（parse_revisions 结尾排过）
    fresh = [r["revision"] for r in revs if r["changes"]]
    if keep <= 0 or len(fresh) <= keep:
        return 0
    return fresh[keep - 1]


def _stamp(revs: list[dict]) -> dict[int, datetime]:
    """给每条 revision 定 published_at，保证时间顺序和 revision 号顺序一致。

    payload 只给少数 revision 配了 design_timestamp（实测原神 5 条里 1 条有），
    其余得回退。逐条独立回退到 utcnow() 是错的：老 revision 拿到「现在」，比带
    真实时间戳的新 revision（2026-08-31）还晚，在按时间排的消息列表里压在它上面
    —— 实测 47785360 就盖过了 47882175。

    所以从最新往老走，拿一个上限往下推：有真时间戳的用自己的并把上限拉到那里，
    没有的取「上限减一秒」。结果严格递减，和 revision 号同序。
    """
    out: dict[int, datetime] = {}
    cap = datetime.utcnow()
    for rev in revs:                      # revs 已是 revision 降序
        ts = rev["ts"]
        if ts:
            when = datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)
            when = min(when, cap)
        else:
            when = cap - timedelta(seconds=1)
        out[rev["revision"]] = when
        cap = when
    return out


async def poll(game: str, limit: int = 0, first_run: bool = False) -> dict[str, Any]:
    """抓一次，把没见过的 revision 写成消息。幂等。

    去重靠 (channel_id, tg_message_id) 唯一约束 —— tg_message_id 存
    revision 号，同一 revision 抓一百次也只有一条。

    first_run=True 时只建频道记录历史 revision 不写消息 —— 避免首跑涌入 20+
    条陈年 beta 数据。启动后第一次 poll_all() 用它，后续正常增量。
    """
    if game not in _SITES:
        raise ValueError(f"未知游戏 {game}")
    html = await fetch_home(game)
    revs = parse_revisions(html)
    if not revs:
        logger.warning("changelog %s 解析出 0 条 revision，站点可能改版", game)
        return {"game": game, "revisions": 0, "added": 0}

    cid = ensure_channel(game)
    base = f"https://{_SITES[game]}.gachabase.net"
    if limit:
        revs = revs[:limit]

    added = 0
    with session_scope() as s:
        # 频道页那个「监控中/已暂停」开关对 website 频道一样有效 —— 想只盯绝区零
        # 就把另两个停掉，不用去改全局 CHANGELOG_ENABLED
        ch = s.get(Channel, cid)
        if ch is not None and not ch.enabled:
            return {"game": game, "revisions": len(revs), "added": 0,
                    "skipped": "channel_disabled"}
        known = {r[0] for r in s.query(MonitorMessage.tg_message_id)
                 .filter(MonitorMessage.channel_id == cid).all()}
        floor = _watermark(known, revs)
        stamps = _stamp(revs)
        for rev in revs:
            mid = str(rev["revision"])
            if mid in known or rev["revision"] < floor:
                continue
            # 空 revision（isEmptyRevision）没有任何实体改动，只是资源号跳了。
            # 写进去全是噪音，跳过但不当错误
            if not rev["changes"]:
                continue
            text = _body(game, rev, base)
            s.add(MonitorMessage(
                channel_id=cid, tg_message_id=mid,
                text_raw=text, text_zh=text,
                translate_status="skipped", lang_detected="zh",
                deeplink=f"{base}/?branch={rev['branch']}",
                sender_name="gachabase",
                # 没有 ts 的回退值由 _stamp 统一推算，不能各自取 utcnow()
                published_at=stamps.get(rev["revision"]) or datetime.utcnow(),
                # 裸列表，和 pipeline.py 的写法一致；读一律走 entity_list()
                entities=sorted({c["name"] for c in rev["changes"]
                                 if not _is_placeholder(c["name"])}) or None,
                glossary_hits={"revision": rev["revision"],
                               "version": rev["version"],
                               "branch": rev["branch"],
                               "changes": len(rev["changes"])},
            ))
            added += 1
        if added:
            ch = s.get(Channel, cid)
            ch.message_count = (ch.message_count or 0) + added
            ch.last_message_at = datetime.utcnow()

    logger.info("changelog %s: %d 个 revision，新增 %d 条", game, len(revs), added)
    return {"game": game, "revisions": len(revs), "added": added}


async def poll_all() -> dict[str, Any]:
    out = {}
    for game in _SITES:
        try:
            out[game] = await poll(game)
        except Exception as e:
            logger.warning("changelog %s 抓取失败: %s", game, e)
            out[game] = {"error": str(e)}
    return out
