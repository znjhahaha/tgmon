"""Validate an upgrade on a DB copy, or verify the running deployment.

The online mode makes a real translation and QQ file upload (srv_send_msg=False).
It captures message delivery locally and never sends a message to a QQ group.
No credentials, session cookies or QQ file_info values are printed.
"""
import argparse
import asyncio
import json
import socket
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import httpx
import numpy as np
from PIL import Image
from sqlalchemy import func

from tgmon import embeddings, glossary, retrieval, settings
from tgmon.bootstrap import init_all
from tgmon.db import session_scope
from tgmon.models import (AdminUser, Channel, GlossaryEntry, MessageMedia,
                          MonitorMessage, QqGroup, RetrievalIndex, KnowledgePage)
from tgmon.paths import MEDIA_DIR


def counts():
    with session_scope() as s:
        return {model.__tablename__: s.query(func.count()).select_from(model).scalar()
                for model in (Channel, MonitorMessage, MessageMedia, GlossaryEntry)}


def local_model_probe():
    start = time.perf_counter()
    with patch.object(socket.socket, "connect", side_effect=AssertionError("Unexpected network")):
        query = embeddings.embed_query("卡芙卡什么时候再次进入卡池")
        documents = embeddings.embed_documents([
            "卡芙卡即将在新版本复刻，下期开放限定角色卡池。",
            "电脑散热器风扇故障，更换电源线后开机。",
        ])
    assert query is not None and all(vector is not None for vector in documents)
    scores = [float(np.dot(query, vector)) for vector in documents]
    assert scores[0] > scores[1] and scores[0] >= 0.45, scores
    return {"model": embeddings.MODEL_NAME, "dimensions": len(query),
            "network_disabled": True, "scores": scores,
            "seconds": round(time.perf_counter() - start, 3)}


async def build_index():
    for _ in range(100):
        if not retrieval.index_pending(128):
            break
    for _ in range(100):
        if not await retrieval.embed_pending(128):
            break
    with session_scope() as s:
        query = s.query(RetrievalIndex).filter(func.length(func.trim(RetrievalIndex.text)) > 0)
        total = query.count()
        ready = query.filter(RetrievalIndex.embedding.isnot(None),
                             RetrievalIndex.embedding_model == embeddings.MODEL_ID).count()
    assert total and ready == total, (total, ready)
    return {"documents": total, "vectors": ready}


async def preflight():
    before = counts()
    with session_scope() as s:
        wiki_before = dict(s.query(KnowledgePage.status, func.count(KnowledgePage.id))
                           .group_by(KnowledgePage.status).all())
    init_all()
    wiki_after = wiki_probe()
    init_all()  # schema/seed migration must be idempotent
    assert wiki_after == wiki_probe(), "Wiki migration is not idempotent"
    after = counts()
    for table in ("channel", "monitor_message", "message_media"):
        assert before[table] == after[table], (table, before, after)
    assert after["glossary_entry"] >= before["glossary_entry"]
    settings.set_many({"EMBEDDING_ENABLED": True, "RETRIEVAL_ENABLED": True})
    from tgmon.providers.registry import load_configs
    configs = load_configs()
    assert configs and all(config.api_key for config in configs), "Existing provider credentials unavailable"
    assert settings.get("QQ_APP_SECRET"), "Existing QQ credential unavailable"
    report = {"mode": "preflight", "counts_before": before, "counts_after": after,
              "wiki_before": wiki_before, "wiki_after": wiki_after,
              "credentials_preserved": True, "embedding": local_model_probe(),
              "index": await build_index()}
    print(json.dumps(report, ensure_ascii=True))


def wiki_probe():
    from tgmon.kb.wiki_review import candidate_problem, page_aliases
    with session_scope() as s:
        rows = s.query(KnowledgePage).all()
        statuses = {}
        for row in rows:
            statuses[row.status] = statuses.get(row.status, 0) + 1
            if row.status in ("pending", "active"):
                assert not candidate_problem(row.entity, page_aliases(row)), row.id
        invalid_entries = [entry.id for entry in s.query(GlossaryEntry).filter(
            GlossaryEntry.origin.like("wiki:%"), GlossaryEntry.status.in_(("pending", "active")))
            if candidate_problem(entry.canonical_zh)]
        assert not invalid_entries, invalid_entries[:10]
        entry_statuses = dict(s.query(GlossaryEntry.status, func.count(GlossaryEntry.id))
                              .filter(GlossaryEntry.origin.like("wiki:%"))
                              .group_by(GlossaryEntry.status).all())
    return {"pages": len(rows), "statuses": statuses, "entry_statuses": entry_statuses,
            "invalid_candidates": 0}


def pages_probe():
    from tgmon.admin.deps import COOKIE_NAME, _serializer
    with session_scope() as s:
        user = s.query(AdminUser).filter(AdminUser.role == "admin").first()
        assert user is not None
        cookie = _serializer.dumps({"u": user.username, "r": "admin"})
    pages = {}
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=30,
                      cookies={COOKIE_NAME: cookie}) as client:
        for path in ("/healthz", "/", "/messages", "/system", "/qqbot", "/kb", "/shares"):
            response = client.get(path)
            pages[path] = response.status_code
            assert response.status_code == 200, (path, response.status_code)
            if path == "/system":
                assert "本地语义检索" in response.text
                assert 'name="embedding_provider"' not in response.text
            if path == "/qqbot":
                assert "/translate" in response.text and "整组图片" in response.text
    return pages


async def online():
    from tgmon.qqbot import client, commands, media
    from tgmon.admin.routes import qqbot_cb
    from tgmon.translate import translate_with_kb

    assert settings.get("EMBEDDING_ENABLED") and settings.get("RETRIEVAL_ENABLED")
    report = {"mode": "online", "base_url": settings.get("BASE_URL"),
              "verified_at_utc": datetime.utcnow().isoformat(),
              "pages": pages_probe(), "embedding": local_model_probe(),
              "index": await build_index()}
    with session_scope() as s:
        message = (s.query(MonitorMessage).filter(MonitorMessage.text_zh.isnot(None))
                   .order_by(MonitorMessage.published_at.desc()).first())
        assert message is not None
        mid = message.id
        needle = message.text_zh.strip()[:12]
    assert any(hit.ref_type == "message" and hit.ref_id == mid for hit in retrieval.search(needle, limit=100))
    report["search"] = {"message_id": mid, "exact_match_found": True}

    terms = glossary.terms_all()
    term = next((t for t in terms if t.source == "Kafka"), None)
    if term is None:
        term = next((t for t in terms if t.category == "character" and t.source.isascii()), None)
    assert term is not None, "No reviewed character term available for translation test"
    result = await translate_with_kb(f"{term.source} has 120 HP.", game=term.game)
    assert result.status == "ok" and term.target in result.text_zh, (result.status, result.glossary_miss)
    assert "120" in result.text_zh
    report["translation"] = {"source_term": term.source, "expected_term": term.target,
                              "text": result.text_zh, "glossary_miss": result.glossary_miss,
                              "provider": result.provider_name, "from_cache": result.from_cache}

    with session_scope() as s:
        grouped = (s.query(MessageMedia.message_id, func.count())
                   .filter(MessageMedia.kind == "photo", MessageMedia.thumb_path.isnot(None))
                   .group_by(MessageMedia.message_id).having(func.count() > 1)
                   .order_by(func.count().desc()).all())
        album_mid, photos = None, []
        for candidate, _ in grouped:
            paths = [p[0] for p in s.query(MessageMedia.thumb_path).filter_by(
                message_id=candidate, kind="photo").order_by(MessageMedia.id).all() if p[0]]
            if all((MEDIA_DIR / p).is_file() for p in paths):
                album_mid, photos = candidate, paths
                break
        group = s.query(QqGroup).filter(QqGroup.enabled.is_(True)).first()
        assert group is not None and album_mid is not None, "No live group/album available"
        openid = group.group_openid
    composite = media.compose_album(photos)
    with Image.open(MEDIA_DIR / composite) as image:
        dimensions = image.size
        assert image.format == "JPEG" and image.height <= 16000
    file_info = await media.upload_album(openid, photos)
    assert file_info, "QQ platform did not accept the complete album upload"
    outgoing = []

    async def capture(path, body):
        outgoing.append(body)
        return "local-validation-only"

    async def already_uploaded(*args, **kwargs):
        return file_info

    replies = commands._cmd_peek(f"/peek {album_mid}")
    with patch.object(client, "_post_message", capture), patch.object(media, "upload_album", already_uploaded):
        await qqbot_cb._passive_reply({"replies": replies, "channel": "group", "target": openid,
                                      "msg_id": "deployment-verification"}, source="validation")
    delivered = [body for body in outgoing if body["msg_type"] == 7]
    assert len(delivered) == 1 and delivered[0].get("content")
    report["album"] = {"message_id": album_mid, "source_images": len(photos),
                        "composite": composite, "dimensions": dimensions,
                        "platform_upload_accepted": True, "media_messages_in_dry_run": len(delivered),
                        "sent_to_group": False}
    print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "online"))
    args = parser.parse_args()
    asyncio.run(preflight() if args.mode == "preflight" else online())
