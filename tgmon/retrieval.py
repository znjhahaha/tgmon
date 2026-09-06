"""消息、记忆和知识库的轻量混合检索。

SQLite FTS5 handles exact terms and versions while local float32 vectors add
semantic recall. The module intentionally has no process-global index state so
worker/admin processes can update it independently.
"""
from __future__ import annotations

import asyncio
import heapq
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
from sqlalchemy import and_, bindparam, func, or_, text as sql_text, tuple_

from .db import engine, session_scope
from .models import (Channel, Conversation, KnowledgePage, KnowledgeSource,
                     MonitorMessage, RetrievalIndex)
from . import embeddings, settings

logger = logging.getLogger(__name__)


@dataclass
class RetrievalDocument:
    ref_type: str
    ref_id: int
    text: str
    title: str = ""
    channel: str = ""
    channel_id: int | None = None
    game: str | None = None
    version: str | None = None
    published_at: datetime | None = None
    deeplink: str | None = None
    embedding: bytes | None = None
    embedding_model: str | None = None


@dataclass
class SearchHit:
    ref_type: str
    ref_id: int
    score: float
    text: str
    title: str
    channel: str
    game: str | None = None
    published_at: datetime | None = None
    deeplink: str | None = None
    version: str | None = None


def _embedding_for_text(text: str) -> np.ndarray | None:
    """Local CPU query embedding; missing weights leave lexical search available."""
    if not settings.get("EMBEDDING_ENABLED"):
        return None
    return embeddings.embed_query(text)


def _pack(vec: Any) -> bytes | None:
    try:
        arr = np.asarray(vec, dtype=np.float32).reshape(-1)
        return arr.tobytes() if arr.size and np.isfinite(arr).all() and np.any(arr) else None
    except Exception:
        return None


def _unpack(blob: bytes | bytearray | memoryview | None) -> np.ndarray | None:
    if not blob:
        return None
    try:
        arr = np.frombuffer(bytes(blob), dtype=np.float32)
        return arr if arr.size and np.isfinite(arr).all() and np.any(arr) else None
    except Exception:
        return None


def _fts_sync(row: RetrievalIndex) -> None:
    with engine.begin() as conn:
        try:
            conn.execute(sql_text("DELETE FROM retrieval_fts WHERE ref_type=:t AND ref_id=:i"),
                         {"t": row.ref_type, "i": str(row.ref_id)})
            conn.execute(sql_text(
                "INSERT INTO retrieval_fts(ref_type,ref_id,text,title,channel,game) "
                "VALUES (:t,:i,:x,:title,:channel,:game)"), {
                    "t": row.ref_type, "i": str(row.ref_id), "x": row.text or "",
                    "title": row.title or "", "channel": row.channel or "",
                    "game": row.game or ""})
        except Exception:
            # FTS5 is optional; callers still have the indexed SQL row for LIKE.
            pass


def index_message(message_id: int) -> None:
    """Upsert one message into the unified index. Safe to call repeatedly."""
    with session_scope() as s:
        pair = s.query(MonitorMessage, Channel).join(
            Channel, Channel.id == MonitorMessage.channel_id).filter(
                MonitorMessage.id == int(message_id)).first()
        if pair is None:
            remove("message", int(message_id))
            return
        m, ch = pair
        entities = m.entities if isinstance(m.entities, list) else []
        topics = m.topics if isinstance(m.topics, list) else []
        text_value = "\n".join(x for x in [m.text_zh, m.text_raw,
                                               ("v" + m.version_tag) if m.version_tag else "",
                                               " ".join(str(e.get("name", "")) for e in entities if isinstance(e, dict)),
                                               " ".join(str(x) for x in topics)] if x)
        row = s.query(RetrievalIndex).filter_by(ref_type="message", ref_id=m.id).first()
        if row is None:
            row = RetrievalIndex(ref_type="message", ref_id=m.id)
            s.add(row)
        if row.text != text_value[:20000]:
            row.embedding = None
            row.embedding_model = None
        row.text = text_value[:20000]
        row.title = (m.game_detected or ch.title or "")
        row.channel = ch.title or ""
        row.channel_id = ch.id
        from .message_query import effective_game
        row.game = s.query(effective_game()).select_from(MonitorMessage).join(Channel).filter(MonitorMessage.id == m.id).scalar()
        row.version = m.version_tag
        row.published_at = m.published_at
        row.deeplink = m.deeplink
        s.flush()
        snapshot = RetrievalIndex(ref_type=row.ref_type, ref_id=row.ref_id,
                                  text=row.text, title=row.title, channel=row.channel,
                                  channel_id=row.channel_id,
                                  game=row.game, version=row.version, published_at=row.published_at,
                                  deeplink=row.deeplink)
    _fts_sync(snapshot)


def index_document(document: RetrievalDocument) -> None:
    """Upsert a non-message document (wiki page or memory summary)."""
    with session_scope() as s:
        row = s.query(RetrievalIndex).filter_by(ref_type=document.ref_type,
                                                ref_id=int(document.ref_id)).first()
        if row is None:
            row = RetrievalIndex(ref_type=document.ref_type, ref_id=int(document.ref_id))
            s.add(row)
        content = (document.text or "")[:20000]
        if row.text != content:
            row.embedding = None
            row.embedding_model = None
        row.text = content
        row.title = document.title or ""
        row.channel = document.channel or ""
        row.channel_id = document.channel_id
        row.game = document.game
        row.version = document.version
        row.published_at = document.published_at
        row.deeplink = document.deeplink
        if document.embedding is not None:
            row.embedding = document.embedding
            row.embedding_model = document.embedding_model
        s.flush()
        snap = RetrievalIndex(ref_type=row.ref_type, ref_id=row.ref_id, text=row.text,
                              title=row.title, channel=row.channel, channel_id=row.channel_id, game=row.game, version=row.version,
                              published_at=row.published_at, deeplink=row.deeplink)
    _fts_sync(snap)


def index_pending(limit: int = 100) -> int:
    """Backfill messages inserted before retrieval was enabled."""
    if not settings.get("RETRIEVAL_ENABLED"):
        return 0
    with session_scope() as s:
        existing = s.query(RetrievalIndex.ref_id).filter(RetrievalIndex.ref_type == "message")
        ids = [x[0] for x in s.query(MonitorMessage.id)
               .filter(~MonitorMessage.id.in_(existing))
               .order_by(MonitorMessage.published_at.desc()).limit(limit).all()]
    for mid in ids:
        index_message(mid)
    return len(ids)


async def embed_pending(limit: int | None = None) -> int:
    """Backfill/replace vectors locally, outside transactions and the event loop."""
    if not settings.get("EMBEDDING_ENABLED"):
        return 0
    size = max(1, min(int(limit or settings.get("EMBEDDING_BATCH_SIZE") or 32), 128))
    with session_scope() as s:
        pending = [(r.id, r.text) for r in s.query(RetrievalIndex)
                   .filter(or_(RetrievalIndex.embedding.is_(None),
                               RetrievalIndex.embedding_model.is_(None),
                               RetrievalIndex.embedding_model != embeddings.MODEL_ID),
                           func.length(func.trim(RetrievalIndex.text)) > 0)
                   .order_by(RetrievalIndex.id).limit(size).all()]
    if not pending:
        return 0
    vectors = await asyncio.to_thread(embeddings.embed_documents, [text for _, text in pending])
    done = 0
    for (rid, content), vector in zip(pending, vectors, strict=True):
        packed = _pack(vector)
        if packed:
            with session_scope() as s:
                # Another process may edit or delete this document during inference.
                done += s.query(RetrievalIndex).filter_by(id=rid, text=content).update({
                    RetrievalIndex.embedding: packed,
                    RetrievalIndex.embedding_model: embeddings.MODEL_ID,
                }, synchronize_session=False)
    return done


def _similarity(query: np.ndarray | None, blob: bytes | None) -> float:
    vector = _unpack(blob)
    if query is None or vector is None or len(query) != len(vector):
        return 0.0
    den = float(np.linalg.norm(query) * np.linalg.norm(vector))
    return max(0.0, float(np.dot(query, vector) / den)) if den else 0.0


def remove(ref_type: str, ref_id: int) -> None:
    with session_scope() as s:
        s.query(RetrievalIndex).filter_by(ref_type=ref_type, ref_id=int(ref_id)).delete()
    with engine.begin() as conn:
        try:
            conn.execute(sql_text("DELETE FROM retrieval_fts WHERE ref_type=:t AND ref_id=:i"),
                         {"t": ref_type, "i": str(ref_id)})
        except Exception:
            pass


def _fts_ids(query: str, eligible_ids: list[int] | None = None) -> dict[tuple[str, int], float]:
    # FTS MATCH syntax is stricter than normal search.  Quoting each token avoids
    # punctuation (7.1, C++ and parentheses) turning into a SQL error.
    terms = re.findall(r"[\w\u4e00-\u9fff]+", query or "", re.UNICODE)
    if not terms:
        return {}
    match = " AND ".join(f'"{t.replace(chr(34), "")}"' for t in terms[:12])
    if eligible_ids == []:
        return {}
    try:
        with engine.connect() as conn:
            stmt = sql_text(
                "SELECT f.ref_type,f.ref_id,bm25(retrieval_fts) AS rank "
                "FROM retrieval_fts f JOIN retrieval_index i "
                "ON i.ref_type=f.ref_type AND CAST(i.ref_id AS TEXT)=f.ref_id "
                "WHERE retrieval_fts MATCH :q " +
                ("AND i.id IN :ids " if eligible_ids is not None else "") +
                "ORDER BY rank LIMIT 200")
            params = {"q": match}
            if eligible_ids is not None:
                stmt = stmt.bindparams(bindparam("ids", expanding=True))
                params["ids"] = eligible_ids
            rows = conn.execute(stmt, params).all()
        return {(str(t), int(i)): 1.0 / (1.0 + max(0.0, float(rank))) for t, i, rank in rows}
    except Exception:
        return {}


def search(query: str, *, game: str | None = None,
           channel_id: int | None = None, since: datetime | None = None,
           limit: int = 8, entity: str | None = None,
           ref_types: tuple[str, ...] = ("message", "wiki"),
           scopes: list[tuple[str, str]] | None = None) -> list[SearchHit]:
    """Search messages with FTS/LIKE and optional stored-vector fusion."""
    query = (query or "").strip()
    if not query:
        return []
    if not settings.get("RETRIEVAL_ENABLED"):
        return []
    if game:
        from .glossary import normalize_game
        game = normalize_game(game)
    limit = max(1, min(int(limit or 8), 100))
    like = f"%{query}%"
    qvec = _embedding_for_text(query)
    entity_map: dict[int, Any] = {}
    with session_scope() as s:
        from .message_query import query as message_query
        permitted_messages = message_query(s, game=game or "", since=since,
            channel_ids=[channel_id] if channel_id else None).with_entities(MonitorMessage.id)
        permitted_pages = (s.query(KnowledgePage.id).join(KnowledgeSource)
            .filter(KnowledgePage.enabled.is_(True), KnowledgeSource.enabled.is_(True),
                    KnowledgePage.document_status == "active"))
        allowed = [and_(RetrievalIndex.ref_type == "message", RetrievalIndex.ref_id.in_(permitted_messages)),
                   and_(RetrievalIndex.ref_type == "wiki", RetrievalIndex.ref_id.in_(permitted_pages))]
        if scopes and "memory" in ref_types:
            ids = s.query(Conversation.id).filter(tuple_(Conversation.scope_type, Conversation.scope_id).in_(scopes))
            allowed.append(and_(RetrievalIndex.ref_type == "memory", RetrievalIndex.ref_id.in_(ids)))
        q = s.query(RetrievalIndex).filter(RetrievalIndex.ref_type.in_(ref_types), or_(*allowed))
        if game:
            q = q.filter(RetrievalIndex.game == game)
        if channel_id:
            q = q.filter(RetrievalIndex.channel_id == channel_id)
        if since:
            q = q.filter(RetrievalIndex.published_at >= since)
        fts_scores = _fts_ids(query, [rid for rid, in q.with_entities(RetrievalIndex.id)])
        lexical = or_(RetrievalIndex.text.ilike(like), RetrievalIndex.title.ilike(like),
                      RetrievalIndex.channel.ilike(like))
        if fts_scores:
            lexical = or_(lexical, tuple_(RetrievalIndex.ref_type, RetrievalIndex.ref_id)
                          .in_(list(fts_scores)))
        rows = q.filter(lexical).order_by(RetrievalIndex.published_at.desc()).limit(500).all()
        if qvec is not None:
            # Scan only compact vectors, then fetch the best documents. Recent
            # unrelated rows must not crowd older exact/semantic matches out.
            vectors = q.with_entities(RetrievalIndex.id, RetrievalIndex.embedding).filter(
                RetrievalIndex.embedding.isnot(None),
                RetrievalIndex.embedding_model == embeddings.MODEL_ID).yield_per(256)
            best = heapq.nlargest(max(40, limit * 3),
                                 ((_similarity(qvec, blob), rid) for rid, blob in vectors))
            known = {row.id for row in rows}
            ids = [rid for score, rid in best if score >= 0.45 and rid not in known]
            if ids:
                rows.extend(q.filter(RetrievalIndex.id.in_(ids)).all())
        if entity:
            mids = [r.ref_id for r in rows if r.ref_type == "message"]
            entity_map = {m.id: m.entities for m in s.query(MonitorMessage).filter(MonitorMessage.id.in_(mids)).all()}
        if not rows and "message" in ref_types:
            # Existing installations (and messages inserted while the worker is
            # offline) may not have been indexed yet.  A bounded LIKE fallback
            # preserves search availability and also makes reindexing optional.
            mq = s.query(MonitorMessage, Channel).join(Channel, Channel.id == MonitorMessage.channel_id)
            mq = mq.filter(or_(MonitorMessage.text_raw.ilike(like),
                               MonitorMessage.text_zh.ilike(like), Channel.title.ilike(like)))
            mq = mq.filter(MonitorMessage.id.in_(permitted_messages))
            if channel_id:
                mq = mq.filter(MonitorMessage.channel_id == channel_id)
            if since:
                mq = mq.filter(MonitorMessage.published_at >= since)
            for m, ch in mq.order_by(MonitorMessage.published_at.desc()).limit(500).all():
                if entity:
                    from .kb.annotate import entity_names
                    if entity not in entity_names(m.entities):
                        continue
                rows.append(RetrievalIndex(ref_type="message", ref_id=m.id,
                                           text="\n".join(x for x in [m.text_zh, m.text_raw] if x),
                                           title=m.game_detected or ch.title or "",
                                           channel=ch.title or "", channel_id=ch.id, game=m.game_detected or ch.game,
                                           version=m.version_tag, published_at=m.published_at, deeplink=m.deeplink))
                entity_map[m.id] = m.entities
        elif len(rows) < 500 and "message" in ref_types:
            # Merge newly arrived, not-yet-indexed rows with a partially built
            # index instead of silently omitting them.
            known_ids = {r.ref_id for r in rows if r.ref_type == "message"}
            mq = s.query(MonitorMessage, Channel).join(Channel, Channel.id == MonitorMessage.channel_id)
            mq = mq.filter(or_(MonitorMessage.text_raw.ilike(like), MonitorMessage.text_zh.ilike(like),
                               Channel.title.ilike(like)))
            mq = mq.filter(MonitorMessage.id.in_(permitted_messages))
            if channel_id:
                mq = mq.filter(MonitorMessage.channel_id == channel_id)
            if since:
                mq = mq.filter(MonitorMessage.published_at >= since)
            for m, ch in mq.limit(500).all():
                if m.id in known_ids:
                    continue
                if entity:
                    from .kb.annotate import entity_names
                    if entity not in entity_names(m.entities):
                        continue
                rows.append(RetrievalIndex(ref_type="message", ref_id=m.id,
                                           text="\n".join(x for x in [m.text_zh, m.text_raw] if x),
                                           title=m.game_detected or ch.title or "", channel=ch.title or "",
                                           channel_id=ch.id, game=m.game_detected or ch.game,
                                           version=m.version_tag, published_at=m.published_at, deeplink=m.deeplink))
                entity_map[m.id] = m.entities
    # If FTS tokenisation could not match Chinese, LIKE rows still provide recall.
    hits: list[SearchHit] = []
    for row in rows:
        if entity and row.ref_type == "message":
            raw_entities = entity_map.get(row.ref_id)
            from .kb.annotate import entity_names
            if entity not in entity_names(raw_entities):
                continue
        lexical = fts_scores.get((row.ref_type, row.ref_id), 0.0)
        if lexical <= 0:
            if query.lower() in (row.text or "").lower():
                lexical = 0.35
            elif any(query.lower() in (value or "").lower() for value in (row.title, row.channel)):
                lexical = 0.25
        semantic = (_similarity(qvec, row.embedding)
                    if row.embedding_model == embeddings.MODEL_ID else 0.0)
        if lexical <= 0 and semantic < 0.45:
            continue
        score = 0.7 * lexical + 0.3 * semantic if semantic else lexical
        hits.append(SearchHit(row.ref_type, row.ref_id, float(score), row.text or "",
                              row.title or "", row.channel or "", row.game,
                              row.published_at, row.deeplink, row.version))
    hits.sort(key=lambda x: (x.score, x.published_at or datetime.min), reverse=True)
    return hits[:limit]


def related(message_id: int, limit: int = 6) -> list[SearchHit]:
    with session_scope() as s:
        row = s.query(RetrievalIndex).filter_by(ref_type="message", ref_id=int(message_id)).first()
        if row is None:
            pair = s.query(MonitorMessage, Channel).join(Channel, Channel.id == MonitorMessage.channel_id).filter(MonitorMessage.id == int(message_id)).first()
            if pair:
                m, ch = pair
                row = RetrievalIndex(ref_type="message", ref_id=m.id,
                                     text="\n".join(x for x in [m.text_zh, m.text_raw] if x),
                                     title=m.game_detected or ch.title or "",
                                     channel=ch.title or "", channel_id=ch.id, game=m.game_detected or ch.game,
                                     version=m.version_tag, published_at=m.published_at, deeplink=m.deeplink)
    if row is None:
        return []
    # Prefer same-game lexical terms; exclude the source itself.
    words = re.findall(r"[\w\u4e00-\u9fff]{2,}", row.text or "")
    query = " ".join(words[:4]) or row.title
    out = [h for h in search(query, game=row.game, limit=max(2, limit + 1))
           if h.ref_type == "message" and h.ref_id != int(message_id)]
    return out[:limit]


def build_context(hits: list[SearchHit], max_chars: int = 6000) -> str:
    lines: list[str] = []
    used = 0
    for h in hits:
        ref_label = "消息" if h.ref_type == "message" else ("Wiki" if h.ref_type == "wiki" else h.ref_type)
        line = f"【{ref_label}#{h.ref_id}】{h.title or h.game or h.channel} | 版本 {h.version or '-'} | {h.text.strip()}"
        if h.deeplink:
            line += f" ({h.deeplink})"
        if used + len(line) + 1 > max_chars:
            if not lines and max_chars >= 100:
                lines.append(line[:max_chars])
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)
