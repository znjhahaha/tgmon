from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tgmon import memory, settings
from tgmon.db import engine, session_scope
from tgmon.glossary import normalize_game
from tgmon.models import Base, Channel, Conversation, ConversationTurn, MonitorMessage
from tgmon.qqbot import agent, commands


@pytest.fixture
def state(monkeypatch):
    Base.metadata.create_all(engine)
    settings.set_many({"MEMORY_ENABLED": True, "MEMORY_RECENT_TURNS": 8,
                       "BASE_URL": "https://example.com"})
    game = normalize_game("HSR")
    with session_scope() as s:
        from tgmon.models import MessageMedia
        s.query(MessageMedia).delete()
        s.query(MonitorMessage).delete()
        s.query(Channel).delete()
        s.add(Channel(id=99001, tg_id="-10099001", title="Unified test", game=game))
        s.add(MonitorMessage(id=99001, channel_id=99001, tg_message_id="1",
                            text_raw="same-news", game_detected=game,
                            published_at=datetime.utcnow() - timedelta(seconds=1)))
        s.add(MonitorMessage(id=99002, channel_id=99001, tg_message_id="2",
                            text_raw="same-news", game_detected=game, duplicate_of=99001,
                            published_at=datetime.utcnow()))
    yield
    with session_scope() as s:
        from tgmon import models
        for name in ("QqPending", "QqEvent", "QqDelivery", "QqGroup", "ShareToken", "AppSetting"):
            model = getattr(models, name, None)
            if model is not None:
                s.query(model).delete()
        s.query(ConversationTurn).delete()
        s.query(Conversation).delete()
        s.query(MonitorMessage).filter(MonitorMessage.channel_id == 99001).delete()
        s.query(Channel).filter(Channel.id == 99001).delete()
    settings.invalidate()


@pytest.mark.parametrize("game", ["HSR", "崩铁", "星铁", "崩坏：星穹铁道"])
def test_latest_aliases_return_one_distinct_story(state, game):
    assert [r["id"] for r in commands._query_latest(3, game)] == [99001]


def test_one_turn_is_not_repeated_or_shared_across_groups(state):
    memory.record_turn("uni-g", "uni-a", "user", "context-marker")
    own = memory.load_context("uni-g", "uni-a")["recent"]
    assert [x["content"] for x in own] == ["context-marker"]
    assert memory.load_context("uni-other", "uni-a")["recent"] == []
    assert memory.load_context("uni-g", "uni-b")["recent"][0]["content"] == "context-marker"


@pytest.mark.asyncio
async def test_router_receives_real_recent_context(state, monkeypatch):
    from tgmon.providers import registry
    memory.record_turn("uni-g", "uni-a", "user", "context-marker")
    complete = AsyncMock(return_value=SimpleNamespace(ok=True, text='{"action":"chat","reply":"ok"}'))
    monkeypatch.setattr(registry, "complete_with_failover", complete)
    await agent.handle("continue", "uni-g", True, "uni-a")
    assert "context-marker" in str(complete.call_args)


@pytest.mark.asyncio
async def test_ask_receives_real_recent_context(state, monkeypatch):
    from tgmon import retrieval
    from tgmon.providers import registry
    memory.record_turn("uni-g", "uni-a", "user", "context-marker")
    hit = retrieval.SearchHit("message", 99001, 1., "evidence", "title", "channel")
    monkeypatch.setattr(retrieval, "search", lambda *args, **kwargs: [hit])
    complete = AsyncMock(return_value=SimpleNamespace(ok=True, text="answer", provider_name="test", tokens_in=0, tokens_out=0))
    monkeypatch.setattr(registry, "complete_with_failover", complete)
    await commands._cmd_ask("followup", "uni-g", "uni-a")
    assert "context-marker" in str(complete.call_args)


def test_private_memory_is_not_public_search_evidence(state, monkeypatch):
    from tgmon import retrieval
    monkeypatch.setattr(retrieval, "_embedding_for_text", lambda text: None)
    memory.record_turn("", "uni-a", "user", "private-marker")
    cid = memory.load_context("", "uni-a")["conversation_id"]
    retrieval.index_document(retrieval.RetrievalDocument("memory", cid, "private-marker"))
    assert retrieval.search("private-marker") == []


def test_disabled_wiki_source_is_not_search_evidence(state, monkeypatch):
    from tgmon import retrieval
    from tgmon.models import KnowledgePage, KnowledgeSource
    monkeypatch.setattr(retrieval, "_embedding_for_text", lambda text: None)
    with session_scope() as s:
        source = KnowledgeSource(game="绝区零", url="https://example.org/api.php", enabled=False)
        s.add(source)
        s.flush()
        page = KnowledgePage(source_id=source.id, wiki_page_id="test", title="Test",
                             content="disabled-marker", status="active")
        s.add(page)
        s.flush()
        pid = page.id
    retrieval.index_document(retrieval.RetrievalDocument("wiki", pid, "disabled-marker"))
    assert retrieval.search("disabled-marker") == []


def test_personal_facts_are_scoped_to_group_and_forget(state):
    memory.remember("uni-g", "uni-a", "group-one")
    memory.remember("uni-other", "uni-a", "group-two")
    assert "group-one" not in memory.load_context("uni-other", "uni-a")["facts"]
    memory.forget("user", "uni-a", group="uni-g")
    assert "group-two" in memory.load_context("uni-other", "uni-a")["facts"]


def test_repeated_event_does_not_duplicate_memory(state):
    for _ in range(2):
        memory.record_turn("uni-g", "uni-a", "user", "once", event_id="event-1")
    assert len(memory.load_context("uni-g", "uni-a")["recent"]) == 1


def test_unique_npc_identifies_unmarked_game(state):
    from tgmon import classify, glossary
    term = glossary.Term(1, 1, "Skott", "斯科特", game=normalize_game("HSR"), category="npc")
    text = '"Lyndon EmbodiMech" Workshop Skott\'s mechatron workshop thataims to rebuild bodies for ghostseverywhere'
    assert classify.detect(text, hits=[term]).game == normalize_game("HSR")


def test_translation_policy_covers_unknown_names():
    from tgmon.prompts import DEFAULT_GLOBAL_PROMPT, game_context
    assert "暂译" in DEFAULT_GLOBAL_PROMPT
    assert "暂译" in game_context(None)


@pytest.mark.asyncio
async def test_latest_returns_a_snapshot_card_and_link(state):
    from tgmon.models import ShareToken
    replies = await commands._dispatch("/latest 3", "uni-g", True, "uni-a")
    assert replies[0].kind == "image"
    assert replies[0].text.startswith("https://example.com/s/")
    with session_scope() as s:
        share = s.query(ShareToken).filter_by(created_by="qqbot").one()
        assert [x["id"] for x in share.snapshot_items] == [99001]


@pytest.mark.asyncio
async def test_duplicate_callbacks_only_dispatch_once(state, monkeypatch):
    dispatch = AsyncMock(return_value=[commands.Reply(text="once")])
    monkeypatch.setattr(commands, "_dispatch", dispatch)
    event = {"id": "uni-event-1", "group_openid": "uni-g", "author": {"member_openid": "uni-a"}, "content": "hello"}
    await commands.handle_group_message(event)
    await commands.handle_group_message(event)
    assert dispatch.await_count == 1


@pytest.mark.asyncio
async def test_proactive_messages_are_queued_for_a_batch(state, monkeypatch):
    from tgmon import qqbot
    from tgmon.models import QqGroup
    from tgmon.qqbot import client
    settings.set_many({"QQ_ENABLED": True, "QQ_CHANNEL_IDS": [], "QQ_DAILY_LIMIT": 100})
    with session_scope() as s:
        s.add(QqGroup(group_openid="uni-g", enabled=True))
    send = AsyncMock(return_value="sent")
    monkeypatch.setattr(client, "send_group_text", send)
    await qqbot.push_message(99001)
    assert send.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("utterance", [
    "/latest 5", "/game 崩铁 5", "/game Star Rail 5", "最近的五条爆料", "给我发最近五条爆料",
    "请发给我 HSR 最新 5 条爆料", "崩坏：星穹铁道最近五条", "最新５条爆料",
    "帮我看看星铁最新五条消息", "来五条爆料", "最近五条爆料文字版",
])
async def test_five_requested_stories_survive_routing_and_card_packaging(state, monkeypatch, utterance):
    with session_scope() as s:
        s.add_all([MonitorMessage(id=99100 + i, channel_id=99001,
            tg_message_id=str(100 + i), text_raw=f"unique story {i}",
            game_detected=normalize_game("HSR"), published_at=datetime.utcnow() + timedelta(seconds=i))
            for i in range(6)])
    route = AsyncMock(side_effect=AssertionError("Simple latest requests need no AI routing"))
    monkeypatch.setattr(agent, "handle", route)
    replies = await commands._dispatch(utterance, "uni-g", True, "uni-a")
    ids = {mid for reply in replies for mid in reply.story_ids}
    assert ids == set(range(99101, 99106))
    assert len(replies) <= commands.MAX_REPLIES
    if "文字版" not in utterance:
        from tgmon.models import ShareToken
        with session_scope() as s:
            assert len(s.get(ShareToken, replies[0].bundle_id).snapshot_items) == 5
    route.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    "翻译：HSR latest leaks", "/translate [崩铁] Latest news", "/search 原神爆料",
    "我最近心情不好", "原神爆料为什么有冲突", "崩铁最新技能有什么变化",
])
async def test_specific_questions_and_commands_do_not_become_latest(state, monkeypatch, content):
    fallback = AsyncMock(return_value=[commands.Reply(text="chosen route")])
    monkeypatch.setattr(commands, "_dispatch_raw", fallback)
    replies = await commands._dispatch(content, "uni-g", True, "uni-a")
    assert replies[0].text == "chosen route"
    fallback.assert_awaited_once_with(content, "uni-g", True, "uni-a")


@pytest.mark.asyncio
async def test_continue_keeps_five_and_text_mode_keeps_the_same_results(state):
    with session_scope() as s:
        s.add_all([MonitorMessage(id=99200 + i, channel_id=99001,
            tg_message_id=str(200 + i), text_raw=f"continuation story {i}",
            game_detected=normalize_game("HSR"), published_at=datetime.utcnow() + timedelta(seconds=i))
            for i in range(12)])
    first = await commands._dispatch("HSR最近五条爆料", "uni-g", True, "uni-a")
    second = await commands._dispatch("继续", "uni-g", True, "uni-a")
    ids = lambda replies: {mid for reply in replies for mid in reply.story_ids}
    assert ids(first) == set(range(99207, 99212))
    assert ids(second) == set(range(99202, 99207))
    text = await commands._dispatch("改成文字版", "uni-g", True, "uni-a")
    assert len(text) == 5
    assert all(reply.kind == "text" for reply in text)
    assert all(any(f"#{mid}" in reply.text for reply in text) for mid in ids(second))


@pytest.mark.asyncio
@pytest.mark.parametrize("model_count", ["5", "五", 3])
async def test_agent_preserves_explicit_count_even_when_model_defaults_to_three(state, monkeypatch, model_count):
    import json
    from tgmon.providers import registry
    complete = AsyncMock(return_value=SimpleNamespace(ok=True, text=json.dumps({
        "action": "game", "args": {"name": "Star Rail", "n": model_count}})))
    monkeypatch.setattr(registry, "complete_with_failover", complete)
    calls = []
    monkeypatch.setattr(commands, "_cmd_game", lambda value: calls.append(value) or [commands.Reply(text="ok")])
    await agent.handle("我想了解一下星铁最近的五条爆料", "uni-g", True, "uni-a")
    assert calls == ["/game Star Rail 5"]
    calls.clear()
    await agent._run_action("game", {"name": "Star Rail", "n": str(model_count)}, "uni-g", True, "uni-a")
    assert calls == [f"/game Star Rail {3 if model_count == 3 else 5}"]
