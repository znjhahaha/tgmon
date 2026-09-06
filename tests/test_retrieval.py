from datetime import datetime, timedelta

import pytest

from tgmon.db import engine, session_scope
from tgmon.models import Base, Channel, MonitorMessage, RetrievalIndex
from tgmon.retrieval import index_message, search, build_context


@pytest.fixture
def indexed_messages():
    Base.metadata.create_all(engine)
    with session_scope() as s:
        s.add(Channel(id=88001, title="检索测试", game="原神", tg_id="retrieval-test"))
        s.add(MonitorMessage(id=88001, channel_id=88001, tg_message_id="1",
                             text_raw="Kafka 7.1", text_zh="卡芙卡卡池更新",
                             game_detected="原神", published_at=datetime.utcnow()))
    index_message(88001)
    yield
    with session_scope() as s:
        s.query(RetrievalIndex).filter(RetrievalIndex.ref_id == 88001).delete()
        s.query(MonitorMessage).filter(MonitorMessage.id == 88001).delete()
        s.query(Channel).filter(Channel.id == 88001).delete()


def test_index_is_idempotent_and_filters(indexed_messages):
    index_message(88001)
    assert search("卡芙卡", game="原神", channel_id=88001)[0].ref_id == 88001
    assert not search("卡芙卡", game="绝区零")
    assert not search("卡芙卡", since=datetime.utcnow() + timedelta(days=1))
    with session_scope() as s:
        assert s.query(RetrievalIndex).filter_by(ref_type="message", ref_id=88001).count() == 1


def test_context_contains_evidence_reference(indexed_messages):
    assert "【消息#88001】" in build_context(search("卡芙卡"))
    assert build_context(search("卡芙卡"), max_chars=1) == ""
