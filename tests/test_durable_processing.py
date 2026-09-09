from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from tgmon import jobs, memory, member_profile, settings
from tgmon.conversation_scope import activate
from tgmon.db import engine, session_scope
from tgmon.models import Base, Channel, ProcessingJob, SourceCursor, SourceEvent


@pytest.fixture(autouse=True)
def state():
    Base.metadata.create_all(engine)
    settings.set_many({"MEMORY_ENABLED": True})
    with session_scope() as s:
        s.query(ProcessingJob).delete()
        s.add(Channel(id=889001, tg_id="-100889001", title="Durable source", enabled=True))
    yield
    with session_scope() as s:
        s.query(ProcessingJob).delete()
        s.query(SourceEvent).filter_by(channel_id=889001).delete()
        s.query(SourceCursor).filter_by(channel_id=889001).delete()
        s.query(Channel).filter_by(id=889001).delete()


def test_job_atomic_claim_and_lease_recovery():
    jid = jobs.enqueue("test", "one-job", {"value": 1})
    assert jobs.enqueue("test", "one-job", {"value": 2}) == jid
    with ThreadPoolExecutor(max_workers=4) as executor:
        claims = list(executor.map(lambda _: jobs.claim("test"), range(4)))
    assert sum(c is not None for c in claims) == 1
    old = next(c for c in claims if c)
    with session_scope() as s:
        s.get(ProcessingJob, jid).lease_until = datetime.utcnow() - timedelta(seconds=1)
    replacement = jobs.claim("test")
    assert replacement and replacement["attempts"] == 2
    assert jobs.finish(old) is False
    assert jobs.finish(replacement) is True
    assert jobs.claim("test") is None


def test_telegram_raw_events_survive_replay():
    from telethon.tl.types import Message, PeerChannel
    message = Message(id=12, peer_id=PeerChannel(889001), message="Persist this",
                      date=datetime.utcnow())
    first = jobs.save_events(889001, [message])
    assert jobs.save_events(889001, [message]) == first
    with session_scope() as s:
        event = s.get(SourceEvent, first[0])
        assert jobs.decode_event(event).message == "Persist this"
        assert s.query(ProcessingJob).count() == 1
    assert jobs.claim("ingest")["payload"]["event_id"] == first[0]


@pytest.mark.asyncio
async def test_pagination_continues_even_when_latest_message_is_already_present():
    from tgmon.worker.tasks import _iter_and_ingest

    class Client:
        async def get_entity(self, entity):
            return entity

        async def iter_messages(self, entity, **kw):
            offset = kw.get("offset_id", 100)
            for mid in [5, 4, 3, 2, 1]:
                if mid < offset and kw["limit"]:
                    kw["limit"] -= 1
                    yield SimpleNamespace(id=mid, message=str(mid), date=datetime.utcnow(),
                                          entities=[], grouped_id=777 if mid in (3, 4) else None)

    first = await _iter_and_ingest(Client(), 889001, -100889001, limit=2,
                                   persistent_cursor=True)
    second = await _iter_and_ingest(Client(), 889001, -100889001, limit=2,
                                    persistent_cursor=True)
    assert first["offset_id"] == 4 and second["offset_id"] == 2
    with session_scope() as s:
        assert {r.source_id for r in s.query(SourceEvent)} == {"5", "4", "3", "2"}


def test_profiles_and_history_are_scoped_by_bot_group_and_private_chat():
    with activate({"id": 1}, "group-a", "person", "event"):
        member_profile.add_fact("person", "我喜欢摄影")
        memory.record_turn("group-a", "person", "user", "上次的照片", event_id="event")
        assert "摄影" in memory.build_chat_context("group-a", "person")
        assert memory.chat_messages("group-a", "person")[0]["role"] == "user"
    for bot, group, member in [({"id": 2}, "group-a", "person"),
                              ({"id": 1}, "group-b", "person"),
                              ({"id": 1}, "", "person"),
                              ({"id": 1}, "group-a", "other")]:
        with activate(bot, group, member):
            assert "摄影" not in memory.build_chat_context(group, member)
    with activate({"id": 1}, "group-a", "person", "forget"):
        member_profile.forget_fact("person", "摄影")
        assert "摄影" not in memory.build_chat_context("group-a", "person")


@pytest.mark.asyncio
async def test_identical_translation_calls_are_coalesced(monkeypatch):
    from tgmon import translate
    entered = 0

    async def implementation(*args):
        nonlocal entered
        entered += 1
        await asyncio.sleep(0.01)
        return translate.TranslateResult(text_zh="译文 100", status="ok")

    monkeypatch.setattr(translate, "_translate_impl", implementation)
    a, b = await asyncio.gather(translate.translate("source 100", "prompt"),
                                translate.translate("source 100", "prompt"))
    assert entered == 1 and a.text_zh == b.text_zh
    await translate.translate("source 101", "prompt")
    assert entered == 2
