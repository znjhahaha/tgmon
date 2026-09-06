"""Wiki candidate validation and shared review transitions."""
from __future__ import annotations

from datetime import datetime
from urllib.parse import urlparse

from ..db import session_scope
from ..models import GlossaryAlias, GlossaryEntry, KnowledgePage, KnowledgeSource
from ..util import alias_match_mode, has_cjk, unreliable_alias_reason

STATUSES = ("pending", "active", "rejected", "disabled")
REASONS = {"missing_chinese_name": "缺少中文译名", "invalid_name": "译名含未解析标记",
           "no_translation_alias": "缺少有效别名", "manual": "人工拒绝"}


def candidate_problem(name: str | None, aliases: list | None = None) -> str | None:
    name = (name or "").strip()
    if not has_cjk(name):
        return "missing_chinese_name"
    if len(name) > 200 or any(marker in name for marker in ("{{", "}}", "[[", "]]", "<", ">", "\n")):
        return "invalid_name"
    if aliases is not None and not any(str(a).strip() and str(a).strip().casefold() != name.casefold() for a in aliases):
        return "no_translation_alias"
    return None


def page_aliases(page: KnowledgePage) -> list[str]:
    return list(dict.fromkeys(str(a).strip() for a in [page.title, *(page.aliases or [])] if str(a).strip()))


def review_page(s, page: KnowledgePage, status: str, canonical_zh: str | None = None) -> None:
    if status not in STATUSES:
        raise ValueError("无效审核状态")
    source = s.get(KnowledgeSource, page.source_id)
    if source is None:
        raise ValueError("Wiki 来源不存在")
    name = page.entity if canonical_zh is None else canonical_zh.strip()
    problem = candidate_problem(name, page_aliases(page))
    if status in ("active", "pending") and problem:
        raise ValueError("请补充有效中文译名和别名后再审核")
    previous_name = page.entity
    page.entity = name
    page.status = status
    page.reviewed_at = datetime.utcnow()
    page.review_reason = (problem or "manual") if status == "rejected" else None
    origin = f"wiki:{urlparse(source.url).netloc}"[:120]
    entry = s.query(GlossaryEntry).filter_by(game=source.game, canonical_zh=name).first()
    if status == "active":
        if entry is None:
            entry = GlossaryEntry(game=source.game, canonical_zh=name, category=page.category,
                                  origin=origin, origin_ref=page.wiki_page_id, attrs=page.attrs or {})
            s.add(entry)
            s.flush()
        was_active = entry.status == "active"
        entry.status = "active"
        entry.enabled = True
        entry.reviewed_at = datetime.utcnow()
        # Wiki metadata must not overwrite an existing reviewed official entry.
        if not was_active or entry.origin == origin:
            entry.attrs = page.attrs or entry.attrs
            entry.category = page.category or entry.category
        known = {(a.surface, a.lang) for a in s.query(GlossaryAlias).filter_by(entry_id=entry.id)}
        for surface in [name, *page_aliases(page)]:
            lang = "zh" if has_cjk(surface) else "en"
            if (surface, lang) in known:
                continue
            s.add(GlossaryAlias(entry_id=entry.id, surface=surface, lang=lang,
                                alias_kind="primary", match_mode=alias_match_mode(surface),
                                enabled=unreliable_alias_reason(surface) is None))
            known.add((surface, lang))
    elif (entry is not None and entry.origin == origin
          and entry.origin_ref == page.wiki_page_id):
        entry.status = status
        entry.reviewed_at = datetime.utcnow()
    if previous_name != name:
        old = s.query(GlossaryEntry).filter_by(game=source.game, canonical_zh=previous_name,
                                               origin=origin, origin_ref=page.wiki_page_id).first()
        if old is not None and old.status in ("pending", "active"):
            old.status = "rejected"
            old.reviewed_at = datetime.utcnow()
    from .service import publish_page
    publish_page(s, page)


def review_entry(s, entry: GlossaryEntry, status: str) -> bool:
    """Keep the entity and its staged Wiki pages in sync for existing bulk APIs."""
    if status not in STATUSES:
        raise ValueError("无效审核状态")
    if status == "active" and entry.origin.startswith("wiki:") and candidate_problem(entry.canonical_zh):
        return False
    pages = (s.query(KnowledgePage).join(KnowledgeSource)
             .filter(KnowledgeSource.game == entry.game, KnowledgePage.entity == entry.canonical_zh,
                     KnowledgePage.status == "pending").all())
    for page in pages:
        if status == "active" and candidate_problem(page.entity, page_aliases(page)):
            continue
        review_page(s, page, status)
    entry.status = status
    entry.enabled = status == "active" or entry.enabled
    entry.reviewed_at = datetime.utcnow()
    return True


def repair_invalid_wiki() -> dict[str, int]:
    """Idempotently reject legacy candidates, retaining original page evidence."""
    counts = {"pages": 0, "entries": 0}
    with session_scope() as s:
        for page in s.query(KnowledgePage).filter(KnowledgePage.status.in_(("pending", "active"))):
            problem = candidate_problem(page.entity, page_aliases(page))
            if problem and page.content:
                from .wiki import parse_page
                parsed = parse_page({"title": page.title, "content": page.content}, page.category)
                if not parsed["quality_reason"]:
                    page.entity = parsed["canonical_zh"]
                    page.aliases = parsed["aliases"]
                    page.attrs = parsed["attrs"]
                    page.status = "pending"
                    page.review_reason = None
                    continue
            if problem:
                page.status = "rejected"
                page.review_reason = problem
                page.reviewed_at = datetime.utcnow()
                counts["pages"] += 1
        for entry in s.query(GlossaryEntry).filter(GlossaryEntry.origin.like("wiki:%"),
                                                  GlossaryEntry.status.in_(("pending", "active"))):
            if candidate_problem(entry.canonical_zh):
                entry.status = "rejected"
                entry.reviewed_at = datetime.utcnow()
                counts["entries"] += 1
    if any(counts.values()):
        from ..glossary import touch_version
        touch_version()
    return counts
