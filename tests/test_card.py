from __future__ import annotations

from PIL import Image

from tgmon import card


def test_long_card_wraps_full_summary_and_keeps_all_media(monkeypatch):
    images = {
        "a": Image.new("RGB", (320, 180), "red"),
        "b": Image.new("RGB", (180, 320), "blue"),
        "c": Image.new("RGB", (200, 200), "green"),
    }

    monkeypatch.setattr(card, "_load_thumb", lambda path, media_dir: images[path].copy())
    pages = card.render_long_cards(
        [{
            "game": "崩坏:星穹铁道",
            "channel": "测试频道",
            "date_str": "09-05 12:00",
            "version": "4.6",
            "text": "第一条消息正文，包含角色和版本信息。" * 5,
            "raw": "Original text",
            "thumb_paths": ["a", "b", "c"],
        }],
        summary="这是一个很长的摘要。" * 80,
        url="https://example.com/s/demo",
        max_height=1800,
    )

    assert len(pages) >= 2
    assert all(page[:2] == b"\xff\xd8" for page in pages)


def test_render_card_remains_backward_compatible(monkeypatch):
    monkeypatch.setattr(
        card,
        "_load_thumb",
        lambda path, media_dir: Image.new("RGB", (100, 100), "white"),
    )
    data = card.render_card(
        game="原神",
        channel="测试",
        summary="摘要",
        url="https://example.com/s/x",
        thumb_paths=["a"],
    )
    assert data[:2] == b"\xff\xd8"
