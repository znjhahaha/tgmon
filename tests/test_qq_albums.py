"""One complete album per QQ media message, including command and push paths."""
from datetime import datetime

import pytest
from PIL import Image

from tgmon import paths, qqbot, settings
from tgmon.admin.routes import qqbot_cb
from tgmon.db import engine, session_scope
from tgmon.models import Base, Channel, MessageMedia, MonitorMessage, QqGroup
from tgmon.qqbot import client, commands, media


@pytest.fixture(autouse=True)
def database():
    Base.metadata.create_all(engine)
    previous = settings.get("BASE_URL")
    settings.set_many({"BASE_URL": "https://example.com"})
    qqbot_cb._REPLIED.clear()
    yield
    with session_scope() as s:
        from tgmon.models import QqEvent, QqPending, QqDelivery, ShareToken
        for model in (QqEvent, QqPending, QqDelivery, ShareToken):
            s.query(model).delete()
        s.query(MessageMedia).filter(MessageMedia.message_id == 996001).delete()
        s.query(MonitorMessage).filter(MonitorMessage.id == 996001).delete()
        s.query(Channel).filter(Channel.id == 996001).delete()
        s.query(QqGroup).filter(QqGroup.group_openid == "album-group").delete()
    qqbot_cb._REPLIED.clear()
    settings.set_many({"BASE_URL": previous or ""})


@pytest.fixture
def album(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "MEDIA_DIR", tmp_path)
    from tgmon import sharing
    monkeypatch.setattr(sharing, "MEDIA_DIR", tmp_path)
    names = []
    colors = [(220, 20, 20), (20, 180, 20), (20, 20, 220), (220, 160, 20), (160, 20, 160)]
    for i, color in enumerate(colors):
        name = f"image-{i}.png"
        Image.new("RGB", (160, 120), color).save(tmp_path / name)
        names.append(name)
    return names, colors


def test_album_render_keeps_every_image_and_order(album):
    names, colors = album
    relative = media.compose_album(names)
    with Image.open(paths.MEDIA_DIR / relative) as image:
        assert image.format == "JPEG"
        assert image.height > 120 * len(names)
        # Each source's color must occupy a band in original order.
        column = [image.getpixel((image.width // 2, y)) for y in range(image.height)]
        previous = -1
        for color in colors:
            positions = [y for y, got in enumerate(column)
                         if sum(abs(a - b) for a, b in zip(got, color)) < 25]
            assert positions and min(positions) > previous
            previous = max(positions)


def test_album_render_is_bounded_without_cropping(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "MEDIA_DIR", tmp_path)
    im = Image.new("RGB", (100, 10000), "red")
    im.paste(Image.new("RGB", (100, 1000), "blue"), (0, 9000))
    im.save(tmp_path / "tall.png")
    result = media.compose_album(["tall.png", "tall.png"])
    with Image.open(tmp_path / result) as image:
        assert image.height <= 16000 and image.width <= 1200
        r, g, b = image.getpixel((image.width // 2, image.height - 20))
        assert b > 150 and r < 80


def test_album_missing_image_fails_instead_of_silently_sending_partial(album):
    names, _ = album
    with pytest.raises(ValueError):
        media.compose_album(names + ["missing.png"])


def test_album_rejects_path_outside_public_media(album):
    names, _ = album
    with pytest.raises(ValueError):
        media.compose_album(names + ["../outside.png"])
    (paths.MEDIA_DIR / "private").mkdir()
    Image.new("RGB", (10, 10)).save(paths.MEDIA_DIR / "private" / "secret.png")
    with pytest.raises(ValueError):
        media.compose_album(names + ["private/secret.png"])


def test_album_caption_is_kept_in_conversation_memory():
    from tgmon import memory
    from tgmon.models import Conversation, ConversationTurn

    previous = settings.get("MEMORY_ENABLED")
    settings.set_many({"MEMORY_ENABLED": True})
    try:
        commands._record_conversation("album-memory-group", "album-memory-member", "/peek 1", [
            commands.Reply(kind="image", text="卡芙卡的卡池说明", thumb_paths=["1.webp", "2.webp"])])
        context = memory.load_context("album-memory-group", "album-memory-member")
        assert any(turn["role"] == "assistant" and turn["content"] == "卡芙卡的卡池说明"
                   for turn in context["recent"])
    finally:
        with session_scope() as s:
            ids = [row[0] for row in s.query(Conversation.id).filter(
                Conversation.scope_id.in_(["album-memory-group", "album-memory-member"]))]
            s.query(ConversationTurn).filter(ConversationTurn.conversation_id.in_(ids)).delete()
            s.query(Conversation).filter(Conversation.id.in_(ids)).delete()
        settings.set_many({"MEMORY_ENABLED": previous})


@pytest.mark.asyncio
async def test_album_upload_sends_one_complete_composite(album, monkeypatch):
    names, _ = album
    uploaded = []

    async def upload(group, path, raise_fatal=False):
        uploaded.append(path)
        with Image.open(paths.MEDIA_DIR / path) as image:
            assert image.height > 500
        return "album-file-info"

    monkeypatch.setattr(media, "upload_image", upload)
    assert await media.upload_album("group", names) == "album-file-info"
    assert len(uploaded) == 1


@pytest.mark.asyncio
async def test_single_photo_keeps_existing_upload_path(monkeypatch):
    uploaded = []

    async def upload(group, path, raise_fatal=False):
        uploaded.append((path, raise_fatal))
        return "single"

    monkeypatch.setattr(media, "upload_image", upload)
    assert await media.upload_album("group", ["one.webp"], raise_fatal=True) == "single"
    assert uploaded == [("one.webp", True)]


@pytest.mark.parametrize("command", ["/latest 1", "/game 原神 1", "/peek 996001", "/new"])
@pytest.mark.asyncio
async def test_commands_preserve_full_album_in_one_reply(command):
    names = [f"image-{i}.webp" for i in range(10)]
    with session_scope() as s:
        s.add(Channel(id=996001, title="album", game="原神", tg_id="qq-album-test"))
        s.add(MonitorMessage(id=996001, channel_id=996001, tg_message_id="1",
                             text_raw="完整正文", game_detected="原神", published_at=datetime.utcnow()))
        s.add_all([MessageMedia(message_id=996001, kind="photo", thumb_path=name) for name in names])
    replies = await commands._dispatch(command, "album-group", True)
    images = [r for r in replies if r.kind == "image"]
    assert len(images) == 1
    from tgmon.models import ShareToken
    with session_scope() as s:
        snapshot = s.get(ShareToken, images[0].bundle_id).snapshot_items
        assert snapshot[0]["photos"] == names
        assert snapshot[0]["text"] == "完整正文"
    assert images[0].text.startswith("https://example.com/s/")
    assert "完整正文" not in commands.replies_text(replies)


@pytest.mark.asyncio
async def test_passive_album_caption_and_images_are_one_api_message(album, monkeypatch):
    names, _ = album
    posted = []

    async def upload(group, path, raise_fatal=False):
        with Image.open(paths.MEDIA_DIR / path) as image:
            assert image.height > 500
        return "combined"

    async def post(path, body):
        posted.append(body)
        return "sent"

    monkeypatch.setattr(media, "upload_image", upload)
    monkeypatch.setattr(client, "_post_message", post)
    result = {"replies": [commands.Reply(kind="image", text="整组说明", thumb_paths=names)],
              "channel": "group", "target": "group", "msg_id": "album-source"}
    await qqbot_cb._passive_reply(result, source="test")
    images = [body for body in posted if body["msg_type"] == 7]
    assert len(images) == 1
    assert images[0]["content"] == "整组说明"
    assert images[0]["media"] == {"file_info": "combined"}
    assert len(posted) <= 5


@pytest.mark.asyncio
async def test_push_album_counts_as_one_daily_message(album, monkeypatch):
    names, _ = album
    posted = []

    async def upload(group, path, raise_fatal=False):
        return "album"

    async def post(path, body):
        posted.append(body)
        return "sent"

    monkeypatch.setattr(media, "upload_image", upload)
    monkeypatch.setattr(client, "_post_message", post)
    previous = {key: settings.get(key) for key in ("QQ_ENABLED", "QQ_DAILY_LIMIT", "QQ_CHANNEL_IDS")}
    settings.set_many({"QQ_ENABLED": True, "QQ_DAILY_LIMIT": 1, "QQ_CHANNEL_IDS": [996001]})
    with session_scope() as s:
        s.add(Channel(id=996001, title="album", game="原神", tg_id="qq-album-test"))
        s.add(MonitorMessage(id=996001, channel_id=996001, tg_message_id="1", text_raw="",
                             game_detected="原神", published_at=datetime.utcnow()))
        s.add_all([MessageMedia(message_id=996001, kind="photo", thumb_path=name) for name in names])
        s.add(QqGroup(group_openid="album-group", enabled=True))
    try:
        assert await qqbot.push_message(996001) >= 1
        from datetime import timedelta
        from tgmon.qqbot.batches import flush_pending
        await flush_pending(datetime.utcnow() + timedelta(seconds=61))
        assert len(posted) == 1 and posted[0]["msg_type"] == 7
        with session_scope() as s:
            assert s.query(QqGroup).filter_by(group_openid="album-group").one().sent_today == 1
    finally:
        settings.set_many(previous)
