"""Verify the live release without provider requests or QQ delivery/upload."""
import asyncio
import json
import socket
from unittest.mock import patch

import httpx
from PIL import Image
from sqlalchemy import func

from tgmon import embeddings, glossary, retrieval, settings, translate
from tgmon.admin.deps import COOKIE_NAME, _serializer
from tgmon.admin.routes import qqbot_cb
from tgmon.db import session_scope
from tgmon.models import AdminUser, MessageMedia, MonitorMessage, RetrievalIndex
from tgmon.paths import MEDIA_DIR
from tgmon.providers.base import AIResult
from tgmon.qqbot import client, commands, media
from verify_upgrade import local_model_probe, pages_probe, wiki_probe


def emit(step, result):
    print(json.dumps({"step": step, "result": result}, ensure_ascii=True), flush=True)
    return result


def wiki_pages():
    with session_scope() as s:
        user = s.query(AdminUser).filter_by(role="admin").first()
        cookie = _serializer.dumps({"u": user.username, "r": "admin"})
    result = {}
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=20,
                      cookies={COOKIE_NAME: cookie}) as browser:
        for state in ("pending", "active", "rejected", "disabled"):
            response = browser.get("/kb/wiki/review", params={"wiki_status": state})
            assert response.status_code == 200 and 'id="wiki-review"' in response.text
            result[state] = response.status_code
    return result


async def translation_probe():
    terms = glossary.terms_all()
    term = next(t for t in terms if t.source == "Kafka")

    async def complete(prompt, text):
        assert f"{term.source} → {term.target}" in prompt
        return AIResult(text=f"{term.source} has 120 HP.", provider_name="local-test", model="local-test")

    with patch.object(translate, "complete_with_failover", complete), \
            patch.object(translate, "_cache_get", return_value=None), \
            patch.object(translate, "_cache_put"), patch.object(translate, "record_usage"), \
            patch.object(glossary, "bump_hits"):
        result = await translate.translate_with_kb(f"{term.source} has 120 HP.", game=term.game)
    assert result.status == "ok" and term.target in result.text_zh and "120" in result.text_zh
    assert term.source in result.glossary_corrected
    return {"source": term.source, "target": term.target, "result": result.text_zh,
            "corrected": result.glossary_corrected, "external_provider_called": False}


async def album_probe():
    with session_scope() as s:
        grouped = (s.query(MessageMedia.message_id, func.count()).filter(
            MessageMedia.kind == "photo", MessageMedia.thumb_path.isnot(None))
            .group_by(MessageMedia.message_id).having(func.count() > 1).order_by(func.count().desc()).all())
        chosen, photos = None, []
        for mid, _ in grouped:
            paths = [p[0] for p in s.query(MessageMedia.thumb_path).filter_by(
                message_id=mid, kind="photo").order_by(MessageMedia.id) if p[0]]
            if all((MEDIA_DIR / path).is_file() for path in paths):
                chosen, photos = mid, paths
                break
    assert chosen is not None, "No complete album available"
    composite = media.compose_album(photos)
    with Image.open(MEDIA_DIR / composite) as picture:
        dimensions = picture.size
        assert picture.format == "JPEG" and picture.height <= 16000
    outgoing = []

    async def capture(path, body):
        outgoing.append(body)
        return "local-only"

    async def upload(*args, **kwargs):
        return "local-upload-placeholder"

    with patch.object(client, "_post_message", capture), patch.object(media, "upload_album", upload):
        await qqbot_cb._passive_reply({"replies": commands._cmd_peek(f"/peek {chosen}"),
                                      "channel": "group", "target": "local-test",
                                      "msg_id": "wiki-release-verification"}, source="validation")
    delivered = [body for body in outgoing if body["msg_type"] == 7]
    assert len(delivered) == 1 and delivered[0].get("content")
    return {"source_images": len(photos), "dimensions": dimensions,
            "media_messages_in_dry_run": len(delivered), "sent_to_group": False,
            "external_qq_upload_called": False}


async def main():
    report = {"pages": emit("pages", pages_probe()), "wiki": emit("wiki", wiki_probe()),
              "review_pages": emit("review_pages", wiki_pages()),
              "embedding": emit("embedding", local_model_probe())}
    with session_scope() as s:
        rows = s.query(RetrievalIndex).filter(func.length(func.trim(RetrievalIndex.text)) > 0)
        total = rows.count()
        ready = rows.filter(RetrievalIndex.embedding.isnot(None),
                            RetrievalIndex.embedding_model == embeddings.MODEL_ID).count()
        message = s.query(MonitorMessage).filter(func.length(MonitorMessage.text_zh) >= 12).first()
        assert message is not None
        needle, mid = message.text_zh.strip()[:12], message.id
    report["index"] = emit("index", {"documents": total, "vectors": ready, "pending": total - ready})
    assert total and total == ready, (total, ready)
    hits = retrieval.search(needle, limit=100)
    assert any(hit.ref_type == "message" and hit.ref_id == mid for hit in hits)
    report["search"] = emit("search", {"existing_message_found": True})
    with patch.object(socket.socket, "connect", side_effect=AssertionError("Unexpected network")), \
            patch.object(socket.socket, "connect_ex", side_effect=AssertionError("Unexpected network")):
        report["translation"] = emit("translation", await translation_probe())
        report["album"] = emit("album", await album_probe())
    print(json.dumps(report, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
