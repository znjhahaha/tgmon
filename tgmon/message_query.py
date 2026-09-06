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


def distinct_stories():
    other = aliased(MonitorMessage)
    earlier = or_(other.published_at < MonitorMessage.published_at,
                  and_(other.published_at == MonitorMessage.published_at,
                       other.id < MonitorMessage.id))
    repeated = and_(MonitorMessage.text_hash.isnot(None), MonitorMessage.text_hash != "",
                    other.text_hash == MonitorMessage.text_hash,
                    other.duplicate_of.is_(None), earlier)
    from sqlalchemy import exists, select
    return and_(MonitorMessage.duplicate_of.is_(None),
                ~exists(select(other.id).where(repeated)))


def query(s, *, game: str = "", keyword: str = "", duplicates: str = "hide",
          channel_ids: list[int] | None = None, since: datetime | None = None):
    q = s.query(MonitorMessage).join(Channel, Channel.id == MonitorMessage.channel_id)
    if game:
        q = q.filter(effective_game() == normalize_game(game))
    if channel_ids:
        q = q.filter(MonitorMessage.channel_id.in_(channel_ids))
    if keyword:
        q = q.filter(or_(MonitorMessage.text_raw.ilike(f"%{keyword}%"),
                         MonitorMessage.text_zh.ilike(f"%{keyword}%")))
    if duplicates == "hide":
        q = q.filter(distinct_stories())
    elif duplicates == "only":
        q = q.filter(MonitorMessage.duplicate_of.isnot(None))
    if since:
        q = q.filter(MonitorMessage.published_at >= since)
    return q


def project(s, m: MonitorMessage) -> dict:
    ch = s.get(Channel, m.channel_id)
    children = []
    frontier = [m.id]
    while frontier:
        found = [mid for mid, in s.query(MonitorMessage.id).filter(MonitorMessage.duplicate_of.in_(frontier))
                 if mid != m.id and mid not in children]
        children.extend(found)
        frontier = found
    media = s.query(MessageMedia).filter(MessageMedia.message_id.in_([m.id, *children])).order_by(
        MessageMedia.message_id != m.id, MessageMedia.id).all()
    photos = list(dict.fromkeys(x.thumb_path for x in media if x.kind == "photo" and x.thumb_path))
    game = normalize_game(m.game_detected or (
        ch.games[0] if ch and isinstance(ch.games, list) and len(ch.games) == 1
        else "" if ch and ch.games and len(ch.games) > 1 else ch.game if ch else ""))
    return {"id": m.id, "title": ch.title if ch else "", "game": game,
            "text": (m.text_zh or m.text_raw or "").strip(), "raw": m.text_raw or "",
            "published_at": m.published_at, "deeplink": m.deeplink or "",
            "version": m.version_tag or "", "has_media": bool(media),
            "photos": photos, "media_ids": [x.id for x in media],
            "media": [{"id": x.id, "kind": x.kind, "thumb": x.thumb_path,
                       "duration": x.duration, "width": x.width, "height": x.height,
                       "video_status": x.video_status, "video_path": x.video_path,
                       "video_bytes": x.video_bytes, "orig_bytes": x.orig_bytes,
                       "has_spoiler": bool(x.has_spoiler)} for x in media],
            "source_ids": [m.id, *children],
            "video_mid": next((x.message_id for x in media if x.kind == "video" and x.video_path), 0),
            "has_spoiler": bool(m.has_spoiler)}


def latest(n: int = 3, game: str | None = None, **filters) -> list[dict]:
    with session_scope() as s:
        rows = query(s, game=game or "", **filters).order_by(
            MonitorMessage.published_at.desc(), MonitorMessage.id.desc()).limit(max(1, min(n, 50))).all()
        return [project(s, m) for m in rows]


def by_ids(ids: list[int]) -> list[dict]:
    with session_scope() as s:
        out, seen = [], set()
        for mid in ids:
            m = s.get(MonitorMessage, int(mid))
            visited = set()
            while m and m.duplicate_of and m.id not in visited:
                visited.add(m.id)
                m = s.get(MonitorMessage, m.duplicate_of)
            if m and m.id not in seen:
                seen.add(m.id)
                out.append(project(s, m))
        return out
