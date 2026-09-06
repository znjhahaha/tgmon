import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tgmon import glossary
from tgmon.admin.routes import glossary_ui
from tgmon.admin.deps import require_admin
from tgmon.db import engine, session_scope
from tgmon.kb.wiki import MediaWikiAdapter
from tgmon.models import Base, GlossaryAlias, GlossaryEntry, KnowledgePage, KnowledgeSource, RetrievalIndex


@pytest.fixture(autouse=True)
def db():
    Base.metadata.create_all(engine)
    with session_scope() as s:
        s.add(KnowledgeSource(id=998001, game="绝区零", url="https://wiki.example/api.php"))
        s.flush()
        s.add_all([
            KnowledgePage(id=998001, source_id=998001, wiki_page_id="bad",
                          title="Zenless Limit", entity="Zenless Limit", status="pending"),
            KnowledgePage(id=998002, source_id=998001, wiki_page_id="good",
                          title="Sons of Calydon", entity="卡吕冬之子", status="pending",
                          category="faction", aliases=["Sons of Calydon"]),
            GlossaryEntry(id=998001, game="绝区零", category="lore",
                          canonical_zh="Zenless Limit", status="pending",
                          origin="wiki:wiki.example", origin_ref="bad"),
        ])
    yield
    with session_scope() as s:
        s.query(GlossaryEntry).filter(GlossaryEntry.origin == "wiki:wiki.example").delete()
        page_ids = s.query(KnowledgePage.id).filter_by(source_id=998001)
        s.query(RetrievalIndex).filter(RetrievalIndex.ref_type == "wiki", RetrievalIndex.ref_id.in_(page_ids)).delete(synchronize_session=False)
        s.query(KnowledgePage).filter_by(source_id=998001).delete()
        s.query(KnowledgeSource).filter(KnowledgeSource.id == 998001).delete()
    glossary.invalidate_cache()


@pytest.mark.asyncio
async def test_wiki_sync_rejects_english_page_without_creating_entry(monkeypatch):
    adapter = MediaWikiAdapter("https://wiki.example/api.php", min_interval=0)

    async def members(category, limit=500):
        return [{"ns": 0, "pageid": 1, "title": "Zenless Limit"}]

    async def page(pageid):
        return {"pageid": 1, "revid": 2, "title": "Zenless Limit",
                "content": "{{Infobox Lore|name=Zenless Limit}}"}

    monkeypatch.setattr(adapter, "category_members", members)
    monkeypatch.setattr(adapter, "page", page)
    result = await adapter.sync(998001, {"lore": "Category:Lore"})
    assert result["rejected"] == 1
    with session_scope() as s:
        row = s.query(KnowledgePage).filter_by(source_id=998001, wiki_page_id="1").one()
        assert row.status == "rejected"
        assert s.query(GlossaryEntry).filter(GlossaryEntry.origin_ref == "1").count() == 0


@pytest.mark.asyncio
async def test_wiki_bulk_reject_only_invalid_pages_and_is_idempotent():
    app = FastAPI()
    app.include_router(glossary_ui.router)
    app.dependency_overrides[require_admin] = lambda: "admin"
    with TestClient(app) as client:
        data = {"wiki_game": "绝区零", "wiki_status": "pending", "status": "rejected",
                "scope": "filtered", "invalid_only": "1"}
        response = client.post("/kb/wiki/bulk-status", data=data)
        assert response.status_code == 200
        assert "已处理 1" in response.text
        response = client.post("/kb/wiki/bulk-status", data=data)
        assert "已处理 0" in response.text
    with session_scope() as s:
        assert s.get(KnowledgePage, 998001).status == "rejected"
        assert s.get(KnowledgePage, 998002).status == "pending"
        assert s.get(GlossaryEntry, 998001).status == "rejected"


def test_wiki_pending_page_requires_chinese_name_for_activation():
    with session_scope() as s:
        page = s.get(KnowledgePage, 998001)
        page.status = "pending"
    with pytest.raises(ValueError, match="中文"):
        with session_scope() as s:
            from tgmon.kb.wiki_review import review_page
            review_page(s, s.get(KnowledgePage, 998001), "active")


@pytest.mark.asyncio
async def test_rejected_page_stays_rejected_when_upstream_changes(monkeypatch):
    with session_scope() as s:
        s.get(KnowledgePage, 998002).status = "rejected"
    adapter = MediaWikiAdapter("https://wiki.example/api.php", min_interval=0)

    async def members(*args, **kwargs):
        return [{"title": "Sons of Calydon", "pageid": "good"}]

    async def page(*args):
        return {"pageid": "good", "revid": "new", "title": "Sons of Calydon",
                "content": "{{Other Languages|zh_cn=卡吕冬之子}} a new paragraph"}

    monkeypatch.setattr(adapter, "category_members", members)
    monkeypatch.setattr(adapter, "page", page)
    await adapter.sync(998001, {"faction": "Category:Factions"})
    with session_scope() as s:
        assert s.get(KnowledgePage, 998002).status == "rejected"
        assert s.query(GlossaryEntry).filter_by(canonical_zh="卡吕冬之子").count() == 0


def test_batch_selection_is_scoped_and_empty_selection_changes_nothing():
    app = FastAPI()
    app.include_router(glossary_ui.router)
    app.dependency_overrides[require_admin] = lambda: "admin"
    with TestClient(app) as client:
        data = {"wiki_game": "原神", "wiki_status": "pending", "status": "rejected",
                "scope": "selected", "page_ids": "998002"}
        assert "已处理 0" in client.post("/kb/wiki/bulk-status", data=data).text
        data.pop("page_ids")
        data["wiki_game"] = "绝区零"
        assert "选择" in client.post("/kb/wiki/bulk-status", data=data).text
    with session_scope() as s:
        assert s.get(KnowledgePage, 998002).status == "pending"


def test_review_approval_merges_aliases_and_updates_fragment():
    app = FastAPI()
    app.include_router(glossary_ui.router)
    app.dependency_overrides[require_admin] = lambda: "admin"
    with TestClient(app) as client:
        response = client.post("/kb/wiki/page/998002/status", data={"status": "active"})
        assert response.status_code == 200
        assert 'id="wiki-review"' in response.text
        assert 'value="998002"' not in response.text
    with session_scope() as s:
        assert s.get(KnowledgePage, 998002).status == "active"
        term = s.query(GlossaryEntry).filter_by(canonical_zh="卡吕冬之子").one()
        assert term.status == "active"
        assert s.query(GlossaryAlias).filter_by(entry_id=term.id, surface="Sons of Calydon").count() == 1


def test_legacy_repair_keeps_evidence_and_is_idempotent():
    from tgmon.kb.wiki_review import repair_invalid_wiki
    first = repair_invalid_wiki()
    assert first["pages"] >= 1 and first["entries"] >= 1
    assert repair_invalid_wiki() == {"pages": 0, "entries": 0}
    with session_scope() as s:
        row = s.get(KnowledgePage, 998001)
        assert row.status == "rejected" and row.review_reason == "missing_chinese_name"
        assert row.title == "Zenless Limit"
        assert s.get(KnowledgePage, 998002).status == "pending"


def test_legacy_repair_recovers_chinese_candidate_from_original_fandom_page():
    from tgmon.kb.wiki_review import repair_invalid_wiki
    with session_scope() as s:
        s.get(KnowledgePage, 998001).content = "{{Other Languages|en=Zenless Limit|zhs=绝区极限}}"
    repair_invalid_wiki()
    assert repair_invalid_wiki() == {"pages": 0, "entries": 0}
    with session_scope() as s:
        row = s.get(KnowledgePage, 998001)
        assert row.entity == "绝区极限" and row.status == "pending"
        assert s.get(GlossaryEntry, 998001).status == "rejected"


@pytest.mark.asyncio
async def test_manual_chinese_name_survives_later_wiki_sync(monkeypatch):
    from tgmon.kb.wiki_review import review_page
    with session_scope() as s:
        row = s.get(KnowledgePage, 998001)
        row.content = "{{Infobox Lore|name=Zenless Limit}}"
        row.aliases = ["Zenless Limit"]
        review_page(s, row, "active", canonical_zh="绝区极限")
    adapter = MediaWikiAdapter("https://wiki.example/api.php", min_interval=0)

    async def members(*args, **kwargs):
        return [{"title": "Zenless Limit", "pageid": "bad"}]

    async def page(*args):
        return {"pageid": "bad", "title": "Zenless Limit", "revid": "new",
                "content": "{{Infobox Lore|name=Zenless Limit}} new paragraph"}

    monkeypatch.setattr(adapter, "category_members", members)
    monkeypatch.setattr(adapter, "page", page)
    await adapter.sync(998001, {"lore": "Category:Lore"})
    with session_scope() as s:
        row = s.get(KnowledgePage, 998001)
        assert row.entity == "绝区极限"
        assert row.status == "active"


@pytest.mark.parametrize("status", ["rejected", "disabled", "pending"])
def test_revoking_approved_wiki_page_removes_its_term_from_translation(status):
    from tgmon.kb.wiki_review import review_page
    with session_scope() as s:
        row = s.get(KnowledgePage, 998002)
        review_page(s, row, "active")
        review_page(s, row, status)
        term = s.query(GlossaryEntry).filter_by(canonical_zh="卡吕冬之子").one()
        assert term.status == status


def test_rejecting_wiki_candidate_preserves_existing_official_term():
    from tgmon.kb.wiki_review import review_page
    with session_scope() as s:
        row = s.get(KnowledgePage, 998002)
        s.add(GlossaryEntry(game="绝区零", canonical_zh=row.entity, status="active",
                            origin="official", origin_ref="official-test"))
        s.flush()
        review_page(s, row, "rejected")
        term = s.query(GlossaryEntry).filter_by(canonical_zh=row.entity).one()
        assert term.status == "active"
        s.delete(term)
