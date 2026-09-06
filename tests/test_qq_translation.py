"""QQ translations use reviewed KB terms and the existing translation budget."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from tgmon import glossary, settings, translate
from tgmon.db import engine, session_scope
from tgmon.models import Base, Channel, GlossaryAlias, GlossaryEntry, MonitorMessage
from tgmon.qqbot import agent, client, commands


@pytest.fixture(autouse=True)
def kb(monkeypatch):
    Base.metadata.create_all(engine)
    conf = {"QQ_AI_ENABLED": True, "TRANSLATE_ENABLED": True,
            "TRANSLATE_CACHE_ENABLED": False, "AI_DAILY_CALL_LIMIT": 0,
            "AI_DAILY_COST_LIMIT": 0, "GLOSSARY_CHECK_ENABLED": True}
    previous = {key: settings.get(key) for key in conf}
    settings.set_many(conf)
    with session_scope() as s:
        for eid, status, source, target in (
                (995001, "active", "KafkaTest", "卡芙卡测试角色"),
                (995002, "pending", "SecretPrototype", "待审译名不可使用")):
            s.add(GlossaryEntry(id=eid, game="崩坏:星穹铁道", category="character",
                               canonical_zh=target, status=status,
                               attrs={"rarity": 5, "path": "Warlock"}))
            s.add(GlossaryAlias(entry_id=eid, surface=source, lang="en", enabled=True))
    glossary.invalidate_cache()
    cfg = SimpleNamespace(name="test-provider", model="test-model", price_in=0., price_out=0.)
    monkeypatch.setattr(translate, "load_configs", lambda **kwargs: [cfg])
    yield
    with session_scope() as s:
        s.query(GlossaryAlias).filter(GlossaryAlias.entry_id.in_([995001, 995002])).delete()
        s.query(GlossaryEntry).filter(GlossaryEntry.id.in_([995001, 995002])).delete()
        s.query(MonitorMessage).filter(MonitorMessage.id == 995001).delete()
        s.query(Channel).filter(Channel.id == 995001).delete()
    glossary.invalidate_cache()
    settings.set_many(previous)


@pytest.fixture
def provider(monkeypatch):
    calls = []

    async def complete(system, user, **kwargs):
        calls.append((system, user))
        return SimpleNamespace(ok=True, text="「卡芙卡测试角色」速度为 120。SecretPrototype",
                               provider_name="test-provider", model="test-model",
                               tokens_in=10, tokens_out=10, error=None)

    monkeypatch.setattr(translate, "complete_with_failover", complete)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["/translate [崩铁] ", "/tr ", "翻译："])
async def test_translation_dispatch_uses_active_terms_only(prefix, provider):
    replies = await commands._dispatch(prefix + "KafkaTest has 120 SPD. SecretPrototype",
                                       "group", True, "member")
    assert provider, commands.replies_text(replies)
    prompt, source = provider[0]
    assert "KafkaTest → 卡芙卡测试角色" in prompt
    assert "待审译名不可使用" not in prompt
    assert "5★" in prompt and "虚无" in prompt
    assert "120" in source and "SecretPrototype" in source
    assert "卡芙卡测试角色" in commands.replies_text(replies)


@pytest.mark.asyncio
async def test_translation_from_message_uses_original_text(provider):
    with session_scope() as s:
        s.add(Channel(id=995001, title="test", tg_id="qq-translate-test", game="崩铁"))
        s.add(MonitorMessage(id=995001, channel_id=995001, tg_message_id="1",
                             text_raw="KafkaTest new kit", text_zh="不该再翻译这份旧译文",
                             published_at=datetime.utcnow()))
    replies = await commands._dispatch("/translate #995001", "group", True, "member")
    assert provider and provider[0][1] == "KafkaTest new kit"
    assert "卡芙卡测试角色" in commands.replies_text(replies)


def test_verified_beta_terms_seed_is_idempotent():
    """The concrete HSR beta names are seeded without duplicating corrections."""
    from tgmon.bootstrap import _seed_verified_terms

    assert _seed_verified_terms() == 2
    assert _seed_verified_terms() == 0
    with session_scope() as s:
        rows = (s.query(GlossaryEntry, GlossaryAlias)
                .join(GlossaryAlias, GlossaryAlias.entry_id == GlossaryEntry.id)
                .filter(GlossaryEntry.origin == "verified:hsr-beta")
                .all())
        assert {(alias.surface, entry.canonical_zh) for entry, alias in rows} == {
            ("Skott", "斯科特"), ("Rakshmi", "拉克什米")}
        ids = {entry.id for entry, _ in rows}
        s.query(GlossaryAlias).filter(GlossaryAlias.entry_id.in_(ids)).delete(
            synchronize_session=False)
        s.query(GlossaryEntry).filter(GlossaryEntry.id.in_(ids)).delete(
            synchronize_session=False)
    glossary.invalidate_cache()


@pytest.mark.asyncio
async def test_translation_is_available_in_private_chat(provider):
    replies = await commands._dispatch("/translate KafkaTest", "", False, "user")
    assert provider and "卡芙卡测试角色" in commands.replies_text(replies)


@pytest.mark.asyncio
async def test_agent_translation_uses_same_kb_pipeline(provider):
    replies = await agent._run_action("translate", {"text": "KafkaTest", "game": "崩铁"},
                                      "group", True, "member")
    assert replies is not None
    assert provider and "KafkaTest → 卡芙卡测试角色" in provider[0][0]


@pytest.mark.asyncio
async def test_translation_obeys_budget(monkeypatch, provider):
    monkeypatch.setattr(translate, "budget_blocked", lambda: "今日调用已达上限 10")
    replies = await commands._dispatch("/translate KafkaTest", "group", True)
    assert not provider
    assert "上限" in commands.replies_text(replies)


@pytest.mark.asyncio
async def test_translation_disabled_does_not_call_provider(provider):
    settings.set_many({"QQ_AI_ENABLED": False})
    replies = await commands._dispatch("/translate KafkaTest", "group", True)
    assert not provider
    assert "关闭" in commands.replies_text(replies)


@pytest.mark.asyncio
async def test_translation_long_reply_is_split_without_losing_text(monkeypatch):
    expected = "翻译后的段落。" * 470

    async def complete(system, user, **kwargs):
        return SimpleNamespace(ok=True, text=expected, provider_name="test-provider",
                               model="test-model", tokens_in=10, tokens_out=10)

    monkeypatch.setattr(translate, "complete_with_failover", complete)
    replies = await commands._dispatch("/translate some text", "group", True)
    assert "".join(r.text for r in replies) == expected
    assert len(replies) <= 5 and all(len(r.text) <= commands.MAX_TEXT for r in replies)


@pytest.mark.asyncio
async def test_translation_repairs_approved_term_when_provider_keeps_english(monkeypatch):
    async def complete(system, user, **kwargs):
        return SimpleNamespace(ok=True, text="KafkaTest has 120 HP.", provider_name="test-provider",
                               model="test-model", tokens_in=10, tokens_out=10)

    monkeypatch.setattr(translate, "complete_with_failover", complete)
    replies = await commands._dispatch("/translate KafkaTest has 120 HP", "group", True)
    text = commands.replies_text(replies)
    assert "卡芙卡测试角色" in text
    assert "KafkaTest" not in text


@pytest.mark.asyncio
async def test_translation_excludes_other_game_entity_context(provider):
    await translate.translate("KafkaTest", "Translate", game="崩铁", entities_data={
        "entities": [
            {"name": "卡芙卡测试角色", "game": "崩坏:星穹铁道", "category": "character"},
            {"name": "其他游戏不应注入", "game": "原神", "category": "character"},
        ]})
    assert "卡芙卡测试角色" in provider[0][0]
    assert "其他游戏不应注入" not in provider[0][0]


@pytest.mark.asyncio
async def test_cached_translation_repairs_terms_without_calling_provider(monkeypatch, provider):
    monkeypatch.setattr(translate, "_cache_get", lambda key: ("KafkaTest 120", "cached", "test-model"))
    result = await translate.translate("KafkaTest 120", "Translate", game="崩铁", use_cache=True)
    assert not provider and result.from_cache
    assert result.text_zh == "卡芙卡测试角色 120"
    assert result.glossary_corrected == ["KafkaTest"]


@pytest.mark.asyncio
async def test_c2c_reply_preserves_message_sequence(monkeypatch):
    posted = []

    async def post(path, body):
        posted.append((path, body))
        return "sent"

    monkeypatch.setattr(client, "_post_message", post)
    await client.send_c2c_text("user", "翻译结果", msg_id="source", msg_seq=2)
    assert posted == [("/v2/users/user/messages", {
        "msg_type": 0, "content": "翻译结果", "msg_id": "source", "msg_seq": 2})]
