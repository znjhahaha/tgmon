"""Regression tests for conversation, album dedupe, and share snapshots."""
from __future__ import annotations

from datetime import datetime

import pytest

from tgmon import memory, pipeline, settings, sharing
from tgmon.db import engine, session_scope
from tgmon.models import Base, Channel, MessageMedia, MonitorMessage, ShareToken

Base.metadata.create_all(engine)


@pytest.fixture(autouse=True)
def cleanup_rebuild_rows():
    yield
    from tgmon.models import Conversation, ConversationTurn, QqGroup, QqInbound, QqEvent
    with session_scope() as s:
        mids = [r[0] for r in s.query(MonitorMessage.id).join(Channel).filter(
            Channel.tg_id.like("rebuild%")).all()]
        if mids:
            s.query(ShareToken).filter(ShareToken.message_id.in_(mids)).delete(synchronize_session=False)
            s.query(MessageMedia).filter(MessageMedia.message_id.in_(mids)).delete(synchronize_session=False)
            s.query(MonitorMessage).filter(MonitorMessage.id.in_(mids)).delete(synchronize_session=False)
        s.query(Channel).filter(Channel.tg_id.like("rebuild%")).delete(synchronize_session=False)
        s.query(QqGroup).filter(QqGroup.group_openid.like("rebuild%")).delete(synchronize_session=False)
        s.query(QqInbound).filter(QqInbound.member_openid.like("rebuild%")).delete(synchronize_session=False)
        s.query(ConversationTurn).filter(ConversationTurn.actor_id.like("rebuild%")).delete(synchronize_session=False)
        s.query(Conversation).filter(Conversation.scope_id.like("rebuild%")).delete(synchronize_session=False)
        s.query(QqEvent).delete()


def test_profile_only_memory_is_available_without_group_turns():
    settings.set_many({"MEMORY_ENABLED": True})
    member = "rebuild-profile-only"
    memory.remember("rebuild-group", member, "喜欢流萤")
    context = memory.build_chat_context("rebuild-group", member)
    assert "【当前用户】" in context
    assert "喜欢流萤" in context


def test_image_dedupe_requires_the_complete_media_set():
    settings.set_many({"DEDUP_ENABLED": True, "DEDUP_WINDOW_DAYS": 7,
                       "PHASH_DISTANCE": 5})
    with session_scope() as s:
        ch = Channel(tg_id="rebuild", title="rebuild", enabled=True)
        s.add(ch)
        s.flush()
        msg = MonitorMessage(channel_id=ch.id, tg_message_id="rebuild-1",
                             text_raw="original", published_at=datetime.utcnow())
        s.add(msg)
        s.flush()
        s.add_all([
            MessageMedia(message_id=msg.id, kind="photo", thumb_path="rebuild/a.webp",
                         phash="a5a5a5a5a5a5a5a5", dhash="9696969696969696"),
        ])
    # One old image plus one genuinely new image is a new story, not a duplicate.
    assert pipeline._find_image_dup([
        ("a5a5a5a5a5a5a5a5", "9696969696969696"),
        ("5a5a5a5a5a5a5a5a", "6969696969696969"),
    ]) is None


def test_share_content_change_does_not_reuse_old_snapshot():
    settings.set_many({"BASE_URL": "https://example.test"})
    with session_scope() as s:
        ch = Channel(tg_id="rebuild-share", title="rebuild", enabled=True)
        s.add(ch)
        s.flush()
        msg = MonitorMessage(channel_id=ch.id, tg_message_id="rebuild-share-1",
                             text_raw="raw", text_zh="before", published_at=datetime.utcnow())
        s.add(msg)
        s.flush()
        s.add(MessageMedia(message_id=msg.id, kind="photo", thumb_path="rebuild/b.webp"))
        mid = msg.id
    first = sharing.create_bundle([mid])
    with session_scope() as s:
        s.get(MonitorMessage, mid).text_zh = "after"
        s.add(MessageMedia(message_id=mid, kind="photo", thumb_path="rebuild/c.webp"))
    second = sharing.create_bundle([mid])
    assert second != first
    with session_scope() as s:
        snapshot = s.get(ShareToken, second).snapshot_items[0]
        assert snapshot["text"] == "after"
        assert len(snapshot["photos"]) == 2


@pytest.mark.asyncio
async def test_assistant_turn_is_recorded_only_after_send_confirmation(monkeypatch):
    """A generated answer must not become history before QQ acknowledges it."""
    from tgmon import qqbot
    from tgmon.admin.routes import qqbot_cb
    from tgmon.models import ConversationTurn, QqEvent
    from tgmon.qqbot import client
    from tgmon.qqbot import commands

    settings.set_many({"MEMORY_ENABLED": True, "QQ_AI_ENABLED": True})
    event = {
        "type": "GROUP_AT_MESSAGE_CREATE",
        "d": {"id": "rebuild-confirmed-send", "group_openid": "rebuild-send-group",
              "content": "你好", "author": {"member_openid": "rebuild-send-member"}},
    }
    with session_scope() as s:
        s.query(QqEvent).delete()
        s.query(ConversationTurn).delete()

    async def fake_dispatch(*args, **kwargs):
        return [commands.Reply(text="已确认的回答")]

    monkeypatch.setattr(commands, "_dispatch", fake_dispatch)
    result = await qqbot.handle_callback_event(event)
    with session_scope() as s:
        assert not s.query(ConversationTurn).filter_by(role="assistant").first()

    async def send_text(*args, **kwargs):
        return "ROBOT1.0"

    monkeypatch.setattr(client, "send_group_text", send_text)
    await qqbot_cb._passive_reply(result, source="test")
    with session_scope() as s:
        assert s.query(ConversationTurn).filter_by(
            role="assistant", content="已确认的回答").first()
