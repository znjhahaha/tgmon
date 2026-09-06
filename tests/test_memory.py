import pytest

from tgmon.db import engine, session_scope
from tgmon.models import Base, Conversation, ConversationTurn
from tgmon import memory


@pytest.fixture(autouse=True)
def memory_db():
    Base.metadata.create_all(engine)
    yield
    with session_scope() as s:
        ids = [x.id for x in s.query(Conversation).filter(Conversation.scope_id.in_(["mem-g", "mem-a", "mem-b"]))]
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
