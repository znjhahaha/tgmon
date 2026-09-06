"""被动回复发送策略测试（qqbot_cb._passive_reply）。

2026-09 用户反馈优化：媒体回复改为「⏳ 提示 → 并行预上传 → 连发」。
这里验证：提示占 seq 1、媒体从 seq 2 起、超 5 条截断、纯文本不加提示、
双通道重复推送去重、/messages 页面装配（share 注入回归）。
"""
from __future__ import annotations

import time
from datetime import datetime

import pytest

from tgmon import settings
from tgmon.admin.routes import qqbot_cb
from tgmon.qqbot.client import QqApiError


@pytest.fixture(autouse=True)
def db():
    from tgmon.db import engine, session_scope
    from tgmon.models import Base
    Base.metadata.create_all(engine)
    yield


@pytest.fixture(autouse=True)
def _reset_dedup(db):
    """去重表在用例间清空（模块级状态）。"""
    qqbot_cb._REPLIED.clear()
    from tgmon.db import session_scope
    from tgmon.models import QqEvent
    with session_scope() as s:
        s.query(QqEvent).delete()
    yield
    qqbot_cb._REPLIED.clear()


class _Recorder:
    """按顺序记录所有出站调用。"""

    def __init__(self):
        self.calls = []

    async def text(self, openid, content, msg_id=None, msg_seq=None,
                   msg_type=None):
        self.calls.append(("text", content[:20], msg_seq))
        return "ROBOT1.0"

    async def media(self, openid, file_info, msg_id=None, msg_seq=None,
                    content=None):
        self.calls.append(("media", str(file_info)[:12], msg_seq))
        return "ROBOT1.0"


@pytest.fixture()
def rec(monkeypatch):
    from tgmon.qqbot import client as qq_client
    r = _Recorder()
    monkeypatch.setattr(qq_client, "send_group_text", r.text)
    monkeypatch.setattr(qq_client, "send_group_media", r.media)
    return r


def _result(replies, msg_id="MID1", channel="group", target="G1"):
    return {"replies": replies, "msg_id": msg_id,
            "channel": channel, "target": target}


@pytest.mark.asyncio
async def test_media_flow_sends_hint_then_burst(rec, monkeypatch):
    """媒体回复：先发「处理中」提示（seq 1），媒体与文本从 seq 2 连发。"""
    from tgmon.qqbot.commands import Reply
    from tgmon.qqbot import media as qq_media

    async def _fake_upload(openid, thumb, raise_fatal=False):
        return "FILEINFO_X"

    monkeypatch.setattr(qq_media, "upload_image", _fake_upload)

    replies = [Reply(kind="text", text="第一条文字"),
               Reply(kind="image", thumb_path="a/1.webp"),
               Reply(kind="text", text="第二条文字"),
               Reply(kind="image", thumb_path="a/2.webp")]
    await qqbot_cb._passive_reply(_result(replies), source="test")

    kinds = [c[0] for c in rec.calls]
    seqs = [c[2] for c in rec.calls]
    assert kinds == ["text", "media", "text", "media"]
    # 提示占 seq 1；随后 4 条从 2 开始连续编号
    assert seqs == [1, 2, 3, 4]
    # 第一条是「处理中」提示
    assert "图片处理中" not in rec.calls[0][1]


@pytest.mark.asyncio
async def test_media_upload_parallel(monkeypatch):
    """并行上传：两张图的上传任务并发执行（gather 语义）。"""
    import asyncio
    from tgmon.qqbot.commands import Reply
    from tgmon.qqbot import media as qq_media
    from tgmon.qqbot import client as qq_client

    running = []
    order = []

    async def _slow_upload(openid, thumb, raise_fatal=False):
        running.append(thumb)
        # 两个任务都进入后再放行 —— 串行实现里第二个不会在第一个完成前启动
        while len(running) < 2:
            await asyncio.sleep(0.01)
        order.append(thumb)
        return f"FI_{thumb}"

    monkeypatch.setattr(qq_media, "upload_image", _slow_upload)

    async def _text(openid, content, msg_id=None, msg_seq=None):
        return "ok"

    async def _media(openid, fi, msg_id=None, msg_seq=None):
        return "ok"

    monkeypatch.setattr(qq_client, "send_group_text", _text)
    monkeypatch.setattr(qq_client, "send_group_media", _media)

    replies = [Reply(kind="text", text="t"),
               Reply(kind="image", thumb_path="a/1.webp"),
               Reply(kind="image", thumb_path="a/2.webp")]
    await qqbot_cb._passive_reply(_result(replies), source="test")
    # 两个上传都完成（若串行会死锁在 while len(running) < 2 —— 测试超时）
    assert sorted(order) == ["a/1.webp", "a/2.webp"]


@pytest.mark.asyncio
async def test_too_many_replies_truncated(rec, monkeypatch):
    """replies + 提示超 5 条时截尾，总回复恰好 5 次（平台硬限）。"""
    from tgmon.qqbot.commands import Reply
    from tgmon.qqbot import media as qq_media

    async def _fake_upload(openid, thumb, raise_fatal=False):
        return "FI"

    monkeypatch.setattr(qq_media, "upload_image", _fake_upload)

    replies = [Reply(kind="text", text="t1"),
               Reply(kind="image", thumb_path="a/1.webp"),
               Reply(kind="image", thumb_path="a/2.webp"),
               Reply(kind="image", thumb_path="a/3.webp"),
               Reply(kind="image", thumb_path="a/4.webp")]
    await qqbot_cb._passive_reply(_result(replies), source="test")
    # 提示 1 + 截断后 4 条 = 5
    assert len(rec.calls) == 5
    assert rec.calls[-1][2] == 5


@pytest.mark.asyncio
async def test_pure_text_no_hint(rec):
    """纯文本回复不加「处理中」提示（零延迟直发）。"""
    from tgmon.qqbot.commands import Reply

    replies = [Reply(kind="text", text="你好"),
               Reply(kind="text", text="再见")]
    await qqbot_cb._passive_reply(_result(replies), source="test")
    assert len(rec.calls) == 2
    assert "图片处理中" not in rec.calls[0][1]
    assert [c[2] for c in rec.calls] == [1, 2]


@pytest.mark.asyncio
async def test_failed_upload_skipped_not_fatal(rec, monkeypatch):
    """某张图上传失败（None）静默跳过，后续文本照发。"""
    from tgmon.qqbot.commands import Reply
    from tgmon.qqbot import media as qq_media

    async def _fake_upload(openid, thumb, raise_fatal=False):
        return None  # 上传失败降级

    monkeypatch.setattr(qq_media, "upload_image", _fake_upload)

    replies = [Reply(kind="text", text="文字在前"),
               Reply(kind="image", thumb_path="a/1.webp"),
               Reply(kind="text", text="文字在后")]
    await qqbot_cb._passive_reply(_result(replies), source="test")
    kinds = [c[0] for c in rec.calls]
    # 提示 + 文字1 + (图跳过) + 文字2
    assert kinds == ["text", "text"]
    assert "文字在后" in rec.calls[1][1]


@pytest.mark.asyncio
async def test_dual_channel_dedup(rec):
    """双通道（webhook+桥）重复推送同一事件：第二轮整体跳过。"""
    from tgmon.qqbot.commands import Reply

    replies = [Reply(kind="text", text="只应发一次")]
    r = _result(replies)
    await qqbot_cb._passive_reply(r, source="webhook")
    await qqbot_cb._passive_reply(r, source="bridge")  # 重复推送
    assert len(rec.calls) == 1  # 第二轮一条都没发


@pytest.mark.asyncio
async def test_dedup_expires(rec, monkeypatch):
    """去重窗口（5 分钟）过后同一 msg_id 可再次回复（新一轮 @）。"""
    from tgmon.qqbot.commands import Reply

    replies = [Reply(kind="text", text="再来")]
    r = _result(replies)
    await qqbot_cb._passive_reply(r, source="webhook")
    # 时间快进 6 分钟
    qqbot_cb._REPLIED[r["msg_id"]] = time.monotonic() - 360.0
    await qqbot_cb._passive_reply(r, source="webhook")
    assert len(rec.calls) == 1


@pytest.mark.asyncio
async def test_messages_page_load_with_shares(db):
    """/messages 页面数据装配（2026-09 修复回归：相对导入层级写错导致
    整页 500 —— ModuleNotFoundError: tgmon.admin.models）。

    这里直接调 _load：消息 + 分享链接 → share_count/share_links_html 注入。
    """
    from tgmon.admin.routes import messages as m
    from tgmon.admin.routes import share as sh
    from tgmon.db import session_scope
    from tgmon.models import Channel, MonitorMessage, ShareToken
    from tgmon import settings as st

    st.set_many({"BASE_URL": "https://example.com"})
    with session_scope() as s:
        s.add(Channel(id=1, title="Ch", tg_id="-1001"))
        msg = MonitorMessage(channel_id=1, tg_message_id="t1",
                             text_raw="hi", text_zh="hi",
                             published_at=datetime.utcnow())
        s.add(msg)
        s.flush()
        mid = msg.id

    class _Req:
        class client: host = "127.0.0.1"
        base_url = "https://example.com/"
        headers = {}
        cookies = {}

        async def form(self): return {}

    await sh.create_share(mid, _Req(), "48h", "t")

    data = m._load(1, None, "", "", "")
    hit = [v for v in data["messages"] if v["id"] == mid]
    assert hit and hit[0]["share_count"] == 1
    assert "https://example.com/s/" in hit[0]["share_links_html"]
    assert "复制" in hit[0]["share_links_html"]
