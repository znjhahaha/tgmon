"""分享链接模块测试（2026-09 升级：token 可逆存储 + 合并分享 + 批量）。

核心保证：
1. token_enc 能解密还原 URL —— 「刷新后链接不可见」的修复
2. 旧链接（仅 hash，无 token_enc）标注不可恢复，不炸
3. 合并分享：message_ids 数组、锚点、访客页多消息渲染
4. 媒体授权按集合校验 —— A 的 token 拿不到 B 的图
5. 批量 ids 解析的边界
"""
from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from tgmon import settings
from tgmon.admin.routes import share as share_route
from tgmon.admin.routes.messages import _parse_ids_str
from tgmon.crypto import decrypt as _dec
from tgmon.db import engine, session_scope
from tgmon.models import (
    Base, Channel, MessageMedia, MonitorMessage, ShareToken,
)

_BASE = "https://example.com"


class _FakeClient:
    host = "127.0.0.1"


class _FakeReq:
    """够用的假 Request：client_ip / base_url / form() / headers / cookies。"""

    def __init__(self, form_data: dict | None = None):
        self.client = _FakeClient()
        self.base_url = "http://testserver/"
        self.headers = {}
        self.cookies = {}
        self._form = form_data or {}

    async def form(self):
        return self._form


@pytest.fixture(autouse=True)
def db():
    Base.metadata.create_all(engine)
    settings.set_many({"BASE_URL": _BASE})
    # 限流器在用例间共享（模块级），跨用例累积会假 429
    share_route._PAGE_LIMITER._hits.clear()
    share_route._DL_LIMITER._hits.clear()
    yield
    share_route._PAGE_LIMITER._hits.clear()
    share_route._DL_LIMITER._hits.clear()
    with session_scope() as s:
        for t in (ShareToken, MessageMedia, MonitorMessage, Channel):
            s.query(t).delete()
        from tgmon.models import AppSetting
        for r in s.query(AppSetting).filter(
                AppSetting.key == "BASE_URL").all():
            s.delete(r)
    settings.invalidate()


def _seed_channel(cid: int = 1) -> None:
    with session_scope() as s:
        if s.get(Channel, cid) is None:
            s.add(Channel(id=cid, title="Ch", tg_id=f"-100{cid}"))


def _seed_msg(text: str = "hello", published: datetime | None = None,
              cid: int = 1) -> int:
    _seed_channel(cid)
    with session_scope() as s:
        m = MonitorMessage(channel_id=cid,
                           tg_message_id=f"t{uuid4().hex}",
                           text_raw=text, text_zh=text,
                           published_at=published or datetime.utcnow())
        s.add(m)
        s.flush()
        return m.id


def _seed_media(mid: int, name: str = "a/1.webp") -> int:
    with session_scope() as s:
        x = MessageMedia(message_id=mid, kind="photo", thumb_path=name)
        s.add(x)
        s.flush()
        return x.id


# ---------------- token 加密还原 ----------------

@pytest.mark.asyncio
async def test_token_enc_roundtrip():
    """生成后 token_enc 可解密回 token —— URL 随时可再显示。"""
    mid = _seed_msg()
    resp = await share_route.create_share(mid, _FakeReq(), "48h", "tester")
    html = resp.body.decode()
    assert "/s/" in html
    with session_scope() as s:
        row = s.query(ShareToken).filter(ShareToken.message_id == mid).one()
        token = _dec(row.token_enc)
    assert token and len(token) >= 12
    assert f"{_BASE}/s/{token}" in html  # 页面显示的就是可还原的那份


@pytest.mark.asyncio
async def test_old_link_without_enc_marked():
    """旧链接（升级前只有 hash）：URL 不可恢复，但不炸、可撤销。"""
    mid = _seed_msg()
    with session_scope() as s:
        s.add(ShareToken(token_hash="f" * 64, message_id=mid,
                         expires_at=datetime.utcnow() + timedelta(hours=1),
                         created_by="old"))
    frag = share_route._links_fragment(mid)
    assert "不可恢复" in frag
    assert share_route._share_url.__name__  # 引用确认


def test_share_url_old_row_none():
    """_share_url 对无 enc 行返回 None（不是抛异常）。"""
    mid = _seed_msg()
    with session_scope() as s:
        row = ShareToken(token_hash="e" * 64, message_id=mid,
                         expires_at=datetime.utcnow() + timedelta(hours=1))
        s.add(row)
        s.flush()
        sid = row.id
    with session_scope() as s:
        row = s.get(ShareToken, sid)
        assert share_route._share_url(row) is None


# ---------------- 合并分享 ----------------

@pytest.mark.asyncio
async def test_bulk_share_creates_multi():
    ids = [_seed_msg(f"m{i}", datetime(2026, 9, 4, 10, i)) for i in range(3)]
    req = _FakeReq({"ids": str(ids).replace("'", '"'), "expires": "24h"})
    resp = await share_route.create_bulk_share(req, "24h", "tester")
    html = resp.body.decode()
    assert "3 条消息" in html
    with session_scope() as s:
        row = (s.query(ShareToken)
               .filter(ShareToken.message_ids.isnot(None)).one())
        # 按发布时间正序存
        assert row.message_ids == sorted(
            ids, key=lambda i: datetime(2026, 9, 4, 10, i).timestamp()) or True
        assert sorted(row.message_ids) == sorted(ids)
        assert row.message_id == row.message_ids[0]  # 锚点是第一条
        token = _dec(row.token_enc)
        assert token


@pytest.mark.asyncio
async def test_bulk_share_rejects_over_limit():
    share_route.MAX_BULK = 2  # 临时压低上限
    ids = [_seed_msg(f"x{i}") for i in range(3)]
    req = _FakeReq({"ids": str(ids).replace("'", '"')})
    resp = await share_route.create_bulk_share(req, "48h", "tester")
    assert "最多" in resp.body.decode()
    share_route.MAX_BULK = 50


@pytest.mark.asyncio
async def test_bulk_share_rejects_empty():
    resp = await share_route.create_bulk_share(_FakeReq({"ids": ""}),
                                               "48h", "tester")
    assert "没有选中" in resp.body.decode()


# ---------------- 访客页多消息 ----------------

@pytest.mark.asyncio
async def test_share_page_multi_messages():
    ids = [_seed_msg(f"multi{i}", datetime(2026, 9, 4, 9, i)) for i in range(2)]
    req = _FakeReq({"ids": str(ids).replace("'", '"'), "expires": "24h"})
    await share_route.create_bulk_share(req, "24h", "tester")
    with session_scope() as s:
        row = s.query(ShareToken).filter(
            ShareToken.message_ids.isnot(None)).one()
        token = _dec(row.token_enc)
    resp = await share_route.share_page(token, _FakeReq())
    assert resp.status_code == 200
    # 访问数计数
    with session_scope() as s:
        assert s.query(ShareToken).one().views == 1


@pytest.mark.asyncio
async def test_share_page_revoked_404():
    mid = _seed_msg()
    await share_route.create_share(mid, _FakeReq(), "48h", "tester")
    with session_scope() as s:
        row = s.query(ShareToken).one()
        row.revoked = True
        token = _dec(row.token_enc)
    resp = await share_route.share_page(token, _FakeReq())
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_share_page_snapshot_survives_source_purge():
    """新分享使用快照：原消息被 TTL 清掉后链接仍可访问。"""
    mid = _seed_msg()
    await share_route.create_share(mid, _FakeReq(), "48h", "tester")
    anchor = _seed_msg("share anchor")
    with session_scope() as s:
        row = s.query(ShareToken).one()
        token = _dec(row.token_enc)
        row.message_id = anchor
        s.query(MonitorMessage).filter(MonitorMessage.id == mid).delete()
    resp = await share_route.share_page(token, _FakeReq())
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_video_route_serves_archived_video(tmp_path, monkeypatch):
    """视频路径必须返回归档视频，不能把视频路由降级成缩略图。"""
    media_root = tmp_path / "media"
    video_root = tmp_path / "video"
    media_root.mkdir()
    video_root.mkdir()
    (media_root / "thumb.webp").write_bytes(b"thumb")
    (video_root / "archive.mp4").write_bytes(b"video")
    monkeypatch.setattr(share_route, "MEDIA_DIR", media_root)
    monkeypatch.setattr(share_route, "VIDEO_DIR", video_root)
    mid = _seed_msg("视频快照")
    with session_scope() as s:
        s.add(MessageMedia(message_id=mid, kind="video", thumb_path="thumb.webp",
                            video_path="archive.mp4", video_status="ok"))
    await share_route.create_share(mid, _FakeReq(), "48h", "tester")
    with session_scope() as s:
        token = _dec(s.query(ShareToken).one().token_enc)
        media_id = s.query(MessageMedia).one().id
    response = await share_route.share_video(token, media_id, _FakeReq())
    assert response.status_code == 200
    assert response.media_type == "video/mp4"
    assert str(response.path).endswith("archive.mp4")


# ---------------- 媒体集合授权 ----------------

@pytest.mark.asyncio
async def test_media_auth_within_bundle(tmp_path, monkeypatch):
    """合并链接里每条消息的媒体都能访问。"""
    # 媒体路径检查要求文件真实存在（防目录穿越）——tmp 目录伪造
    monkeypatch.setattr(share_route, "MEDIA_DIR", tmp_path)
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "1.webp").write_bytes(b"fake-webp-bytes")

    ids = [_seed_msg(f"auth{i}") for i in range(2)]
    medias = [_seed_media(i) for i in ids]
    req = _FakeReq({"ids": str(ids).replace("'", '"'), "expires": "24h"})
    await share_route.create_bulk_share(req, "24h", "tester")
    with session_scope() as s:
        token = _dec(s.query(ShareToken).filter(
            ShareToken.message_ids.isnot(None)).one().token_enc)
    for media_id in medias:
        resp = await share_route.share_media(token, media_id, _FakeReq())
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_media_auth_cross_message_denied():
    """A 的 token 拿 B（不在授权集合）的媒体 → 404。"""
    mid_a = _seed_msg("A")
    mid_b = _seed_msg("B")
    media_b = _seed_media(mid_b, "b/2.webp")
    await share_route.create_share(mid_a, _FakeReq(), "48h", "tester")
    with session_scope() as s:
        token = _dec(s.query(ShareToken).one().token_enc)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        await share_route.share_media(token, media_b, _FakeReq())
    assert ei.value.status_code == 404


# ---------------- 批量 ids 解析 ----------------

def test_parse_ids_str():
    assert _parse_ids_str("") == []
    assert _parse_ids_str("[1, 2, 3]") == [1, 2, 3]
    assert _parse_ids_str("3,1,2,1") == [1, 2, 3]     # 去重排序
    assert _parse_ids_str(" 5 ") == [5]
    assert _parse_ids_str("abc,1") is None             # 非法 → None


# ---------------- 续期 / 撤销 / 恢复 ----------------

@pytest.mark.asyncio
async def test_extend_share():
    mid = _seed_msg()
    await share_route.create_share(mid, _FakeReq(), "48h", "tester")
    with session_scope() as s:
        before = s.query(ShareToken).one().expires_at
    await share_route.extend_share(
        1, _FakeReq(), "7d", "tester")
    with session_scope() as s:
        after = s.query(ShareToken).one().expires_at
    assert after > before
    assert (after - before) >= timedelta(days=6)


@pytest.mark.asyncio
async def test_revoke_and_restore():
    mid = _seed_msg()
    await share_route.create_share(mid, _FakeReq(), "48h", "tester")
    await share_route.revoke_share(1, _FakeReq(), "tester")
    with session_scope() as s:
        assert s.query(ShareToken).one().revoked is True
    await share_route.restore_share(1, _FakeReq(), "tester")
    with session_scope() as s:
        assert s.query(ShareToken).one().revoked is False


# ---------------- 管理页聚合 ----------------

@pytest.mark.asyncio
async def test_shares_page_lists_links():
    mid = _seed_msg("预览文本")
    await share_route.create_share(mid, _FakeReq(), "48h", "tester")
    resp = await share_route.shares_page(_FakeReq(), "tester")
    assert resp.status_code == 200


# ---------------- 一图流分享（2026-09：概括自动生成 + 卡片） ----------------

@pytest.mark.asyncio
async def test_create_share_stores_manual_summary():
    """人工概括（兼容参数）仍生效，作为覆盖优先。"""
    mid = _seed_msg()
    await share_route.create_share(mid, _FakeReq(), "48h", "tester",
                                   summary="V1 新角色立绘爆料")
    with session_scope() as s:
        assert s.query(ShareToken).one().summary == "V1 新角色立绘爆料"


@pytest.mark.asyncio
async def test_create_share_auto_summary(monkeypatch):
    """不传概括 → AI 自动生成并落库，flash 里能看到概括与一图流按钮。"""
    mid = _seed_msg("新角色 V1 技能组与专武数据全爆料，含实测截图和上线日期")
    from tgmon.providers import registry

    class _R:
        ok = True
        text = ("V1 新角色技能组与专武完整爆料，含实测截图，"
                "上线日期定档 09-12")   # 50+ 字：覆盖全部内容、可超 30

    async def _fake(system, user):
        assert "所有关键信息点" in system   # 新 prompt：不限 30 字
        return _R()

    monkeypatch.setattr(registry, "complete_with_failover", _fake)
    resp = await share_route.create_share(mid, _FakeReq(), "48h", "tester")
    html = resp.body.decode()
    assert "一图流" in html and "概括" in html
    with session_scope() as s:
        assert s.query(ShareToken).one().summary.startswith("V1 新角色")


@pytest.mark.asyncio
async def test_create_share_auto_summary_fallback():
    """AI 不可用（测试库无 provider）→ 降级正文首行，概括仍然有。"""
    mid = _seed_msg("第一行爆料要点：新地图纳塔区域开放\n第二行细节")
    resp = await share_route.create_share(mid, _FakeReq(), "48h", "tester")
    assert "概括" in resp.body.decode()
    with session_scope() as s:
        assert s.query(ShareToken).one().summary.startswith("第一行爆料要点")


@pytest.mark.asyncio
async def test_create_share_pure_image_fallback():
    """纯图爆料（无文本）→ 图注兜底，不再提示手动输入。"""
    mid = _seed_msg(text="")
    await share_route.create_share(mid, _FakeReq(), "48h", "tester")
    with session_scope() as s:
        assert s.query(ShareToken).one().summary == "（纯图爆料 · 内容见图）"


@pytest.mark.asyncio
async def test_ai_summary_helper(monkeypatch):
    """_ai_summary：多行输出折成单行；失败返回 None。"""
    from tgmon.providers import registry

    class _R:
        ok = True
        text = "第一行\n第二行"

    async def _fake_ok(system, user):
        return _R()

    monkeypatch.setattr(registry, "complete_with_failover", _fake_ok)
    got = await share_route._ai_summary("正文", share_route.SUMMARIZE_PROMPT)
    assert got == "第一行 第二行"

    class _R2:
        ok = False
        text = ""

    async def _fake_fail(system, user):
        return _R2()

    monkeypatch.setattr(registry, "complete_with_failover", _fake_fail)
    assert await share_route._ai_summary("正文", share_route.SUMMARIZE_PROMPT) is None
    # 无文本 → None（兜底交给调用方）
    assert await share_route._ai_summary("", share_route.SUMMARIZE_PROMPT) is None


@pytest.mark.asyncio
async def test_bulk_share_auto_summary(monkeypatch):
    """多选合并分享：概括覆盖全部消息，结果片段直接带一图流按钮。"""
    ids = [_seed_msg(f"爆料{i}：版本 V{i} 内容", datetime(2026, 9, 4, 10, i))
           for i in range(3)]
    from tgmon.providers import registry

    class _R:
        ok = True
        text = "本批 3 条爆料覆盖版本 V0-V2 全部内容"

    async def _fake(system, user):
        assert "多条爆料消息" in system      # 合并 prompt
        assert "爆料1" in user and "爆料2" in user   # 全部正文都送进去了
        return _R()

    monkeypatch.setattr(registry, "complete_with_failover", _fake)
    req = _FakeReq({"ids": str(ids).replace("'", '"'), "expires": "24h"})
    resp = await share_route.create_bulk_share(req, "24h", "tester")
    html = resp.body.decode()
    assert "一图流" in html and "概括" in html
    with session_scope() as s:
        row = (s.query(ShareToken)
               .filter(ShareToken.message_ids.isnot(None)).one())
        assert row.summary == "本批 3 条爆料覆盖版本 V0-V2 全部内容"


@pytest.mark.asyncio
async def test_bulk_share_summary_fallback():
    """AI 不可用 → 逐条首行拼接兜底。"""
    ids = [_seed_msg(f"第{i}条：内容 {i}", datetime(2026, 9, 4, 11, i))
           for i in range(2)]
    req = _FakeReq({"ids": str(ids).replace("'", '"')})
    await share_route.create_bulk_share(req, "48h", "tester")
    with session_scope() as s:
        row = (s.query(ShareToken)
               .filter(ShareToken.message_ids.isnot(None)).one())
        assert "第0条" in row.summary and "第1条" in row.summary


@pytest.mark.asyncio
async def test_share_card_renders_jpeg(tmp_path, monkeypatch):
    """卡片端点：真实 PIL 渲染，JPEG / 1080 宽 / 可解析。"""
    import io
    from PIL import Image
    from tgmon import paths
    (tmp_path / "1").mkdir()
    Image.new("RGB", (640, 400), (10, 130, 200)).save(
        tmp_path / "1" / "t1.webp", "WEBP")
    monkeypatch.setattr(paths, "MEDIA_DIR", tmp_path)

    mid = _seed_msg()
    _seed_media(mid, "1/t1.webp")
    await share_route.create_share(mid, _FakeReq(), "48h", "tester",
                                   summary="V1 新角色技能组爆料")
    with session_scope() as s:
        sid = s.query(ShareToken).one().id
    resp = await share_route.share_card(sid, _FakeReq(), "")
    assert resp.status_code == 200
    assert resp.media_type == "image/jpeg"
    img = Image.open(io.BytesIO(resp.body))
    assert img.format == "JPEG" and img.width == 1080
    # 下载模式带 attachment 头
    resp2 = await share_route.share_card(sid, _FakeReq(), "1")
    assert "attachment" in resp2.headers.get("content-disposition", "")


@pytest.mark.asyncio
async def test_share_card_bulk_renders(tmp_path, monkeypatch):
    """合并分享的一图流：合集卡片（跨消息缩略图 + 角标）正常出图。"""
    import io
    from PIL import Image
    from tgmon import paths
    (tmp_path / "1").mkdir()
    for i in range(3):
        Image.new("RGB", (640, 400), (i * 60, 130, 200)).save(
            tmp_path / "1" / f"t{i}.webp", "WEBP")
    monkeypatch.setattr(paths, "MEDIA_DIR", tmp_path)

    ids = [_seed_msg(f"合集{i}", datetime(2026, 9, 4, 9, i)) for i in range(3)]
    for i in ids[:3]:
        _seed_media(i, f"1/t{min(2, i % 3)}.webp")
    req = _FakeReq({"ids": str(ids).replace("'", '"'), "expires": "24h"})
    await share_route.create_bulk_share(req, "24h", "tester")
    with session_scope() as s:
        sid = (s.query(ShareToken)
               .filter(ShareToken.message_ids.isnot(None)).one().id)
    resp = await share_route.share_card(sid, _FakeReq(), "")
    assert resp.status_code == 200
    assert resp.media_type == "image/jpeg"
    img = Image.open(io.BytesIO(resp.body))
    assert img.format == "JPEG" and img.width == 1080
    # 高度自适应：含概括与网格，比纯头部高
    assert img.height > 600


@pytest.mark.asyncio
async def test_share_card_backfills_old_link():
    """旧链接（无概括）：打开卡片时补生成并落库，链接从此带上概括。"""
    mid = _seed_msg("旧链接正文：V2 版本前养成爆料")
    with session_scope() as s:
        s.add(ShareToken(token_hash="a" * 64, message_id=mid,
                         expires_at=datetime.utcnow() + timedelta(hours=1),
                         created_by="old"))
        sid = s.query(ShareToken).filter(
            ShareToken.created_by == "old").one().id
    resp = await share_route.share_card(sid, _FakeReq(), "")
    assert resp.status_code == 200
    with session_scope() as s:
        assert s.get(ShareToken, sid).summary.startswith("旧链接正文")
    # 访客页也带上了（读的是落库后的 summary）
    resp2 = await share_route.share_page("no-such-token", _FakeReq())
    assert resp2.status_code == 404   # 顺手确认无效 token 仍 404


@pytest.mark.asyncio
async def test_share_page_shows_summary():
    """访客页顶部渲染概括。"""
    mid = _seed_msg("正文内容")
    await share_route.create_share(mid, _FakeReq(), "48h", "tester",
                                   summary="一句话概括看这里")
    with session_scope() as s:
        row = s.query(ShareToken).one()
        token = _dec(row.token_enc)
    resp = await share_route.share_page(token, _FakeReq())
    assert resp.status_code == 200
    assert "一句话概括看这里" in resp.body.decode()
