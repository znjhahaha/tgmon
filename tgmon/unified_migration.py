"""Idempotent upgrade with an online SQLite backup and archived legacy scopes."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sqlite3

from sqlalchemy import text

from . import settings
from .db import engine, session_scope


def backup_before_upgrade():
    path = Path(engine.url.database or "")
    if not path.is_file():
        return
    with sqlite3.connect(str(path)) as source:
        exists = source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migration'").fetchone()
        if not exists or not source.execute("SELECT 1 FROM schema_migration WHERE version=2").fetchone():
            target = path.parent / "backups" / "before-general-v2.sqlite3"
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                with sqlite3.connect(str(target)) as destination:
                    source.backup(destination)
        columns = {r[1] for r in source.execute("PRAGMA table_info(conversation_turn)")}
        if not columns or "event_key" in columns:
            return
        target = path.parent / "backups" / "before-unified-v1.sqlite3"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            with sqlite3.connect(str(target)) as destination:
                source.backup(destination)


def migrate():
    with engine.begin() as conn:
        for table, column in (("conversation_turn", "event_key"), ("qq_delivery", "dedup_key")):
            conn.execute(text(f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{table}_{column} ON {table}({column}) WHERE {column} IS NOT NULL"))
        for operation in ("UPDATE", "DELETE"):
            conn.execute(text(f"CREATE TRIGGER IF NOT EXISTS unified_page_{operation.lower()} AFTER {operation} ON knowledge_page BEGIN DELETE FROM retrieval_index WHERE ref_type='wiki' AND ref_id=OLD.id; END"))
        conn.execute(text("CREATE TRIGGER IF NOT EXISTS unified_source_update AFTER UPDATE OF enabled,trusted ON knowledge_source BEGIN DELETE FROM retrieval_index WHERE ref_type='wiki' AND ref_id IN (SELECT id FROM knowledge_page WHERE source_id=NEW.id); END"))
        conn.execute(text("CREATE TRIGGER IF NOT EXISTS unified_source_delete AFTER DELETE ON knowledge_source BEGIN DELETE FROM retrieval_index WHERE ref_type='wiki' AND ref_id IN (SELECT id FROM knowledge_page WHERE source_id=OLD.id); END"))
        conn.execute(text("CREATE TRIGGER IF NOT EXISTS unified_memory_update AFTER UPDATE OF facts,summary,revision ON conversation BEGIN DELETE FROM retrieval_index WHERE ref_type='memory' AND ref_id=NEW.id; END"))
    if settings.get("UNIFIED_SCHEMA_VERSION") == "1":
        return
    from .models import Channel, KnowledgeSource, QqInbound, QqEvent, QqGroup, MonitorMessage, Task, ConversationTurn
    from .qqbot.events import event_key
    from .memory import scope_keys, _get
    from .kb.service import TRUSTED_HOSTS
    from urllib.parse import urlparse
    import hashlib
    with session_scope() as s:
        for source in s.query(KnowledgeSource):
            if urlparse(source.url).hostname in TRUSTED_HOSTS:
                source.trusted = True
        seen = set()
        for inbound in s.query(QqInbound).order_by(QqInbound.id):
            if not inbound.msg_id or not inbound.member_openid:
                continue
            if (inbound.content or "").startswith(("/remember", "/forget", "记住", "忘记", "修改记忆", "我的记忆")):
                continue
            private = inbound.event_type == "C2C_MESSAGE_CREATE"
            if not private and (inbound.event_type != "GROUP_AT_MESSAGE_CREATE" or not inbound.group_openid):
                continue
            group = "" if private else inbound.group_openid
            key = event_key("c2c" if private else "group", inbound.member_openid if private else group, inbound.msg_id)
            if key in seen or s.get(QqEvent, key):
                continue
            seen.add(key)
            s.add(QqEvent(event_key=key, status="legacy", result={}, updated_at=inbound.created_at))
            scope = scope_keys(group, inbound.member_openid)[0]
            conv = _get(s, scope, True, group, inbound.member_openid if private else "")
            # Legacy inbound.reply only proves generation, not QQ delivery.
            # Preserve the source utterance without inventing a completed turn.
            for role, value in (("user", inbound.content),):
                if value:
                    turn_key = hashlib.sha256(f"{scope}\0{inbound.msg_id}\0{role}\0{0}".encode()).hexdigest()
                    if s.query(ConversationTurn.id).filter_by(event_key=turn_key).first():
                        continue
                    s.add(ConversationTurn(conversation_id=conv.id, actor_id=inbound.member_openid,
                        role=role, content=value, event_key=turn_key, created_at=inbound.created_at))
        for group in s.query(QqGroup).filter(QqGroup.last_seen_msg_id.isnot(None)):
            last = s.get(MonitorMessage, group.last_seen_msg_id)
            if last and not group.cursors:
                from .message_query import effective_game
                games = [game or "" for game, in s.query(effective_game()).select_from(MonitorMessage).join(Channel).distinct()]
                group.cursors = {game: {"published_at": last.published_at.isoformat(), "id": last.id} for game in games}
        s.add(Task(kind="unified_backfill", payload={"after": 0}, status="pending"))
        s.add(Task(kind="knowledge_index", payload={"after": 0}, status="pending"))
    settings.set_many({"UNIFIED_SCHEMA_VERSION": "1", "QQ_LATEST_DEFAULT_N": 3})
