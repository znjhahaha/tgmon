"""Knowledge publication and context shared by translation and conversation."""
from __future__ import annotations

import hashlib
from urllib.parse import urlparse

from ..db import session_scope
from ..models import KnowledgePage, KnowledgeSource

TRUSTED_HOSTS = {"genshin-impact.fandom.com", "honkai-star-rail.fandom.com",
                 "zenless-zone-zero.fandom.com"}
TRUSTED_ORIGINS = {"starrailres", "genshin-db", "zenlessdata", "gachabase", "seed"}


def publish_page(s, page: KnowledgePage) -> bool:
    source = s.get(KnowledgeSource, page.source_id)
    valid = bool(page.content.strip() and page.title.strip() and page.enabled
                 and source and source.enabled and page.status != "disabled"
                 and page.review_reason != "manual")
    approved = bool(source and source.trusted) or page.status == "active"
    decision = (page.attrs or {}).get("document_review")
    if decision == "active":
        valid = bool(page.content.strip() and page.title.strip() and page.enabled and source and source.enabled)
    page.document_status = "active" if valid and (approved or decision == "active") else "pending"
    if page.status == "rejected" and page.review_reason not in ("missing_chinese_name", "no_translation_alias") and decision != "active":
        page.document_status = "rejected"
    if decision in ("disabled", "pending"):
        page.document_status = decision
    return page.document_status == "active"


def sync_page_index(page_id: int) -> None:
    from ..retrieval import RetrievalDocument, index_document, remove
    from .wiki import _plain
    with session_scope() as s:
        page = s.get(KnowledgePage, page_id)
        if page is None:
            data = None
        else:
            active = publish_page(s, page)
            source = s.get(KnowledgeSource, page.source_id)
            data = RetrievalDocument("wiki", page.id, _plain(page.content), title=page.title,
                game=source.game, version=page.revision_id,
                deeplink=source.url.split("/api.php")[0] + "/wiki/" + page.title.replace(" ", "_")) if active else None
    if data:
        index_document(data)
    else:
        remove("wiki", page_id)


def refresh_documents(limit: int = 100, after: int = 0) -> dict:
    with session_scope() as s:
        ids = [pid for pid, in s.query(KnowledgePage.id).filter(KnowledgePage.id > after)
               .order_by(KnowledgePage.id).limit(limit)]
    for pid in ids:
        sync_page_index(pid)
    return {"done": len(ids), "after": ids[-1] if ids else after}


def reference_context(text: str, game: str | None = None) -> tuple[str, str]:
    from ..retrieval import build_context, search
    hits = search(text[:600], game=game, ref_types=("wiki",), limit=4)
    context = build_context(hits, max_chars=4500)
    return context, hashlib.sha256(context.encode()).hexdigest()[:16]


def origin_trusted(origin: str) -> bool:
    return origin.lower() in TRUSTED_ORIGINS
