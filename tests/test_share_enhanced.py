from __future__ import annotations

import pytest

from tgmon import settings
from tgmon.admin.routes import share as share_route
from tgmon.db import session_scope
from tgmon.models import Base, Channel, MonitorMessage, ShareToken
from tgmon.db import engine


class _Req:
    base_url = "https://example.com/"

    async def form(self):
        return {}


@pytest.fixture(autouse=True)
def db():
    Base.metadata.create_all(engine)
    settings.set_many({"BASE_URL": "https://example.com"})
    with session_scope() as s:
        s.add(Channel(id=991, title="分享测试", tg_id="-100991"))
        s.add(MonitorMessage(id=991, channel_id=991, tg_message_id="991",
                             text_raw="原文", text_zh="译文"))
    yield
    with session_scope() as s:
        s.query(ShareToken).delete()
        s.query(MonitorMessage).filter(MonitorMessage.id == 991).delete()
        s.query(Channel).filter(Channel.id == 991).delete()
    settings.invalidate()


@pytest.mark.asyncio
async def test_create_share_contains_one_click_copy_text(monkeypatch):
    async def fake_summary(texts, single):
        return "这是分享摘要"

    monkeypatch.setattr(share_route, "_gen_summary", fake_summary)
    response = await share_route.create_share(991, _Req(), "48h", "tester")
    html = response.body.decode()
    assert "复制分享文案" in html
    assert "这是分享摘要" in html
    assert "https://example.com/s/" in html

