"""Bounded worker jobs for classification and knowledge publication."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from . import classify
from .db import session_scope
from .models import Channel, MonitorMessage, Task


async def reclassify(runner, payload):
    from .retrieval import index_message
    after = max(0, int(payload.get("after") or 0))
    with session_scope() as s:
        rows = s.query(MonitorMessage).filter(MonitorMessage.id > after).order_by(MonitorMessage.id).limit(50).all()
        work = []
        for row in rows:
            if (row.game_scores or {}).get("method") == "manual":
                continue
            channel = s.get(Channel, row.channel_id)
            work.append((row.id, "\n".join(x for x in (row.text_raw, row.text_zh) if x),
                         SimpleNamespace(game=channel.game, games=channel.games) if channel else None))
        last = rows[-1].id if rows else after
    changed = 0
    for mid, content, channel in work:
        result = await classify.resolve_game(content, channel)
        with session_scope() as s:
            row = s.get(MonitorMessage, mid)
            if row and (row.game_scores or {}).get("method") != "manual":
                row.game_detected = result.game
                row.game_scores = result.as_json()
                changed += 1
        await asyncio.to_thread(index_message, mid)
    if len(rows) == 50:
        with session_scope() as s:
            s.add(Task(kind="unified_backfill", payload={"after": last}, status="pending"))
    return {"done": changed, "after": last, "has_more": len(rows) == 50}


async def knowledge_index(runner, payload):
    from .kb.service import refresh_documents
    result = await asyncio.to_thread(refresh_documents, 100, int(payload.get("after") or 0))
    if result["done"] == 100:
        with session_scope() as s:
            s.add(Task(kind="knowledge_index", payload={"after": result["after"]}, status="pending"))
    return result
