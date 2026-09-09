import pytest

from tgmon.db import engine, session_scope
from tgmon.memory import scope_keys
from tgmon.models import Base, Conversation, ConversationTurn
from tgmon import memory


@pytest.fixture(autouse=True)
def memory_db():
    Base.metadata.create_all(engine)
    yield
    # 个人记忆行的 scope_id 是 sha256(group\0member)（见 memory.scope_keys），
    # 不是字面 member —— 只按字面量删会遗留 member_v2 行，顶偏后续模块的
    # 自增 id（曾让 test_memory_v2 的 summarize 测试组合运行必挂）
    scope_ids = {"mem-g"} | {scope_keys("mem-g", m)[1][1] for m in ("mem-a", "mem-b")}
    with session_scope() as s:
        ids = [x.id for x in s.query(Conversation).filter(Conversation.scope_id.in_(scope_ids))]
        s.query(ConversationTurn).filter(ConversationTurn.conversation_id.in_(ids)).delete()
        s.query(Conversation).filter(Conversation.id.in_(ids)).delete()


def test_personal_facts_are_isolated():
    memory.remember("mem-g", "mem-a", "喜欢卡芙卡")
    assert "喜欢卡芙卡" in memory.load_context("mem-g", "mem-a")["facts"]
    assert "喜欢卡芙卡" not in memory.load_context("mem-g", "mem-b")["facts"]
    assert memory.forget("user", "mem-a") == 1
    assert memory.load_context("mem-g", "mem-a")["facts"] == {}


def test_group_recent_context_is_shared():
    memory.record_turn("mem-g", "mem-a", "user", "今天有新消息吗")
    assert any(t["content"] == "今天有新消息吗" for t in memory.load_context("mem-g", "mem-b")["recent"])
