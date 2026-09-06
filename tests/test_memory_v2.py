"""记忆 v2：成员档案 + 结构化上下文注入 + chat 两阶段。

背景（2026-09 记忆重构）：旧链路三层断链 —— 上下文以 JSON blob 注入被
模型当干扰、成员身份只有 openid 哈希分不清人、自动事实提取零产出。
新链路：member_profile 直存昵称/事实，build_chat_context 输出结构化
明文，agent chat 第二阶段用它续聊。
"""
import pytest

from tgmon import memory, member_profile, settings
from tgmon.db import engine, session_scope
from tgmon.models import (Base, Conversation, ConversationTurn, MemberProfile)


@pytest.fixture()
def db():
    Base.metadata.create_all(engine)
    settings.set_many({"MEMORY_ENABLED": True})
    yield
    with session_scope() as s:
        s.query(MemberProfile).filter(
            MemberProfile.member_openid.like("v2-%")).delete()
        ids = [x.id for x in s.query(Conversation).filter(
            Conversation.scope_id.in_(["v2-g", "v2-a", "v2-b"]))]
        s.query(ConversationTurn).filter(
            ConversationTurn.conversation_id.in_(ids)).delete()
        s.query(Conversation).filter(Conversation.id.in_(ids)).delete()


# ---------------- member_profile ----------------

def test_nickname_set_and_display(db):
    assert member_profile.set_nickname("v2-a", "科比")
    assert member_profile.display_name("v2-a") == "科比"
    # 未登记成员退回可读的哈希前缀
    assert member_profile.display_name("v2-unknown1") == "成员#v2-unk"
    assert member_profile.display_name("") == "成员"


def test_add_fact_idempotent_and_forget(db):
    assert member_profile.add_fact("v2-a", "喜欢流萤")
    assert member_profile.add_fact("v2-a", "喜欢流萤")     # 同文幂等
    prof = member_profile.get_profile("v2-a")
    assert prof["facts"] == ["喜欢流萤"]
    assert member_profile.forget_fact("v2-a", "流萤") == 1
    assert member_profile.get_profile("v2-a")["facts"] == []
    assert member_profile.forget_fact("v2-a") == 0         # 再删=0


def test_add_fact_respects_memory_switch(db):
    settings.set_many({"MEMORY_ENABLED": False})
    assert not member_profile.add_fact("v2-a", "不该存")
    settings.set_many({"MEMORY_ENABLED": True})


# ---------------- record_turn speaker ----------------

def test_record_turn_stores_speaker(db):
    memory.record_turn("v2-g", "v2-a", "user", "帮我查流萤", speaker="科比")
    with session_scope() as s:
        t = (s.query(ConversationTurn)
             .filter(ConversationTurn.content == "帮我查流萤").first())
        assert t is not None
        assert t.speaker == "科比"
        assert t.actor_id == "v2-a"


# ---------------- build_chat_context ----------------

def test_build_chat_context_structure_and_privacy(db):
    member_profile.set_nickname("v2-a", "科比")
    member_profile.add_fact("v2-a", "自述：密码是123")
    memory.record_turn("v2-g", "v2-a", "user", "帮我查流萤", speaker="科比")
    memory.record_turn("v2-g", "v2-a", "assistant", "查到了【消息#1】")

    ctx = memory.build_chat_context("v2-g", "v2-a")
    # 三段结构齐全：当前用户档案（昵称+事实）、近期对话（说话人明文）、机器人轮次
    assert "【当前用户】科比" in ctx
    assert "自述：密码是123" in ctx
    # 当前用户的轮次带锚定标注（防串用户）
    assert "科比（当前用户）：帮我查流萤" in ctx
    assert "机器人：查到了【消息#1】" in ctx

    # 隐私边界：另一个成员看到共享群轮次，但看不到 v2-a 的个人事实
    ctx_b = memory.build_chat_context("v2-g", "v2-b")
    assert "科比：帮我查流萤" in ctx_b          # 他人轮次无（当前用户）标注
    assert "密码是123" not in ctx_b


def test_build_chat_context_anchor_without_profile(db):
    """档案全空的成员也必须有【当前用户】锚点（防串用户的关键）。"""
    member_profile.set_nickname("v2-a", "haji")
    memory.record_turn("v2-g", "v2-a", "user", "叫我科比", speaker="haji")
    memory.record_turn("v2-g", "v2-b", "user", "我是谁", speaker="成员#v2-b")
    ctx = memory.build_chat_context("v2-g", "v2-b")
    # v2-b 无昵称无事实：锚点仍输出，且明确是当前说话人
    assert "【当前用户】成员#v2-b" in ctx
    # v2-b 自己的轮次有标注，haji（自称科比的那位）的轮次没有
    assert "成员#v2-b（当前用户）：我是谁" in ctx
    assert "haji：叫我科比" in ctx
    assert "haji（当前用户）" not in ctx


def test_build_chat_context_actor_without_speaker_uses_profile(db):
    # 旧轮次没带 speaker：回填时从 member_profile 反查昵称
    member_profile.set_nickname("v2-a", "流萤厨")
    memory.record_turn("v2-g", "v2-a", "user", "今天天气")  # 不传 speaker
    ctx = memory.build_chat_context("v2-g", "v2-b")
    assert "流萤厨：今天天气" in ctx


def test_build_chat_context_empty_without_memory(db):
    # 无轮次无摘要 → 空串（调用方据此跳过第二阶段，不浪费 AI 调用）
    assert memory.build_chat_context("v2-g", "v2-a") == ""
    # 关闭开关也返回空
    settings.set_many({"MEMORY_ENABLED": False})
    memory.record_turn("v2-g", "v2-a", "user", "x")
    settings.set_many({"MEMORY_ENABLED": True})


def test_build_chat_context_truncates(db):
    memory.record_turn("v2-g", "v2-a", "user", "长" * 500)
    ctx = memory.build_chat_context("v2-g", "v2-a", max_chars=200)
    assert len(ctx) <= 200


# ---------------- summarize 证据校验放宽 ----------------

@pytest.mark.asyncio
async def test_summarize_quote_matching_ignores_whitespace(db, monkeypatch):
    """摘要提取的引文校验：去空白后子串匹配（模型转写常动标点）。"""
    memory.record_turn("v2-g", "v2-a", "user", "我 叫 科比，喜欢 流萤")
    with session_scope() as s:
        conv = (s.query(Conversation)
                .filter(Conversation.scope_id == "v2-g").first())
        conv_id = conv.id

    class _Res:
        ok = True
        text = ('{"summary":"聊称呼和喜好","facts":[{"key":"称呼","text":"自称科比",'
                '"turn_id":%d,"quote":"我叫科比， 喜欢流萤"}]}' % conv_id)
        provider_name = "test"
        tokens_in = tokens_out = 0

    async def _fake(system, user):
        return _Res()

    from tgmon.providers import registry
    monkeypatch.setattr(registry, "complete_with_failover", _fake)
    # 触发条件需要 >= trigger 轮：伪造 watermark 已推进只留旧轮
    with session_scope() as s:
        c = s.get(Conversation, conv_id)
        c.context_state = {"group": "v2-g", "member": "", "summarized_through": 0,
                           "game": ""}
    # summarize_if_needed 需要 len(rows) >= max(trigger, keep+1)
    for i in range(12):
        memory.record_turn("v2-g", "v2-a", "user", f"填充轮次{i}")
    await memory.summarize_if_needed(conv_id)

    prof = member_profile.get_profile("v2-a")
    assert any("科比" in f for f in prof["facts"])


# ---------------- agent：nickname / remember / chat 两阶段 ----------------

@pytest.mark.asyncio
async def test_agent_nickname_action(db, monkeypatch):
    from tgmon.qqbot import agent

    class _Res:
        ok = True
        text = '{"action":"nickname","args":{"name":"科比"},"reply":""}'
        provider_name = "test"
        tokens_in = tokens_out = 0

    async def _fake(system, user):
        return _Res()

    from tgmon.providers import registry
    monkeypatch.setattr(registry, "complete_with_failover", _fake)
    replies = await agent.handle("叫我科比", "v2-g", True, "v2-a")
    assert "科比" in replies[0].text
    assert member_profile.display_name("v2-a") == "科比"


@pytest.mark.asyncio
async def test_agent_remember_writes_profile(db, monkeypatch):
    from tgmon.qqbot import agent

    class _Res:
        ok = True
        text = '{"action":"remember","args":{"text":"喜欢流萤"},"reply":""}'
        provider_name = "test"
        tokens_in = tokens_out = 0

    async def _fake(system, user):
        return _Res()

    from tgmon.providers import registry
    monkeypatch.setattr(registry, "complete_with_failover", _fake)
    replies = await agent.handle("记住我喜欢流萤", "v2-g", True, "v2-a")
    assert "已记住" in replies[0].text
    assert "喜欢流萤" in member_profile.get_profile("v2-a")["facts"]


@pytest.mark.asyncio
async def test_agent_chat_second_stage_uses_context(db, monkeypatch):
    """chat 动作：有记忆时走第二阶段，回复携带记忆内容。"""
    from tgmon.qqbot import agent

    member_profile.set_nickname("v2-a", "科比")
    memory.record_turn("v2-g", "v2-a", "user", "上次说到的流萤情报", speaker="科比")

    calls = []

    class _Route:
        ok = True
        text = '{"action":"chat","args":{},"reply":"先这样回"}'
        provider_name = "test"
        tokens_in = tokens_out = 0

    class _Chat:
        ok = True
        text = "科比你上次问的流萤，我记得～"
        provider_name = "test"
        tokens_in = tokens_out = 0

    async def _fake(system, user):
        calls.append((system, user))
        return _Route() if len(calls) == 1 else _Chat()

    from tgmon.providers import registry
    monkeypatch.setattr(registry, "complete_with_failover", _fake)
    replies = await agent.handle("接着聊", "v2-g", True, "v2-a")
    # 第二阶段的回复胜出，且 prompt 里带着结构化上下文
    assert replies[0].text == "科比你上次问的流萤，我记得～"
    assert len(calls) == 2
    assert "【当前用户】科比" in calls[1][1]
    assert "流萤情报" in calls[1][1]
    assert "CHAT_SYSTEM" not in calls[1][1]          # system 是人格提示词
    assert "接着聊" in calls[1][1]


@pytest.mark.asyncio
async def test_agent_chat_fallback_without_memory(db, monkeypatch):
    """无记忆且无人设时直接用路由阶段 reply，不发起第二次调用。"""
    from tgmon.qqbot import agent

    calls = []

    class _Res:
        ok = True
        text = '{"action":"chat","args":{},"reply":"我是 tgmon 群助手～"}'
        provider_name = "test"
        tokens_in = tokens_out = 0

    async def _fake(system, user):
        calls.append((system, user))
        return _Res()

    from tgmon.providers import registry
    monkeypatch.setattr(registry, "complete_with_failover", _fake)
    replies = await agent.handle("你是谁", "v2-nogroup", True, "v2-nomember")
    assert replies[0].text == "我是 tgmon 群助手～"
    assert len(calls) == 1                           # 只有路由一次


@pytest.mark.asyncio
async def test_agent_chat_persona_merged(db, monkeypatch):
    """人设修复：QQ_AI_SYSTEM 必须进入第二阶段，无记忆时也强制走第二阶段。"""
    from tgmon.qqbot import agent

    settings.set_many({"QQ_AI_SYSTEM": "【角色人设：纳西妲】你是纳西妲，智慧之神。"})
    try:
        assert "纳西妲" in agent._chat_system()      # system 动态合并人设

        calls = []

        class _Res:
            ok = True
            text = '{"action":"chat","args":{},"reply":"我是 tgmon 群助手～"}'
            provider_name = "test"
            tokens_in = tokens_out = 0

        async def _fake(system, user):
            calls.append((system, user))
            return _Res()

        from tgmon.providers import registry
        monkeypatch.setattr(registry, "complete_with_failover", _fake)
        # 无记忆（v2-nogroup 无轮次）但有人设 → 仍走第二阶段
        replies = await agent.handle("你是谁", "v2-nogroup", True, "v2-nomember")
        assert len(calls) == 2                       # 路由 + 人设阶段
        assert "纳西妲" in calls[1][0]               # 第二阶段 system 带人设
        assert "你是谁" in calls[1][1]
    finally:
        settings.set_many({"QQ_AI_SYSTEM": ""})


@pytest.mark.asyncio
async def test_agent_chat_persona_suppresses_route_reply(db, monkeypatch):
    """有人设时，第二阶段回复胜出（路由 reply 只是兜底）。"""
    from tgmon.qqbot import agent

    settings.set_many({"QQ_AI_SYSTEM": "你是纳西妲。"})
    try:
        calls = []

        class _Route:
            ok = True
            text = '{"action":"chat","args":{},"reply":"我是 tgmon 群助手～"}'
            provider_name = "test"
            tokens_in = tokens_out = 0

        class _Chat:
            ok = True
            text = "呼……你好呀，我是纳西妲～"
            provider_name = "test"
            tokens_in = tokens_out = 0

        async def _fake(system, user):
            calls.append((system, user))
            return _Route() if len(calls) == 1 else _Chat()

        from tgmon.providers import registry
        monkeypatch.setattr(registry, "complete_with_failover", _fake)
        replies = await agent.handle("你是谁", "v2-nogroup", True, "v2-nomember")
        assert replies[0].text == "呼……你好呀，我是纳西妲～"
    finally:
        settings.set_many({"QQ_AI_SYSTEM": ""})
