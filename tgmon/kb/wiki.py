"""Small MediaWiki/Fandom API adapter used by the knowledge base."""
from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx
import mwparserfromhell

from ..db import session_scope
from ..models import KnowledgePage, KnowledgeSource, GlossaryAlias, GlossaryEntry
from .importer import upsert_entity
from .wiki_review import candidate_problem


def _field_key(key) -> str:
    return str(key).strip().lower().replace(" ", "_").replace("-", "_")


def _plain(value) -> str:
    code = mwparserfromhell.parse(str(value))
    for tag in list(code.filter_tags()):
        if str(tag.tag).strip().lower() == "ref":
            code.remove(tag)
    for template in reversed(code.filter_templates()):
        name = _field_key(template.name)
        if name in ("lang", "nowrap", "nobr"):
            key = "2" if name == "lang" else "1"
            if template.has(key):
                code.replace(template, str(template.get(key).value))
    return " ".join(code.strip_code().split()).strip()

# Public Fandom MediaWiki endpoints.  These are seeded on first boot so the
# administrator can sync all three supported games without hand-entering URLs.
DEFAULT_SOURCES = (
    ("原神", "https://genshin-impact.fandom.com/api.php",
     {"character": "Category:Characters", "npc": "Category:Non-Player Characters",
      "faction": "Category:Factions", "lore": "Category:Lore"}),
    ("崩坏:星穹铁道", "https://honkai-star-rail.fandom.com/api.php",
     {"character": "Category:Characters", "npc": "Category:Non-Playable Characters",
      "faction": "Category:Factions", "lore": "Category:World"}),
    ("绝区零", "https://zenless-zone-zero.fandom.com/api.php",
     {"character": "Category:Agents", "npc": "Category:Non-Player Characters",
      "faction": "Category:Factions", "lore": "Category:Lore"}),
)

_CATEGORY_ALIASES = {
    "character": ("Character", "Characters", "Playable Characters", "Agent", "Agents"),
    "npc": ("Non-Player Character", "Non-Player Characters", "Non-Playable Character", "Non-Playable Characters", "NPCs"),
    "faction": ("Faction", "Factions", "Organizations"),
    "lore": ("Lore", "World", "Worldbuilding"),
}


def normalize_api_url(url: str) -> str:
    """Accept either a Fandom page URL or its explicit ``api.php`` endpoint."""
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return raw
    if parsed.path.endswith("/api.php"):
        return raw
    return f"{parsed.scheme}://{parsed.netloc}/api.php" if parsed.scheme and parsed.netloc else raw


def parse_infobox(wikitext: str) -> dict[str, str]:
    """Parse nested MediaWiki templates without splitting links or inner pipes."""
    for template in mwparserfromhell.parse(wikitext or "").filter_templates():
        if _field_key(template.name).startswith("infobox"):
            return {_field_key(p.name): _plain(p.value) for p in template.params if _plain(p.value)}
    return {}


def parse_page(page: dict[str, Any], category: str = "character") -> dict[str, Any]:
    title = str(page.get("title") or "").strip()
    content = str(page.get("content") or page.get("wikitext") or "")
    info = parse_infobox(content)
    languages = {}
    for template in mwparserfromhell.parse(content).filter_templates():
        if _field_key(template.name) in ("other_languages", "otherlanguages", "languages", "language"):
            languages.update({_field_key(p.name): _plain(p.value) for p in template.params})
    candidates = [languages.get(k, "") for k in
                  ("zh_cn", "zh_hans", "zhs", "zh_s", "chinese_simplified", "simplified_chinese", "zh")]
    candidates += [info.get(k, "") for k in
                   ("name_zh", "chinese_name", "zh_name", "localized_name", "name")]
    candidates.append(title)
    canonical = next((value.strip() for value in candidates if value and not candidate_problem(value)), "")
    aliases: list[str] = []
    for key in ("aliases", "alias", "other_names", "nicknames", "name_en", "english_name"):
        if info.get(key):
            aliases.extend(x.strip() for x in re.split(r"[,;/、|]", info[key]) if x.strip())
    if info.get("name") and info["name"] != canonical:
        aliases.append(info["name"])
    if title and title != canonical:
        aliases.append(title)
    attrs = {k: info[k] for k in ("rarity", "element", "path", "faction", "weapon", "region") if info.get(k)}
    return {"wiki_page_id": str(page.get("pageid") or page.get("id") or title),
            "revision_id": str(page.get("revid") or page.get("revision_id") or ""),
            "title": title, "content": content, "canonical_zh": canonical,
            "aliases": list(dict.fromkeys(aliases)), "attrs": attrs,
            "category": category, "quality_reason": candidate_problem(canonical, aliases)}


class MediaWikiAdapter:
    def __init__(self, api_url: str, *, timeout: float = 30, min_interval: float = 0.5):
        self.api_url = normalize_api_url(api_url)
        self.timeout = timeout
        self.min_interval = min_interval
        self._last_request = 0.0

    async def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        wait = self.min_interval - (asyncio.get_event_loop().time() - self._last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        last = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                    response = await client.get(self.api_url, params={**params, "format": "json"},
                                                 headers={"User-Agent": "tgmon/1.0 knowledge-bot"})
                    self._last_request = asyncio.get_event_loop().time()
                    response.raise_for_status()
                    return response.json()
            except Exception as exc:
                last = exc
                await asyncio.sleep(0.5 * (attempt + 1))
        raise last or RuntimeError("Wiki request failed")

    async def category_members(self, category: str, limit: int = 500) -> list[dict[str, Any]]:
        out, cont = [], {}
        while len(out) < limit:
            data = await self._get({"action": "query", "list": "categorymembers",
                                    "cmtitle": category, "cmlimit": min(500, limit-len(out)), **cont})
            out.extend(x for x in data.get("query", {}).get("categorymembers", [])
                       if x.get("ns", 0) == 0)
            cont = data.get("continue") or {}
            if not cont:
                break
        return out[:limit]

    async def find_categories(self, prefix: str, limit: int = 20) -> list[str]:
        """Discover per-wiki category spelling when a configured label is empty."""
        try:
            data = await self._get({"action": "query", "list": "allcategories",
                                    "acprefix": prefix, "aclimit": limit})
            return ["Category:" + str(x.get("*") or x.get("category") or x.get("name"))
                    for x in data.get("query", {}).get("allcategories", [])
                    if x.get("*") or x.get("category") or x.get("name")]
        except Exception:
            return []

    async def page(self, pageid: str | int) -> dict[str, Any]:
        page_key = "pageids" if str(pageid).isdigit() else "titles"
        data = await self._get({"action": "query", "prop": "revisions", page_key: pageid,
                                "rvprop": "ids|content", "rvslots": "main", "formatversion": "2"})
        pages = data.get("query", {}).get("pages", [])
        if isinstance(pages, dict):
            pages = list(pages.values())
        p = pages[0] if pages else {}
        rev = (p.get("revisions") or [{}])[0]
        slot = rev.get("slots", {}).get("main", {}) if isinstance(rev.get("slots"), dict) else {}
        content = (slot.get("content") or slot.get("*") if isinstance(slot, dict) else None) \
            or rev.get("content") or rev.get("*") or ""
        return {"pageid": p.get("pageid", pageid), "title": p.get("title", ""),
                "revid": rev.get("revid", ""), "content": content}

    async def sync(self, source_id: int, categories: dict[str, str] | list[str], limit: int = 500) -> dict[str, int]:
        """Sync category pages. New glossary entries stay pending for review."""
        with session_scope() as s:
            source_row = s.get(KnowledgeSource, source_id)
            if source_row is None:
                raise ValueError("Wiki source does not exist")
            if not source_row.enabled:
                return {"added": 0, "changed": 0, "disabled": 0, "skipped": 1}
        domain = urlparse(self.api_url).netloc or self.api_url
        origin = f"wiki:{domain}"
        seen: set[str] = set(); added = changed = disabled = rejected = 0
        fetched_ids: set[str] = set()
        fetched: list[tuple[str, dict[str, Any]]] = []
        indexed_docs: list[dict[str, Any]] = []
        if isinstance(categories, dict):
            category_items = list(categories.items())
        else:
            category_items = [("other", c if str(c).lower().startswith("category:") else "Category:" + str(c)) for c in categories]
        for cat_name, category in category_items:
            members = await self.category_members(category, limit=limit)
            if not members and cat_name in _CATEGORY_ALIASES:
                # Fandom editors use slightly different labels per game.
                for suffix in _CATEGORY_ALIASES[cat_name]:
                    candidate = "Category:" + suffix
                    if candidate.lower() == str(category).lower():
                        continue
                    members = await self.category_members(candidate, limit=limit)
                    if members:
                        break
            if not members and cat_name == "npc":
                for discovered in await self.find_categories("Non"):
                    if any(k in discovered.lower() for k in ("player", "npc")):
                        members = await self.category_members(discovered, limit=limit)
                        if members:
                            break
            for member in members:
                title = str(member.get("title") or "")
                member_id = str(member.get("pageid") or title)
                if member_id in fetched_ids:
                    continue
                if title.lower().endswith("/list") or title.lower() in {
                    "character", "characters", "agent", "agents", "factions", "faction", "lore", "world"
                }:
                    continue
                page = await self.page(member.get("pageid") or member.get("title"))
                fetched.append((cat_name, parse_page(page, cat_name)))
                fetched_ids.add(member_id)
        with session_scope() as s:
            source = s.get(KnowledgeSource, source_id)
            game = source.game if source else ""
            for cat_name, parsed in fetched:
                pid = parsed["wiki_page_id"]; seen.add(pid)
                digest = hashlib.sha256(parsed["content"].encode()).hexdigest()
                row = s.query(KnowledgePage).filter_by(source_id=source_id, wiki_page_id=pid).first()
                if row is not None and row.reviewed_at and not candidate_problem(row.entity):
                    previous = parse_page({"title": row.title, "content": row.content}, row.category)
                    if row.entity != previous["canonical_zh"]:
                        # A reviewed name edited locally takes precedence over the source.
                        parsed["canonical_zh"] = row.entity
                        parsed["quality_reason"] = candidate_problem(row.entity, parsed["aliases"])
                problem = parsed["quality_reason"]
                old_status = row.status if row is not None else "pending"
                candidate_changed = row is not None and (
                    row.entity != parsed["canonical_zh"] or set(row.aliases or []) != set(parsed["aliases"])
                    or (row.attrs or {}) != parsed["attrs"])
                if row is None:
                    row = KnowledgePage(source_id=source_id, wiki_page_id=pid,
                                        first_seen_at=datetime.utcnow())
                    s.add(row); added += 1
                else:
                    changed += int(row.content_hash != digest)
                row.revision_id = parsed["revision_id"]; row.title = parsed["title"]
                row.content = parsed["content"]; row.content_hash = digest
                row.category = cat_name; row.entity = parsed["canonical_zh"]
                row.attrs = parsed["attrs"]; row.aliases = parsed["aliases"]
                row.last_seen_at = datetime.utcnow()
                row.enabled = True
                if old_status in ("rejected", "disabled"):
                    row.status = old_status
                elif problem:
                    row.status = "rejected"
                    row.review_reason = problem
                    row.reviewed_at = datetime.utcnow()
                    rejected += 1
                elif candidate_changed:
                    row.status = "pending"
                    row.review_reason = None
                else:
                    row.status = old_status
                row.missing_count = 0
                existing_entry = (s.query(GlossaryEntry)
                                  .filter(GlossaryEntry.game == game,
                                          GlossaryEntry.canonical_zh == parsed["canonical_zh"])
                                  .first())
                # A revision of an already-approved entry is staged on the
                # KnowledgePage.  It must not silently add aliases to the
                # active translation glossary before human review.
                if not problem and row.status == "pending" and (existing_entry is None or existing_entry.status == "pending"):
                    upsert_entity(s, game=game, category=cat_name,
                                  canonical_zh=parsed["canonical_zh"],
                                  aliases=[(a, "", "primary") for a in parsed["aliases"]],
                                  origin=origin, origin_ref=pid, attrs=parsed["attrs"],
                                  status="pending", merge_aliases=True)
                s.flush()
                if source and source.trusted and not problem and old_status not in ("rejected", "disabled"):
                    from .wiki_review import review_page
                    review_page(s, row, "active")
                    row.review_reason = "trusted"
                indexed_docs.append({"id": row.id, "title": row.title,
                                     "content": row.content, "game": game})
        with session_scope() as s:
            rows = s.query(KnowledgePage).filter_by(source_id=source_id, enabled=True).all()
            for row in rows:
                if row.wiki_page_id not in seen:
                    row.missing_count = (row.missing_count or 0) + 1
                    if row.missing_count >= 3:
                        row.enabled = False; disabled += 1
            src = s.get(KnowledgeSource, source_id)
            if src:
                src.last_sync_at = datetime.utcnow(); src.last_error = None
        from .service import sync_page_index
        for doc in indexed_docs:
            sync_page_index(doc["id"])
        from ..glossary import touch_version
        touch_version()
        return {"added": added, "changed": changed, "disabled": disabled, "rejected": rejected}


__all__ = ["MediaWikiAdapter", "parse_infobox", "parse_page", "normalize_api_url", "DEFAULT_SOURCES"]
