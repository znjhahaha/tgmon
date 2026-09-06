"""Offline embedding and index lifecycle regressions (no network/model download)."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest

from tgmon import retrieval, settings
from tgmon.db import engine, session_scope
from tgmon.models import (AppSetting, Base, Channel, KnowledgePage, KnowledgeSource,
                         MonitorMessage, RetrievalIndex)


def published_pages(ids):
    with session_scope() as s:
        s.add(KnowledgeSource(id=990001, url="https://example.org/api.php", enabled=True))
        s.flush()
        s.add_all([KnowledgePage(id=i, source_id=990001, wiki_page_id=str(i),
                               title="Test", content="Evidence", document_status="active") for i in ids])


@pytest.fixture(autouse=True)
def database():
    Base.metadata.create_all(engine)
    with session_scope() as s:
        s.query(AppSetting).filter(AppSetting.key.like("EMBEDDING_%")).delete()
    settings.invalidate()
    yield
    with session_scope() as s:
        s.query(KnowledgePage).filter(KnowledgePage.id >= 990000).delete()
        s.query(KnowledgeSource).filter(KnowledgeSource.id >= 990000).delete()
        s.query(RetrievalIndex).filter(RetrievalIndex.ref_id >= 990000).delete()
        s.query(MonitorMessage).filter(MonitorMessage.id >= 990000).delete()
        s.query(Channel).filter(Channel.id >= 990000).delete()
        s.query(AppSetting).filter(AppSetting.key.like("EMBEDDING_%")).delete()
    settings.invalidate()


def test_embedding_is_enabled_without_provider_configuration():
    assert settings.get("EMBEDDING_ENABLED") is True


def test_query_uses_local_model_and_never_provider_registry(monkeypatch):
    from tgmon import embeddings
    from tgmon.providers import registry

    monkeypatch.setattr(registry, "load_configs", lambda *a, **k: pytest.fail("API configuration read"))
    monkeypatch.setattr(embeddings, "embed_query", lambda text: np.array([1., 0.], dtype=np.float32))
    settings.set_many({"EMBEDDING_ENABLED": True})
    assert np.array_equal(retrieval._embedding_for_text("角色复刻"), [1., 0.])


def test_runtime_model_loading_requires_local_files(monkeypatch, tmp_path):
    import fastembed
    from tgmon import embeddings

    calls = []
    backend = object()
    monkeypatch.setattr(fastembed, "TextEmbedding", lambda **kwargs: calls.append(kwargs) or backend)
    monkeypatch.setattr(embeddings, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(embeddings, "_model", None)
    monkeypatch.setattr(embeddings, "_retry_after", 0.)
    assert embeddings._get_model() is backend
    assert embeddings._get_model() is backend
    assert len(calls) == 1
    assert calls[0]["local_files_only"] is True
    assert calls[0]["cuda"] is False


def test_document_embedding_keeps_tail_and_normalizes(monkeypatch):
    from tgmon import embeddings

    seen = []

    def passage_embed(texts, **kwargs):
        for text in texts:
            seen.append(text)
            yield np.array([3., 4.], dtype=np.float32)

    monkeypatch.setattr(embeddings, "_get_model", lambda: SimpleNamespace(passage_embed=passage_embed))
    vectors = embeddings.embed_documents(["", "正文" * 1000 + "末尾证据"])
    assert vectors[0] is None
    assert any("末尾证据" in text for text in seen)
    assert np.allclose(vectors[1], [0.6, 0.8])
    assert vectors[1].dtype == np.float32


def test_model_unavailable_returns_lexical_results(monkeypatch):
    from tgmon import embeddings

    monkeypatch.setattr(embeddings, "_get_model", lambda: None)
    settings.set_many({"EMBEDDING_ENABLED": True})
    published_pages([990001])
    retrieval.index_document(retrieval.RetrievalDocument("wiki", 990001, "卡池复刻安排"))
    assert retrieval.search("复刻")[0].ref_id == 990001
    assert embeddings.embed_query("复刻") is None


def test_semantic_search_does_not_return_unrelated_rows(monkeypatch):
    from tgmon import embeddings

    settings.set_many({"EMBEDDING_ENABLED": True})
    published_pages(range(990001, 990005))
    monkeypatch.setattr(embeddings, "embed_query", lambda text: np.array([1., 0.], dtype=np.float32))
    with session_scope() as s:
        s.add_all([
            RetrievalIndex(ref_type="wiki", ref_id=990001, text="角色再度登场",
                           embedding=retrieval._pack([1., 0.]), embedding_model=embeddings.MODEL_ID),
            RetrievalIndex(ref_type="wiki", ref_id=990002, text="完全无关的内容",
                           embedding=retrieval._pack([0., 1.]), embedding_model=embeddings.MODEL_ID),
            RetrievalIndex(ref_type="wiki", ref_id=990003, text="尚未计算向量"),
            RetrievalIndex(ref_type="wiki", ref_id=990004, text="旧模型的向量",
                           embedding=retrieval._pack([1., 0.]), embedding_model="old-api-model"),
        ])
    assert [h.ref_id for h in retrieval.search("复刻卡池")] == [990001]


def test_old_exact_match_survives_more_than_500_recent_embeddings(monkeypatch):
    from tgmon import embeddings

    settings.set_many({"EMBEDDING_ENABLED": True})
    published_pages([990001, *range(991000, 991501)])
    monkeypatch.setattr(embeddings, "embed_query", lambda text: np.array([1., 0.], dtype=np.float32))
    now = datetime.utcnow()
    retrieval.index_document(retrieval.RetrievalDocument(
        "wiki", 990001, "很早以前的专有代号", published_at=now - timedelta(days=90)))
    with session_scope() as s:
        s.add_all([RetrievalIndex(ref_type="wiki", ref_id=991000 + n,
                                 text="近期无关内容", published_at=now,
                                 embedding=retrieval._pack([0., 1.]),
                                 embedding_model=embeddings.MODEL_ID) for n in range(501)])
    assert [h.ref_id for h in retrieval.search("专有代号")] == [990001]


@pytest.mark.asyncio
async def test_backfill_replaces_old_model_and_is_idempotent(monkeypatch):
    from tgmon import embeddings

    settings.set_many({"EMBEDDING_ENABLED": True})
    with session_scope() as s:
        s.add(RetrievalIndex(ref_type="wiki", ref_id=990001, text="新内容",
                             embedding=retrieval._pack([0., 1.]), embedding_model="old-api-model"))
    monkeypatch.setattr(embeddings, "embed_documents",
                        lambda texts: [np.array([1., 0.], dtype=np.float32) for _ in texts])
    assert await retrieval.embed_pending() >= 1
    with session_scope() as s:
        row = s.query(RetrievalIndex).filter_by(ref_id=990001).one()
        assert row.embedding_model == embeddings.MODEL_ID
        assert np.array_equal(retrieval._unpack(row.embedding), [1., 0.])
    assert await retrieval.embed_pending() == 0


@pytest.mark.asyncio
async def test_backfill_does_not_attach_stale_vector_after_content_edit(monkeypatch):
    from tgmon import embeddings

    settings.set_many({"EMBEDDING_ENABLED": True})
    retrieval.index_document(retrieval.RetrievalDocument("wiki", 990001, "旧内容"))

    def during_embedding(texts):
        retrieval.index_document(retrieval.RetrievalDocument("wiki", 990001, "更新的内容"))
        return [np.array([1., 0.], dtype=np.float32) for _ in texts]

    monkeypatch.setattr(embeddings, "embed_documents", during_embedding)
    await retrieval.embed_pending()
    with session_scope() as s:
        row = s.query(RetrievalIndex).filter_by(ref_id=990001).one()
        assert row.text == "更新的内容"
        assert row.embedding is None


def test_message_edit_invalidates_old_embedding():
    with session_scope() as s:
        s.add(Channel(id=990001, title="向量测试", tg_id="embedding-test"))
        s.add(MonitorMessage(id=990001, channel_id=990001, tg_message_id="1",
                             text_raw="原文", published_at=datetime.utcnow()))
    retrieval.index_message(990001)
    with session_scope() as s:
        row = s.query(RetrievalIndex).filter_by(ref_type="message", ref_id=990001).one()
        row.embedding = retrieval._pack([1., 0.])
        s.get(MonitorMessage, 990001).text_raw = "更新原文"
    retrieval.index_message(990001)
    with session_scope() as s:
        assert s.query(RetrievalIndex).filter_by(ref_type="message", ref_id=990001).one().embedding is None


@pytest.mark.parametrize("bad", [None, [], [float("nan")], [float("inf")], [0., 0.]])
def test_invalid_vectors_are_not_persisted(bad):
    assert retrieval._pack(bad) is None
