"""Shared game identity, story visibility and message projections."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import aliased

from .db import session_scope
from .glossary import _GAME_ALIASES, normalize_game
from .models import Channel, MessageMedia, MonitorMessage


def canonical_game(column):
    key = func.lower(func.replace(func.replace(func.trim(column), " ", ""), "：", ":"))
    aliases = {k.lower().replace(" ", "").replace("：", ":"): v
               for k, v in _GAME_ALIASES.items()}
    return case(aliases, value=key, else_=column)


def effective_game():
    single = case((func.json_array_length(Channel.games) == 1,
                   func.json_extract(Channel.games, "$[0]")),
                  (func.json_array_length(Channel.games) > 1, None), else_=Channel.game)
    return canonical_game(func.coalesce(func.nullif(MonitorMessage.game_detected, ""), single))


def distinct_stories(per_channel: bool = False):
    """「只看首发」的可见性条件。

    per_channel=False（全局视图）：跨频道转发只显示最早一条 —— 原设计。
    per_channel=True（频道视图，调用方传了 channel_ids）：只隐藏「同频道」
    的重复。跨频道的判重标记（dup 指向别的频道）与跨频道的同文首发都
    不再遮蔽本频道 —— 否则用户按频道筛选时会看到「这条爆料在该频道下
    彻底消失」（2026-09 反馈：Seele Leaks 的视频被 Galaxy_Leak 的首发
    判重隐藏，频道里找不到了）。
    """
    other = aliased(MonitorMessage)
    earlier = or_(other.published_at < MonitorMessage.published_at,
                  and_(other.published_at == MonitorMessage.published_at,
                       other.id < MonitorMessage.id))
    # 与入库判重（_find_text_dup）同门槛：normalize 后不足
    # DEDUP_MIN_TEXT_LEN 的短标签文本不参与同文隐藏。爆料人连发多条
    # 同标签、不同图的爆料是独立内容，入库时有意豁免；查询侧漏掉这
    # 个门槛会把它们藏得只剩最早一条（2026-09「相册 6 张显示 3 张、
    # 网站上找不到」的根因）。normalize 只删空白标点，raw 长度 >=
    # 门槛是 norm 达标的必要条件，边缘差异可忽略。
    from .settings import get as _settings_get
    min_len = int(_settings_get("DEDUP_MIN_TEXT_LEN") or 20)
    repeated = and_(MonitorMessage.text_hash.isnot(None), MonitorMessage.text_hash != "",
                    other.text_hash == MonitorMessage.text_hash,
                    other.text_raw == MonitorMessage.text_raw, other.theme == MonitorMessage.theme,
                    other.duplicate_of.is_(None), earlier,
                    func.length(other.text_raw) >= min_len)
    from sqlalchemy import exists, select
    repeated = and_(repeated, ~exists(select(MessageMedia.id).where(
        MessageMedia.message_id == MonitorMessage.id)))
    if not per_channel:
        return and_(MonitorMessage.duplicate_of.is_(None),
                    ~exists(select(other.id).where(repeated)))
    # 频道视图：只有同频道的更早首发才遮蔽本频道的消息
    repeated = and_(repeated, other.channel_id == MonitorMessage.channel_id)
    # 显式判重：duplicate_of 指向同频道的消息才算重复；指向其它频道
    # 的（跨频道转发）在本频道照常显示
    parent = aliased(MonitorMessage)
    same_ch_dup = and_(
        MonitorMessage.duplicate_of.isnot(None),
        exists(select(parent.id).where(
            parent.id == MonitorMessage.duplicate_of,
            parent.channel_id == MonitorMessage.channel_id)))
    return and_(or_(MonitorMessage.duplicate_of.is_(None), ~same_ch_dup),
                ~exists(select(other.id).where(repeated)))


def query(s, *, game: str = "", keyword: str = "", duplicates: str = "hide",
          channel_ids: list[int] | None = None, since: datetime | None = None,
          theme: str = ""):
    q = s.query(MonitorMessage).join(Channel, Channel.id == MonitorMessage.channel_id)
    if game:
        q = q.filter(effective_game() == normalize_game(game))
    if theme:
        q = q.filter(MonitorMessage.theme == theme)
    if channel_ids:
        q = q.filter(MonitorMessage.channel_id.in_(channel_ids))
    if keyword:
        q = q.filter(or_(MonitorMessage.text_raw.ilike(f"%{keyword}%"),
                         MonitorMessage.text_zh.ilike(f"%{keyword}%")))
    if duplicates == "hide":
        # 按频道筛选时用频道感知版：跨频道的转发在该频道下照常显示
        q = q.filter(distinct_stories(per_channel=bool(channel_ids)))
    elif duplicates == "only":
        q = q.filter(MonitorMessage.duplicate_of.isnot(None))
    if since:
        q = q.filter(MonitorMessage.published_at >= since)
    return q


def project_many(s, rows: list[MonitorMessage]) -> list[dict]:
    if not rows:
        return []
    channels = {c.id: c for c in s.query(Channel).filter(Channel.id.in_({m.channel_id for m in rows}))}
    descendants = {m.id: [] for m in rows}
    owners = {m.id: {m.id} for m in rows}
    frontier = list(owners)
    seen_edges = set()
    while frontier:
        found = s.query(MonitorMessage.id, MonitorMessage.duplicate_of).filter(
            MonitorMessage.duplicate_of.in_(frontier)).all()
        frontier = []
        for mid, parent in found:
            if (mid, parent) in seen_edges:
                continue
            seen_edges.add((mid, parent))
            owners.setdefault(mid, set()).update(owners[parent])
            for root in owners[parent]:
                if mid != root and mid not in descendants[root]:
                    descendants[root].append(mid)
            frontier.append(mid)
    assets = {}
    for asset in s.query(MessageMedia).filter(MessageMedia.message_id.in_(owners),
                                            MessageMedia.status != "superseded").order_by(MessageMedia.id):
        assets.setdefault(asset.message_id, []).append(asset)
    return [_projection(m, channels.get(m.channel_id),
                        [a for mid in [m.id, *descendants[m.id]] for a in assets.get(mid, [])],
                        descendants[m.id]) for m in rows]


def project(s, m: MonitorMessage) -> dict:
    return project_many(s, [m])[0]


def _projection(m, ch, media, children):
    distinct, asset_keys = [], set()
    for asset in media:
        key = (asset.kind, asset.sha256 or asset.source_identity or asset.thumb_path or str(asset.id))
        if key not in asset_keys:
            distinct.append(asset)
            asset_keys.add(key)
    media = distinct
    photos = list(dict.fromkeys(x.thumb_path for x in media if x.kind == "photo" and x.thumb_path))
    game = normalize_game(m.game_detected or (
        ch.games[0] if ch and isinstance(ch.games, list) and len(ch.games) == 1
        else "" if ch and ch.games and len(ch.games) > 1 else ch.game if ch else ""))
    if m.theme != "gaming":
        game = ""
    return {"id": m.id, "title": ch.title if ch else "", "game": game, "theme": m.theme,
            "text": (m.text_zh or m.text_raw or "").strip(), "raw": m.text_raw or "",
            "published_at": m.published_at, "deeplink": m.deeplink or "",
            "version": m.version_tag or "", "has_media": bool(media),
            "photos": photos, "media_ids": [x.id for x in media],
            "media": [{"id": x.id, "kind": x.kind, "thumb": x.thumb_path,
                       "status": x.status, "error": x.error, "sha256": x.sha256,
                       "source_tg_id": x.source_tg_id, "source_identity": x.source_identity,
                       "duration": x.duration, "width": x.width, "height": x.height,
                       "video_status": x.video_status, "video_path": x.video_path,
                       "video_bytes": x.video_bytes, "orig_bytes": x.orig_bytes,
                       "mime": x.mime,
                       "has_spoiler": bool(x.has_spoiler)} for x in media],
            "source_ids": [m.id, *children],
            "video_mid": next((x.message_id for x in media if x.kind == "video" and x.video_path), 0),
            "has_spoiler": bool(m.has_spoiler)}


def latest(n: int = 3, game: str | None = None, **filters) -> list[dict]:
    with session_scope() as s:
        rows = query(s, game=game or "", **filters).order_by(
            MonitorMessage.published_at.desc(), MonitorMessage.id.desc()).limit(max(1, min(n, 50))).all()
        return project_many(s, rows)


def by_ids(ids: list[int]) -> list[dict]:
    with session_scope() as s:
        loaded = {m.id: m for m in s.query(MonitorMessage).filter(MonitorMessage.id.in_(ids))}
        frontier = {m.duplicate_of for m in loaded.values() if m.duplicate_of} - loaded.keys()
        while frontier:
            parents = s.query(MonitorMessage).filter(MonitorMessage.id.in_(frontier)).all()
            loaded.update({m.id: m for m in parents})
            frontier = {m.duplicate_of for m in parents if m.duplicate_of} - loaded.keys()
        out, seen = [], set()
        for mid in ids:
            m = loaded.get(int(mid))
            visited = set()
            while m and m.duplicate_of and m.id not in visited:
                visited.add(m.id)
                m = loaded.get(m.duplicate_of)
            if m and m.id not in seen:
                seen.add(m.id)
                out.append(m)
        return project_many(s, out)
