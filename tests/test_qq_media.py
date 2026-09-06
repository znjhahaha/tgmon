"""QQ 图片 GitHub 中转链路测试。

背景（2026-09 实测）：平台富媒体上传是腾讯机房来拉 URL，境外小站
（本站香港 IP）拉不动报 850027 超时，GitHub raw 实测可达 —— 图片
改为经 GitHub 公开仓库中转。这里测路由决策与降级语义。
"""
from __future__ import annotations

import io
import base64
import json

import httpx
import pytest

from tgmon import settings
from tgmon.qqbot import media as qq_media
from tgmon.qqbot.client import QqApiError
from tgmon.qqbot import client as qq_client


@pytest.fixture(autouse=True)
def db():
    """建表 + 每个用例从干净配置开始；teardown 清掉测试写的配置。

    settings.set_many 写 app_setting 表，所以建表必须先于任何 set_many。
    合并在同一个 fixture 里，顺序不会被 pytest 打乱。
    """
    from tgmon.db import engine, session_scope
    from tgmon.models import Base, AppSetting
    Base.metadata.create_all(engine)
    settings.set_many({
        "GITHUB_RELAY_REPO": "",
        "GITHUB_RELAY_TOKEN": "",
        "BASE_URL": "https://example.com",
    })
    yield
    with session_scope() as s:
        for r in s.query(AppSetting).filter(
                AppSetting.key.in_(["GITHUB_RELAY_REPO",
                                    "GITHUB_RELAY_TOKEN",
                                    "BASE_URL"])).all():
            s.delete(r)
    settings.invalidate()


# ---------------- 配置判定 ----------------

def test_relay_disabled_without_config():
    assert qq_media.relay_enabled() is False


def test_relay_enabled_with_config():
    settings.set_many({"GITHUB_RELAY_REPO": "me/relay",
                       "GITHUB_RELAY_TOKEN": "ghp_x"})
    assert qq_media.relay_enabled() is True


def test_relay_config_rejects_bad_repo():
    """没有 owner/name 斜杠格式的仓库不启用（防手滑填了用户名）。"""
    settings.set_many({"GITHUB_RELAY_REPO": "just-a-name",
                       "GITHUB_RELAY_TOKEN": "ghp_x"})
    assert qq_media.relay_enabled() is False


@pytest.mark.asyncio
async def test_local_image_uploads_data_without_public_relay(tmp_path, monkeypatch):
    from PIL import Image
    from tgmon import paths
    monkeypatch.setattr(paths, "MEDIA_DIR", tmp_path)
    Image.new("RGB", (80, 60), "blue").save(tmp_path / "photo.png")
    received = []

    async def upload(group, kind, data):
        received.append((group, kind, data))
        return "direct-file-info"

    async def relay(*args, **kwargs):
        pytest.fail("Successful direct upload must not publish to GitHub")

    monkeypatch.setattr(qq_client, "upload_group_file_data", upload)
    monkeypatch.setattr(qq_media, "github_relay_url", relay)
    settings.set_many({"GITHUB_RELAY_REPO": "owner/relay", "GITHUB_RELAY_TOKEN": "test"})
    assert await qq_media.upload_image("group", "photo.png") == "direct-file-info"
    assert received[0][0:2] == ("group", 1)
    with Image.open(io.BytesIO(received[0][2])) as image:
        assert image.format == "JPEG" and image.size == (80, 60)


@pytest.mark.asyncio
async def test_file_data_request_uploads_without_sending_a_message(monkeypatch):
    requests = []
    real_client = httpx.AsyncClient

    def reply(request):
        requests.append(request)
        return httpx.Response(200, json={"file_info": "uploaded", "ttl": 300})

    async def token():
        return "test-token"

    monkeypatch.setattr(qq_client, "_get_token", token)
    monkeypatch.setattr(qq_client.httpx, "AsyncClient",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(reply)))
    assert await qq_client.upload_group_file_data("group", 1, b"jpeg-data") == "uploaded"
    assert requests[0].url.path == "/v2/groups/group/files"
    body = json.loads(requests[0].content)
    assert body == {"file_type": 1, "file_data": base64.b64encode(b"jpeg-data").decode(),
                    "srv_send_msg": False}


@pytest.mark.asyncio
async def test_direct_upload_failure_uses_configured_url_fallback(monkeypatch):
    settings.set_many({"GITHUB_RELAY_REPO": "owner/relay", "GITHUB_RELAY_TOKEN": "test"})
    monkeypatch.setattr(qq_media, "_jpeg_bytes", lambda path: b"jpeg-data")
    used = []

    async def direct(*args):
        raise QqApiError(400, "Direct upload unavailable")

    async def relay(path):
        return "https://raw.githubusercontent.com/owner/relay/main/image.jpg"

    async def upload(group, kind, url):
        used.append(url)
        return "fallback-file-info"

    monkeypatch.setattr(qq_client, "upload_group_file_data", direct)
    monkeypatch.setattr(qq_media, "github_relay_url", relay)
    monkeypatch.setattr(qq_client, "upload_group_file", upload)
    assert await qq_media.upload_image("group", "image.webp") == "fallback-file-info"
    assert len(used) == 1 and used[0].startswith("https://raw.githubusercontent.com/")


@pytest.mark.asyncio
async def test_direct_upload_not_in_group_preserves_fatal_delivery_state(monkeypatch):
    monkeypatch.setattr(qq_media, "_jpeg_bytes", lambda path: b"jpeg-data")

    async def direct(*args):
        raise QqApiError(40034101, "Not in group")

    async def relay(*args):
        pytest.fail("Not-in-group failures must not retry public hosting")

    monkeypatch.setattr(qq_client, "upload_group_file_data", direct)
    monkeypatch.setattr(qq_media, "github_relay_url", relay)
    with pytest.raises(QqApiError) as error:
        await qq_media.upload_image("group", "image.webp", raise_fatal=True)
    assert error.value.code == 40034101


# ---------------- upload_image 路由决策 ----------------

@pytest.mark.asyncio
async def test_upload_image_prefers_relay(monkeypatch):
    """配了中转 → 走 GitHub URL，不再用本站直链。"""
    settings.set_many({"GITHUB_RELAY_REPO": "me/relay",
                       "GITHUB_RELAY_TOKEN": "ghp_x"})
    calls = {}

    async def _fake_relay(thumb):
        calls["relay"] = thumb
        return "https://raw.githubusercontent.com/me/relay/main/x.jpg"

    async def _fake_upload(openid, ftype, url):
        calls["upload"] = (openid, ftype, url)
        return "FILEINFO123"

    monkeypatch.setattr(qq_media, "github_relay_url", _fake_relay)
    monkeypatch.setattr(qq_media.client, "upload_group_file", _fake_upload)
    out = await qq_media.upload_image("G1", "a/b.webp")
    assert out == "FILEINFO123"
    assert calls["relay"] == "a/b.webp"
    # 传给平台的是 GitHub raw URL
    assert calls["upload"][2].startswith(
        "https://raw.githubusercontent.com/me/relay/")


@pytest.mark.asyncio
async def test_upload_image_relay_upload_fail_no_fallback(monkeypatch):
    """中转上传 GitHub 失败 → 直接降级为 None（不再试本站直链 ——
    境外部署下直链必超时，白等 10 秒+）。"""
    settings.set_many({"GITHUB_RELAY_REPO": "me/relay",
                       "GITHUB_RELAY_TOKEN": "ghp_x"})

    async def _fake_relay(thumb):
        return None  # GitHub 上传失败

    from unittest.mock import AsyncMock
    upload_mock = AsyncMock()
    monkeypatch.setattr(qq_media, "github_relay_url", _fake_relay)
    monkeypatch.setattr(qq_media.client, "upload_group_file", upload_mock)
    out = await qq_media.upload_image("G1", "a/b.webp")
    assert out is None
    upload_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_image_retries_once_on_platform_error(monkeypatch):
    """平台拉取失败（850027 超时）→ 整链重试一次：第二次成功。"""
    settings.set_many({"GITHUB_RELAY_REPO": "me/relay",
                       "GITHUB_RELAY_TOKEN": "ghp_x"})
    calls = {"relay": 0, "upload": 0}

    async def _fake_relay(thumb):
        calls["relay"] += 1
        return f"https://raw.githubusercontent.com/me/relay/main/x{calls['relay']}.jpg"

    async def _fake_upload(openid, ftype, url):
        calls["upload"] += 1
        if calls["upload"] == 1:
            raise QqApiError(850027, "上传失败: timeout")
        return "FILEINFO_RETRY_OK"

    monkeypatch.setattr(qq_media, "github_relay_url", _fake_relay)
    monkeypatch.setattr(qq_media.client, "upload_group_file", _fake_upload)
    out = await qq_media.upload_image("G1", "a/b.webp")
    assert out == "FILEINFO_RETRY_OK"
    assert calls["relay"] == 2   # 重试时重新上传了 GitHub（新文件）
    assert calls["upload"] == 2  # 第二次平台拉取成功


@pytest.mark.asyncio
async def test_upload_image_retry_exhausted(monkeypatch):
    """两次都失败 → 降级 None 不抛异常。"""
    settings.set_many({"GITHUB_RELAY_REPO": "me/relay",
                       "GITHUB_RELAY_TOKEN": "ghp_x"})
    calls = {"upload": 0}

    async def _fake_relay(thumb):
        return "https://raw.githubusercontent.com/me/relay/main/x.jpg"

    async def _fake_upload(openid, ftype, url):
        calls["upload"] += 1
        raise QqApiError(850027, "上传失败: timeout")

    monkeypatch.setattr(qq_media, "github_relay_url", _fake_relay)
    monkeypatch.setattr(qq_media.client, "upload_group_file", _fake_upload)
    out = await qq_media.upload_image("G1", "a/b.webp")
    assert out is None
    assert calls["upload"] == 2  # 总共尝试了两次，不再无限重试


@pytest.mark.asyncio
async def test_upload_image_without_relay_uses_direct(monkeypatch):
    """未配置中转 → 保持原行为：本站直链。"""
    calls = {}

    def _fake_public(thumb):
        calls["public"] = thumb
        return "https://example.com/media/a/b.webp"

    async def _fake_upload(openid, ftype, url):
        calls["upload"] = url
        return "FILEINFO456"

    monkeypatch.setattr(qq_media, "image_public_url", _fake_public)
    monkeypatch.setattr(qq_media.client, "upload_group_file", _fake_upload)
    out = await qq_media.upload_image("G1", "a/b.webp")
    assert out == "FILEINFO456"
    assert calls["upload"] == "https://example.com/media/a/b.webp"


# ---------------- webp → jpeg 转换 ----------------

def test_jpeg_bytes_converts_webp(tmp_path, monkeypatch):
    """磁盘上的 webp 能转出 JPEG 字节（平台只认 png/jpg）。"""
    from PIL import Image
    from tgmon import paths
    monkeypatch.setattr(paths, "MEDIA_DIR", tmp_path)
    (tmp_path / "11").mkdir()
    img = Image.new("RGB", (64, 48), (200, 30, 30))
    img.save(tmp_path / "11" / "x.webp", "WEBP")

    out = qq_media._jpeg_bytes("11/x.webp")
    assert out is not None and out[:3] == b"\xff\xd8\xff"  # JPEG magic


def test_jpeg_bytes_missing_file(tmp_path, monkeypatch):
    from tgmon import paths
    monkeypatch.setattr(paths, "MEDIA_DIR", tmp_path)
    assert qq_media._jpeg_bytes("11/nope.webp") is None


# ---------------- github_put 请求构造 ----------------

@pytest.mark.asyncio
async def test_github_put_builds_correct_request(monkeypatch):
    """contents API PUT：路径、鉴权头、base64 body，成功即返回 raw URL。"""
    settings.set_many({"GITHUB_RELAY_REPO": "me/relay",
                       "GITHUB_RELAY_TOKEN": "ghp_tok"})
    captured = {}

    class _Resp:
        status_code = 201

        def json(self):
            return {}

    class _FakeClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def put(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _Resp()

    monkeypatch.setattr(qq_media.httpx, "AsyncClient", _FakeClient)
    out = await qq_media.github_put(b"\xff\xd8\xffabc", "qq-relay/d/f.jpg")
    assert out == ("https://raw.githubusercontent.com/me/relay/main/"
                   "qq-relay/d/f.jpg")
    assert captured["url"] == ("https://api.github.com/repos/me/relay/"
                               "contents/qq-relay/d/f.jpg")
    assert captured["headers"]["Authorization"] == "Bearer ghp_tok"
    import base64
    assert captured["json"]["content"] == base64.b64encode(
        b"\xff\xd8\xffabc").decode()


@pytest.mark.asyncio
async def test_github_put_non_2xx_returns_none(monkeypatch):
    settings.set_many({"GITHUB_RELAY_REPO": "me/relay",
                       "GITHUB_RELAY_TOKEN": "ghp_tok"})

    class _Resp:
        status_code = 401  # token 无效

        def json(self):
            return {}

        @property
        def text(self):
            return "Bad credentials"

    class _FakeClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def put(self, url, json=None, headers=None):
            return _Resp()

    monkeypatch.setattr(qq_media.httpx, "AsyncClient", _FakeClient)
    assert await qq_media.github_put(b"xx", "qq-relay/d/f.jpg") is None
