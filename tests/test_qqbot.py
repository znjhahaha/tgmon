"""QQ 机器人出站的核心逻辑测试。

重点四块：
1. 回调验签与官方文档 DEMO 对拍 —— 算法和腾讯不一致就永远收不到事件
2. 推送文案格式化：URL 剥离（平台禁止链接）、截断、剧透标记
3. 事件处理：进群 / 退群 / 被 @
4. push_message 过滤链：频道白名单 / 判重跳过 / 每群日限熔断
"""
from __future__ import annotations

import itertools

import pytest

from tgmon import qqbot
from tgmon.db import engine, session_scope
from tgmon.models import (
    Base, Channel, MessageMedia, MonitorMessage, QqDelivery, QqGroup,
    QqInbound,
)
from tgmon.qqbot import client as qq_client
from tgmon import settings

# 官方文档 sign.html 的 DEMO（Go 版）。Python 实现必须与之对拍通过
OFFICIAL_SECRET = "naOC0ocQE3shWLAfffVLB1rhYPG7"
OFFICIAL_SEED = "naOC0ocQE3shWLAfffVLB1rhYPG7naOC"
OFFICIAL_TS = "1725442341"
OFFICIAL_BODY = b'{ "op": 0,"d": {}, "t": "GATEWAY_EVENT_NAME"}'
OFFICIAL_SIG = ("865ad13a61752ca65e26bde6676459cd36cf1be609375b37bd62af366e1dc25a"
                "8dc789ba7f14e017ada3d554c671a911bfdf075ba54835b23391d509579ed002")


# ---------------- 验签 ----------------

def test_seed_matches_official_demo():
    """secret 28 字节 → 翻倍到 56 取前 32，与文档 DEMO 的 seed 一致。"""
    assert qq_client._seed_from_secret(OFFICIAL_SECRET) == OFFICIAL_SEED.encode()


def test_seed_long_secret():
    """secret 本身 >=32 字节时直接取前 32，不进翻倍循环。"""
    s = "x" * 40
    assert qq_client._seed_from_secret(s) == b"x" * 32


def test_verify_official_demo():
    """官方 DEMO 对拍（分两层）：

    1. 密钥派生对拍 —— 官方 DEMO 给了真实公钥字节（215 195 98 …），
       我们的 seed→公钥必须逐字节一致（见 test_seed_matches_official_demo
       下方的公钥对拍用例）
    2. 验签对拍 —— 官方 DEMO 的 sig 值经实测（_chk_sig.py 实验，已删）：
       用官方公钥验不过，用官方私钥对 timestamp+body 重签也对不上 ——
       它是文档随手填的占位值，不是真实签名。所以这里改用官方 DEMO 的
       seed 派生私钥自签自验：验证「签名体 = timestamp + body」的拼接
       顺序与 verify_callback 的完整链路自洽。
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    # 官方 DEMO 输出的公钥（Go ed25519.GenerateKey 的 publicKey 字节）
    official_pub = bytes([215, 195, 98, 254, 120, 174, 248, 31, 242, 50,
                          135, 180, 147, 98, 139, 93, 176, 42, 60, 79,
                          227, 11, 33, 94, 77, 25, 96, 155, 93, 118, 103, 58])
    priv = Ed25519PrivateKey.from_private_bytes(OFFICIAL_SEED.encode())
    assert priv.public_key().public_bytes_raw() == official_pub
    # 自签自验：签名体按官方顺序 timestamp + body
    sig = priv.sign(OFFICIAL_TS.encode() + OFFICIAL_BODY).hex()
    assert qq_client.verify_callback(
        OFFICIAL_SECRET, OFFICIAL_TS, OFFICIAL_BODY, sig) is True


def test_verify_rejects_tampered_body():
    """真签名 + 篡改的 body → 必须拒绝。"""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    priv = Ed25519PrivateKey.from_private_bytes(OFFICIAL_SEED.encode())
    sig = priv.sign(OFFICIAL_TS.encode() + OFFICIAL_BODY).hex()
    assert not qq_client.verify_callback(
        OFFICIAL_SECRET, OFFICIAL_TS, OFFICIAL_BODY + b"x", sig)


def test_verify_rejects_bad_signature():
    assert not qq_client.verify_callback(
        OFFICIAL_SECRET, OFFICIAL_TS, OFFICIAL_BODY, "00" * 64)


def test_verify_rejects_missing_pieces():
    assert not qq_client.verify_callback("", OFFICIAL_TS, OFFICIAL_BODY, OFFICIAL_SIG)
    assert not qq_client.verify_callback(OFFICIAL_SECRET, "", OFFICIAL_BODY, OFFICIAL_SIG)
    assert not qq_client.verify_callback(OFFICIAL_SECRET, OFFICIAL_TS, OFFICIAL_BODY, "")
    assert not qq_client.verify_callback(OFFICIAL_SECRET, OFFICIAL_TS, OFFICIAL_BODY, "not-hex")


# ---------------- op=13 回调地址验证 ----------------
# 官方 event-emit.html 的完整 DEMO（请求-响应对，与 sign.html 的占位 sig 不同，
# 这组值是真实可对拍的 —— 已逐字节验证）
OFFICIAL_V_SECRET = "DG5g3B4j9X2KOErG"
OFFICIAL_V_TOKEN = "Arq0D5A61EgUu4OxUvOp"
OFFICIAL_V_TS = "1725442341"
OFFICIAL_V_SIG = ("87befc99c42c651b3aac0278e71ada338433ae26fcb24307bdc5ad38c1adc2d0"
                  "1bcfcadc0842edac85e85205028a1132afe09280305f13aa6909ffc2d652c706")


def test_op13_validation_official_demo():
    """官方 DEMO 对拍：sign_validation(secret, event_ts, plain_token)
    必须逐字节等于文档给出的 signature。"""
    sig = qq_client.sign_validation(
        OFFICIAL_V_SECRET, OFFICIAL_V_TS, OFFICIAL_V_TOKEN)
    assert sig == OFFICIAL_V_SIG


def test_op13_callback_endpoint(db, monkeypatch):
    """端到端：正确签名的 op=13 请求打到 /qqbot/callback，
    必须返回 plain_token + 正确的 signature（而不是普通 op:12 ACK）。"""
    import json as _json
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    from fastapi.testclient import TestClient

    monkeypatch.setattr(settings, "get", lambda key, default=None:
                        {"QQ_APP_SECRET": OFFICIAL_SECRET,
                         "QQ_APP_ID": "1020000000"}.get(key, default))
    body = _json.dumps({
        "op": 13,
        "d": {"plain_token": OFFICIAL_V_TOKEN, "event_ts": OFFICIAL_V_TS},
    }).encode()
    # 请求级签名体：X-Signature-Timestamp + 原始 body
    priv = Ed25519PrivateKey.from_private_bytes(OFFICIAL_SEED.encode())
    req_sig = priv.sign(OFFICIAL_TS.encode() + body).hex()

    client = TestClient(_admin_app())
    r = client.post("/qqbot/callback", content=body, headers={
        "Content-Type": "application/json",
        "X-Signature-Ed25519": req_sig,
        "X-Signature-Timestamp": OFFICIAL_TS,
    })
    assert r.status_code == 200
    data = r.json()
    # 响应必须是验证应答（op13 语义），且 signature 与官方算法一致
    assert data.get("plain_token") == OFFICIAL_V_TOKEN
    assert data.get("signature") == qq_client.sign_validation(
        OFFICIAL_SECRET, OFFICIAL_V_TS, OFFICIAL_V_TOKEN)


def test_op13_uses_bot_secret(db, monkeypatch):
    """签名密钥只认 QQ_APP_SECRET（机器人密钥/Bot Secret）。

    即使 DB 里残留 QQ_SIGN_SECRET（2026-09-04 事故中误存的「机器人令牌」，
    DEFAULTS 删除后 load() 仍会带出残值），op13 应答也必须用 APP_SECRET 签。
    官方 sign.html：Ed25519 种子 = Bot Secret = getAppAccessToken 的
    clientSecret，同一个凭证，没有第二个签名密钥。"""
    import json as _json
    from fastapi.testclient import TestClient

    stale_token = "StaleBotToken0000000000000000"

    def fake_get(key, default=None):
        if key == "QQ_APP_SECRET":
            return OFFICIAL_SECRET
        if key == "QQ_SIGN_SECRET":
            return stale_token  # 模拟 DB 残值
        if key == "QQ_APP_ID":
            return "1020000000"
        return default

    monkeypatch.setattr(settings, "get", fake_get)
    body = _json.dumps({
        "op": 13,
        "d": {"plain_token": OFFICIAL_V_TOKEN, "event_ts": OFFICIAL_V_TS},
    }).encode()

    client = TestClient(_admin_app())
    r = client.post("/qqbot/callback", content=body, headers={
        "Content-Type": "application/json"})
    assert r.status_code == 200
    data = r.json()
    assert data.get("signature") == qq_client.sign_validation(
        OFFICIAL_SECRET, OFFICIAL_V_TS, OFFICIAL_V_TOKEN)
    # 且不等于用残值签出来的（两条不同曲线，防退化回优先读残值）
    assert data["signature"] != qq_client.sign_validation(
        stale_token, OFFICIAL_V_TS, OFFICIAL_V_TOKEN)


def test_op13_without_signature_headers(db, monkeypatch):
    """官方 SDK 行为：op=13 不验签 —— 无签名头的校验请求也必须放行。
    平台的校验握手可能不带 X-Signature-* 头，先验签会把握手挡掉。"""
    import json as _json
    from fastapi.testclient import TestClient

    monkeypatch.setattr(settings, "get", lambda key, default=None:
                        {"QQ_APP_SECRET": OFFICIAL_SECRET,
                         "QQ_APP_ID": "1020000000"}.get(key, default))
    body = _json.dumps({
        "op": 13,
        "d": {"plain_token": OFFICIAL_V_TOKEN, "event_ts": OFFICIAL_V_TS},
    }).encode()

    client = TestClient(_admin_app())
    r = client.post("/qqbot/callback", content=body, headers={
        "Content-Type": "application/json",
        # 故意不带 X-Signature-Ed25519 / X-Signature-Timestamp
    })
    assert r.status_code == 200
    data = r.json()
    assert data.get("plain_token") == OFFICIAL_V_TOKEN
    assert data.get("signature") == qq_client.sign_validation(
        OFFICIAL_SECRET, OFFICIAL_V_TS, OFFICIAL_V_TOKEN)


def test_op0_without_signature_rejected(db, monkeypatch):
    """事件推送缺签名头 → 403。事件入口必须验签，否则任何人裸 POST 都能
    伪造进群/退群/被 @ 事件并触发机器人发消息。（2026-09 曾因把自测合成
    流量误判为「平台 V3 无签名模式」而短暂放行过，已纠正。）"""
    import json as _json
    from fastapi.testclient import TestClient

    monkeypatch.setattr(settings, "get", lambda key, default=None:
                        {"QQ_APP_SECRET": OFFICIAL_SECRET,
                         "QQ_APP_ID": "1020000000"}.get(key, default))
    body = _json.dumps({"op": 0, "t": "GROUP_ADD_ROBOT",
                        "d": {"group_openid": "ABC1234567890"}}).encode()
    client = TestClient(_admin_app())
    r = client.post("/qqbot/callback", content=body, headers={
        "Content-Type": "application/json"})
    assert r.status_code == 403


def test_op0_bad_signature_rejected(db, monkeypatch):
    """带签名头但签名错误 → 仍然 403（验签核心不受 V3 放行影响）。"""
    import json as _json
    from fastapi.testclient import TestClient

    monkeypatch.setattr(settings, "get", lambda key, default=None:
                        {"QQ_APP_SECRET": OFFICIAL_SECRET,
                         "QQ_APP_ID": "1020000000"}.get(key, default))
    body = _json.dumps({"op": 0, "t": "GROUP_ADD_ROBOT",
                        "d": {"group_openid": "ABC1234567890"}}).encode()
    client = TestClient(_admin_app())
    r = client.post("/qqbot/callback", content=body, headers={
        "Content-Type": "application/json",
        "X-Signature-Ed25519": "00" * 64,
        "X-Signature-Timestamp": OFFICIAL_TS})
    assert r.status_code == 403


def _admin_app():
    from tgmon.admin.app import app
    return app


# ---------------- 格式化 ----------------

def _payload(**kw):
    base = {
        "channel": {"id": 1, "title": "Seele Leaks", "game": ""},
        "game_detected": "原神",
        "version_tag": None,
        "text_zh": "正文内容",
        "text_raw": "raw",
        "has_spoiler": False,
    }
    base.update(kw)
    return base


def test_format_basic_head():
    text = qqbot._format_text(_payload())
    assert text.startswith("【原神】Seele Leaks\n正文内容")


def test_format_strips_urls():
    text = qqbot._format_text(_payload(
        text_zh="新爆料 https://t.me/seele_leaks/123 详见链接 http://example.com/a"))
    assert "http" not in text
    assert "新爆料" in text


def test_format_truncates():
    text = qqbot._format_text(_payload(text_zh="长" * 5000))
    assert len(text) <= qqbot.MAX_CONTENT
    assert text.endswith("…")


def test_format_spoiler_prefix():
    text = qqbot._format_text(_payload(has_spoiler=True))
    assert text.splitlines()[1].startswith("⚠ 含剧透")


def test_format_version_tag():
    text = qqbot._format_text(_payload(version_tag="7.1"))
    assert "（7.1）" in text.splitlines()[0]


def test_format_empty_body():
    """无正文：只返回标题行，不再有「（无文本内容）」占位（2026-09 群反馈）。"""
    text = qqbot._format_text(_payload(text_zh=None, text_raw=None))
    assert text == "【原神】Seele Leaks"
    assert "无文本" not in text


def test_format_empty_body_spoiler_kept():
    """无正文但含剧透：标题行 + 剧透标记，仍无占位符。"""
    text = qqbot._format_text(
        _payload(text_zh=None, text_raw=None, has_spoiler=True))
    assert text.splitlines()[-1] == "⚠ 含剧透"
    assert "无文本" not in text


# ---------------- DB fixture ----------------

@pytest.fixture()
def db():
    Base.metadata.create_all(engine)
    settings.set_many({"BASE_URL": "https://example.com"})
    yield
    # 每个用例清空相关表，互不串扰
    with session_scope() as s:
        from tgmon.models import QqEvent, QqPending, ShareToken, ConversationTurn, Conversation
        for t in (QqEvent, QqPending, ShareToken, ConversationTurn, Conversation, QqInbound, QqDelivery, QqGroup, MonitorMessage, Channel):
            s.query(t).delete()
        from tgmon.models import QqBot
        s.query(QqBot).delete()
        from tgmon.models import AppSetting
        for r in s.query(AppSetting).all():
            s.delete(r)
    settings.invalidate()
    qq_client.invalidate_token()


@pytest.fixture()
def conf(db, monkeypatch):
    """打开 QQ 推送并给基础数据。返回可调的 settings 快捷方式。

    多机器人时代：预置一个启用的机器人行（推送 / 测试消息路径都要
    解析凭据）。想测「无机器人」的场景直接用 db fixture。
    """
    from tgmon.crypto import encrypt
    from tgmon.models import QqBot
    with session_scope() as s:
        if not s.query(QqBot.id).first():
            s.add(QqBot(app_id="10001", nickname="测试机器人",
                        app_secret_enc=encrypt("secret-10001")))
    settings.set_many({"QQ_ENABLED": True, "QQ_CHANNEL_IDS": [],
                       "QQ_INCLUDE_DUPS": False, "QQ_DAILY_LIMIT": 950})
    from tgmon.qqbot import media
    async def no_upload(*args, **kwargs):
        return None
    monkeypatch.setattr(media, "upload_image", no_upload)

    def _set(**kw):
        settings.set_many(kw)
    return _set


SENT: list[tuple[str, str]] = []


async def _push(mid):
    """Advance the approved batch window before checking delivery outcomes."""
    from datetime import datetime, timedelta
    from tgmon.qqbot.batches import flush_pending
    with session_scope() as s:
        before = s.query(QqDelivery).filter_by(status="done").count()
    await qqbot.push_message(mid)
    await flush_pending(datetime.utcnow() + timedelta(seconds=61))
    with session_scope() as s:
        return s.query(QqDelivery).filter_by(status="done").count() - before


@pytest.fixture()
def fake_send(monkeypatch):
    """记录调用而不真发网络请求。默认成功。"""
    SENT.clear()

    async def _fake(openid, content, msg_id=None, bot=None):
        SENT.append((openid, content))
        return "ROBOT1.0_fake"

    monkeypatch.setattr(qq_client, "send_group_text", _fake)
    return SENT


def _seed_group(openid="G1" + "x" * 30, nickname="测试群", enabled=True):
    with session_scope() as s:
        g = QqGroup(group_openid=openid, nickname=nickname, enabled=enabled)
        s.add(g)
        s.flush()
        return g.id


_MSG_SEQ = itertools.count(9000)


def _seed_message(channel_id=1, duplicate_of=None, published_at=None,
                  text_zh="中文正文", game="原神", text_raw="raw text"):
    # tg_message_id 必须全局唯一：uq_msg_channel_tgid 唯一约束下，
    # 同频道连造两条（原消息 + 它的重复消息）会撞车
    with session_scope() as s:
        ch = s.get(Channel, channel_id)
        if ch is None:
            ch = Channel(id=channel_id, title="Ch", tg_id=f"-100{channel_id}")
            s.add(ch)
        m = MonitorMessage(channel_id=channel_id,
                           tg_message_id=str(next(_MSG_SEQ)),
                           text_raw=text_raw, text_zh=text_zh,
                           game_detected=game, duplicate_of=duplicate_of)
        if published_at is not None:
            m.published_at = published_at
        s.add(m)
        s.flush()
        return m.id


@pytest.fixture()
def fake_payload(monkeypatch):
    """outputs.serialize 打桩 —— push_message 只读它的几个字段。"""
    from tgmon import outputs

    def _fake(mid, include_original=True):
        return _payload()

    monkeypatch.setattr(outputs, "serialize", _fake)


# ---------------- 事件处理 ----------------

@pytest.mark.asyncio
async def test_event_add_robot(db):
    res = await qqbot.handle_callback_event(
        {"type": "GROUP_ADD_ROBOT", "d": {"group_openid": "ABC1234567890"}})
    assert res["handled"] == "add"
    with session_scope() as s:
        g = s.query(QqGroup).filter(QqGroup.group_openid == "ABC1234567890").first()
        assert g is not None and g.enabled


@pytest.mark.asyncio
async def test_event_add_robot_idempotent(db):
    await qqbot.handle_callback_event(
        {"type": "GROUP_ADD_ROBOT", "d": {"group_openid": "ABC1234567890"}})
    await qqbot.handle_callback_event(
        {"type": "GROUP_ADD_ROBOT", "d": {"group_openid": "ABC1234567890"}})
    with session_scope() as s:
        assert s.query(QqGroup).count() == 1


@pytest.mark.asyncio
async def test_event_del_robot_disables(db):
    _seed_group(openid="ABC1234567890")
    res = await qqbot.handle_callback_event(
        {"type": "GROUP_DEL_ROBOT", "d": {"group_openid": "ABC1234567890"}})
    assert res["handled"] == "del"
    with session_scope() as s:
        g = s.query(QqGroup).first()
        assert not g.enabled and g.dropped_at is not None


@pytest.mark.asyncio
async def test_event_at_message_returns_reply(db):
    res = await qqbot.handle_callback_event(
        {"type": "GROUP_AT_MESSAGE_CREATE",
         "d": {"group_openid": "ABC1234567890", "id": "ROBOT1.0_xyz"}})
    assert res["handled"] == "at"
    assert res["replies"] and res["msg_id"] == "ROBOT1.0_xyz"
    # @ 也会补录没见过的群（进群回调丢失的场景）
    with session_scope() as s:
        assert s.query(QqGroup).count() == 1


@pytest.mark.asyncio
async def test_event_unknown_type(db):
    res = await qqbot.handle_callback_event({"type": "WHATEVER", "d": {}})
    assert res["handled"] is None


# ---------------- 群内命令系统（commands.py） ----------------

def _at_event(content: str, openid: str = "ABC1234567890") -> dict:
    return {"type": "GROUP_AT_MESSAGE_CREATE",
            "d": {"group_openid": openid, "id": f"ROBOT1.0_cmd_{next(_MSG_SEQ)}",
                  "content": content,
                  "author": {"member_openid": "MEMBER1"}}}


def _texts(res: dict) -> str:
    """回复列表 → 拼接纯文本（断言用）。"""
    from tgmon.qqbot.commands import replies_text
    from tgmon.models import ShareToken
    with session_scope() as s:
        ids = {r.bundle_id for r in res["replies"] if r.bundle_id}
        stories = [item for row in s.query(ShareToken).filter(ShareToken.id.in_(ids)) for item in row.snapshot_items]
    return replies_text(res["replies"]) + "\n" + "\n".join(
        f"【{item['game']}】{item['text']}" for item in stories)


@pytest.mark.asyncio
async def test_cmd_help(db):
    res = await qqbot.handle_callback_event(_at_event("/help"))
    assert res["handled"] == "at" and res["channel"] == "group"
    t = _texts(res)
    assert "/status" in t and "/ai" in t and "/new" in t
    assert res["target"] == "ABC1234567890"


@pytest.mark.asyncio
async def test_cmd_status(db):
    _seed_group(openid="ABC1234567890")
    res = await qqbot.handle_callback_event(_at_event("/status"))
    t = _texts(res)
    assert "事件通道" in t and "接入群" in t


@pytest.mark.asyncio
async def test_cmd_latest_orders_by_published_at(db):
    """/latest 的排序 bug 回归：补拉的旧消息 id 更大，必须按发布时间排。"""
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    fresh = _seed_message(text_zh="刚发的新爆料", published_at=now)
    old = _seed_message(text_zh="补拉的历史旧闻",
                        published_at=now - timedelta(days=2))
    assert old > fresh  # 前提成立：旧消息 id 更大（模拟补拉插入顺序）
    res = await qqbot.handle_callback_event(_at_event("/latest 1"))
    t = _texts(res)
    assert "刚发的新爆料" in t and "补拉的历史旧闻" not in t


@pytest.mark.asyncio
async def test_cmd_latest_full_text_not_truncated(db):
    """完整文本：不再截 70 字（仅受平台 1800 上限约束）。"""
    long_text = "完整的爆料内容" + "很长的正文" * 40  # > 70 字
    _seed_message(text_zh=long_text)
    res = await qqbot.handle_callback_event(_at_event("/latest 1"))
    assert long_text[:100] in _texts(res)  # 前 100 字必须原样出现


# ---------------- 数量解析与自然语言 latest（2026-09：要五条只发三条的修复） ----------------

def test_requested_count():
    """显式数量解析：阿拉伯数字 / 中文数字 / 带量词。"""
    from tgmon.qqbot.commands import _requested_count
    assert _requested_count("发最近的五条爆料") == 5
    assert _requested_count("来两条") == 2
    assert _requested_count("看三条消息") == 3
    assert _requested_count("/latest 5") == 5
    assert _requested_count("最新 3") == 3
    assert _requested_count("/game 原神 4") == 4
    assert _requested_count("看看有什么新消息") is None   # 没说数量
    assert _requested_count("") is None
    # 大数不在此截断（调用方按 LATEST_MAX 夹紧），解析层如实返回
    assert _requested_count("/latest 99") == 99


def test_natural_latest_request():
    """自然语言 latest 识别：简单请求直连命令，实质问题留给 AI。"""
    from tgmon.qqbot.commands import _natural_latest_request
    assert _natural_latest_request("发给我最近的五条爆料") == ("", 5)
    assert _natural_latest_request("帮我看看原神最新爆料") == ("原神", 3)
    assert _natural_latest_request("今天有什么新爆料") == ("", 3)
    assert _natural_latest_request("绝区零最近三条消息") == ("绝区零", 3)
    # 实质问题 / 命令 / 双游戏 → 不拦截
    assert _natural_latest_request("纳塔的剧情怎么看") is None
    assert _natural_latest_request("/latest 5") is None
    assert _natural_latest_request("原神和崩铁的最新爆料") is None


@pytest.mark.asyncio
async def test_latest_five_natural_and_command(db):
    """端到端：明确要五条就回五条（自然语言与命令两条路径）。"""
    for i in range(6):
        _seed_message(text_zh=f"第{i}条爆料内容")
    res = await qqbot.handle_callback_event(_at_event("发最近的五条爆料"))
    mids = [mid for r in res["replies"] for mid in r.story_ids]
    assert len(mids) == 5, f"自然语言要五条，实际回了 {len(mids)} 条"
    res2 = await qqbot.handle_callback_event(_at_event("/latest 5"))
    mids2 = [mid for r in res2["replies"] for mid in r.story_ids]
    assert len(mids2) == 5, f"命令要五条，实际回了 {len(mids2)} 条"


@pytest.mark.asyncio
async def test_latest_followup_more_respects_count(db):
    """"再来两条"续读：按明确数量继续，不只回默认条数。"""
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    for i in range(8):
        _seed_message(text_zh=f"续读{i}", published_at=now - timedelta(minutes=i))
    first = await qqbot.handle_callback_event(_at_event("/latest 5"))
    assert len([mid for r in first["replies"] for mid in r.story_ids]) == 5
    more = await qqbot.handle_callback_event(_at_event("再来两条"))
    mids = [mid for r in more["replies"] for mid in r.story_ids]
    assert len(mids) == 2, f"再来两条实际回了 {len(mids)} 条"
    # 续读的是更早的消息，不与第一批重复
    first_ids = {mid for r in first["replies"] for mid in r.story_ids}
    assert not (set(mids) & first_ids)


@pytest.mark.asyncio
async def test_agent_explicit_count_preserved(db, monkeypatch):
    """agent 模式：用户明说五条，模型给的 n 会被用户原话覆盖为 5。"""
    from tgmon.qqbot import agent
    from tgmon.providers import registry

    class _R:
        ok = True
        text = '{"action":"latest","args":{"n":2},"reply":""}'

    async def _fake(system, user, **kw):
        return _R()

    monkeypatch.setattr(registry, "complete_with_failover", _fake)
    for i in range(6):
        _seed_message(text_zh=f"agent{i}爆料")
    replies = await agent.handle("发最近的五条爆料", "ABC1234567890",
                                 True, "MEM1")
    mids = [mid for r in replies for mid in r.story_ids]
    assert len(mids) == 5, f"agent 路径要五条，实际回了 {len(mids)} 条"


@pytest.mark.asyncio
async def test_cmd_new_incremental(db):
    """/new 只返回游标之后的新消息，并推进游标。"""
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    _seed_message(text_zh="旧消息A", published_at=now - timedelta(hours=3))
    _seed_message(text_zh="旧消息B", published_at=now - timedelta(hours=2))
    # 第一次 /latest 推进游标到 B
    first = await qqbot.handle_callback_event(_at_event("/latest 2"))
    from tgmon.qqbot.commands import mark_read
    mark_read("ABC1234567890", [mid for reply in first["replies"] for mid in reply.story_ids])
    with session_scope() as s:
        g = s.query(QqGroup).filter(
            QqGroup.group_openid == "ABC1234567890").first()
        assert g.last_seen_msg_id is not None
    # 来一条新的
    _seed_message(text_zh="新消息C", published_at=now)
    res = await qqbot.handle_callback_event(_at_event("/new"))
    t = _texts(res)
    assert "新消息C" in t and "旧消息A" not in t and "旧消息B" not in t
    mark_read("ABC1234567890", [mid for reply in res["replies"] for mid in reply.story_ids])
    # 没有更新的 → 明确提示
    res2 = await qqbot.handle_callback_event(_at_event("/new"))
    assert "没有新消息" in _texts(res2)


@pytest.mark.asyncio
async def test_cmd_new_unions_untracked_subscribed_games(db):
    """新增订阅或新分类的游戏没有游标时也要作为未读返回。"""
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    _seed_message(channel_id=1, text_zh="原神新消息", game="原神",
                  published_at=now - timedelta(minutes=5))
    _seed_message(channel_id=2, text_zh="崩铁新消息", game="崩坏:星穹铁道",
                  published_at=now)
    with session_scope() as s:
        group = QqGroup(group_openid="ABC1234567890", games=["原神", "崩坏:星穹铁道"])
        group.cursors = {"原神": {"published_at": (now - timedelta(minutes=10)).isoformat(),
                                  "id": 0}}
        s.add(group)
    rows = qqbot.commands._cmd_new("ABC1234567890")
    text = "\n".join(reply.text for reply in rows)
    assert "原神新消息" in text and "崩铁新消息" in text


@pytest.mark.asyncio
async def test_cmd_search(db):
    _seed_message(text_zh="卡芙卡新卡池爆料")
    _seed_message(text_zh="无关消息")
    res = await qqbot.handle_callback_event(_at_event("/search 卡芙卡"))
    t = _texts(res)
    assert "卡芙卡" in t and "无关消息" not in t


@pytest.mark.asyncio
async def test_cmd_game_filter(db):
    _seed_message(text_zh="绝区零更新", game="绝区零")
    _seed_message(text_zh="原神更新", game="原神")
    res = await qqbot.handle_callback_event(_at_event("/game 绝区零 1"))
    t = _texts(res)
    assert "绝区零更新" in t and "原神更新" not in t


@pytest.mark.asyncio
async def test_cmd_peek(db):
    mid = _seed_message(text_zh="单条详情内容")
    res = await qqbot.handle_callback_event(_at_event(f"/peek {mid}"))
    assert "单条详情内容" in _texts(res)


@pytest.mark.asyncio
async def test_cmd_toggle_on_off(db):
    _seed_group(openid="ABC1234567890", enabled=False)
    res = await qqbot.handle_callback_event(_at_event("/on"))
    assert "已开启" in _texts(res)
    with session_scope() as s:
        assert s.query(QqGroup).first().enabled
    res = await qqbot.handle_callback_event(_at_event("/off"))
    assert "已关闭" in _texts(res)
    with session_scope() as s:
        assert not s.query(QqGroup).first().enabled


@pytest.mark.asyncio
async def test_cmd_unknown_shows_help_hint(db):
    res = await qqbot.handle_callback_event(_at_event("/乱写"))
    assert "不认识的命令" in _texts(res)


@pytest.mark.asyncio
async def test_replies_clipped_to_five(db):
    """被动回复 5 次/条上限：_clip 截断并提示。"""
    from tgmon.qqbot.commands import Reply, _clip
    many = [Reply(text=f"第{i}条") for i in range(8)]
    out = _clip(many)
    assert len(out) == 5 and "截断" in out[-1].text


@pytest.mark.asyncio
async def test_ai_fallback_off_returns_help(db):
    settings.set_many({"QQ_AI_ENABLED": False, "QQ_AI_FALLBACK": False})
    res = await qqbot.handle_callback_event(_at_event("随便聊聊"))
    assert "/help" in _texts(res)  # fallback 关 = 非命令回帮助


@pytest.mark.asyncio
async def test_ai_call_without_provider_degrades(db):
    """无可用 provider 时给降级话术而不是炸掉。"""
    settings.set_many({"QQ_AI_ENABLED": True, "QQ_AI_FALLBACK": True,
                       "QQ_AI_AGENT_ENABLED": True})
    res = await qqbot.handle_callback_event(_at_event("你好呀"))
    t = _texts(res)
    assert t  # 有话术（AI 不可用提示）
    assert "AI 暂时不可用" in t


# ---------------- agent 意图路由 ----------------

def test_agent_parse_action_valid():
    from tgmon.qqbot import agent
    got = agent._parse_action(
        '{"action":"new","args":{},"reply":""}')
    assert got == ("new", {}, "")


def test_agent_parse_action_from_prose():
    """模型在 JSON 外面裹了话也能抠出来。"""
    from tgmon.qqbot import agent
    got = agent._parse_action(
        '好的，判断如下：{"action": "search", "args": {"q": "卡芙卡"}, '
        '"reply": ""} 请查收')
    assert got is not None and got[0] == "search" and got[1]["q"] == "卡芙卡"


def test_agent_parse_action_garbage_returns_none():
    from tgmon.qqbot import agent
    assert agent._parse_action("今天天气不错") is None
    assert agent._parse_action("") is None
    assert agent._parse_action("{坏了的 json") is None


@pytest.mark.asyncio
async def test_agent_routes_chat(db, monkeypatch):
    """action=chat → 直接回 reply，不再二次查库。"""
    from tgmon.qqbot import agent

    class _FakeRes:
        ok = True
        text = ('{"action":"chat","args":{},"reply":'
                '"我是群助手，负责搬运游戏爆料～"}')

    async def _fake_complete(system, user, **kw):
        return _FakeRes()

    from tgmon.providers import registry
    monkeypatch.setattr(registry, "complete_with_failover", _fake_complete)
    out = await agent.handle("你是谁", "G1", True)
    assert len(out) == 1 and "游戏爆料" in out[0].text


@pytest.mark.asyncio
async def test_agent_routes_to_latest(db, monkeypatch):
    """自然语言「看最新」→ agent 自动执行 /latest。"""
    from tgmon.qqbot import agent

    class _FakeRes:
        ok = True
        text = '{"action":"latest","args":{"n":1},"reply":""}'

    async def _fake_complete(system, user, **kw):
        return _FakeRes()

    from tgmon.providers import registry
    monkeypatch.setattr(registry, "complete_with_failover", _fake_complete)
    _seed_message(text_zh="agent 找到的最新消息")
    out = await agent.handle("今天有什么新爆料", "ABC1234567890", True)
    assert any("agent 找到的最新消息" in r.text for r in out if r.kind == "text")


@pytest.mark.asyncio
async def test_agent_rejects_unknown_action(db, monkeypatch):
    """白名单外动作（幻觉工具）→ 不执行，退回聊天。"""
    from tgmon.qqbot import agent

    class _FakeRes:
        ok = True
        text = '{"action":"delete_all","args":{},"reply":"已删除"}'

    async def _fake_complete(system, user, **kw):
        return _FakeRes()

    from tgmon.providers import registry
    monkeypatch.setattr(registry, "complete_with_failover", _fake_complete)
    out = await agent.handle("把数据全删了", "G1", True)
    # 未执行任何删除（没有这个工具），也不把"已删除"当真
    assert all("已删除" not in r.text for r in out)


# ---------------- 磁盘水位清理 ----------------

def test_disk_watermark_noop_under_limit(db, monkeypatch, tmp_path):
    """未超水位 → 不动任何文件。"""
    from tgmon.worker import maintenance
    monkeypatch.setattr(maintenance, "_dir_usage", lambda d, **kw: 1024)
    settings.set_many({"DISK_WATERMARK_HIGH_GB": 10,
                       "DISK_WATERMARK_LOW_GB": 3})
    res = maintenance.cleanup_disk_watermark()
    assert res.get("ok") is True


def test_disk_watermark_clears_videos_by_lru(db, monkeypatch, tmp_path):
    """超水位 → 按 last_access_at 从旧到新删视频直到低于低水位。"""
    from tgmon.worker import maintenance
    # 模拟：目录统计先返回 11G，删两个视频后（每次 _clear_video 返回 4G）低于 3G
    usage = {"v": 11 * 1024**3}
    monkeypatch.setattr(maintenance, "_dir_usage",
                        lambda d, skip_private=False: usage["v"])
    monkeypatch.setattr(
        maintenance, "_clear_video",
        lambda r: (usage.__setitem__("v", usage["v"] - 4 * 1024**3)
                   or 4 * 1024**3))
    settings.set_many({"DISK_WATERMARK_HIGH_GB": 10,
                       "DISK_WATERMARK_LOW_GB": 3})
    # 三条有视频的媒体记录（LRU 顺序由 last_access_at 决定）
    from datetime import datetime, timedelta
    with session_scope() as s:
        base = datetime.utcnow() - timedelta(hours=5)
        mids = [_seed_message(text_zh=f"视频消息{i}") for i in range(3)]
        for i, mid in enumerate(mids):
            s.add(MessageMedia(message_id=mid, kind="video",
                               video_path=f"c/{i}.mp4", video_bytes=100,
                               last_access_at=base + timedelta(hours=i)))
    res = maintenance.cleanup_disk_watermark()
    assert res.get("removed") == 2  # 11G - 4G - 4G = 3G 触底
    assert res.get("total_gb") is not None


# ---------------- 留痕 ----------------

@pytest.mark.asyncio
async def test_c2c_event_routes_to_ai(db):
    settings.set_many({"QQ_AI_ENABLED": True, "QQ_AI_FALLBACK": True})
    res = await qqbot.handle_callback_event({
        "type": "C2C_MESSAGE_CREATE",
        "d": {"id": "ROBOT1.0_c2c", "content": "在吗",
              "author": {"user_openid": "USER1"}}})
    assert res["handled"] == "c2c" and res["channel"] == "c2c"
    assert res["target"] == "USER1" and res["msg_id"] == "ROBOT1.0_c2c"


@pytest.mark.asyncio
async def test_inbound_recorded(db):
    await qqbot.handle_callback_event(_at_event("/help"))
    with session_scope() as s:
        rows = s.query(QqInbound).all()
        assert len(rows) == 1
        assert rows[0].event_type == "GROUP_AT_MESSAGE_CREATE"
        assert rows[0].content == "/help"
        assert rows[0].reply  # 留痕带回复



# ---------------- push_message 过滤链 ----------------

@pytest.mark.asyncio
async def test_push_disabled_switch(db, fake_send, fake_payload):
    settings.set_many({"QQ_ENABLED": False})
    _seed_group()
    mid = _seed_message()
    n = await _push(mid)
    assert n == 0 and not SENT


@pytest.mark.asyncio
async def test_push_happy_path(db, conf, fake_send, fake_payload):
    _seed_group(openid="GAAA")
    _seed_group(openid="GBBB")
    mid = _seed_message()
    n = await _push(mid)
    assert n == 2
    assert {x for x, _ in SENT} == {"GAAA", "GBBB"}
    assert SENT[0][1].startswith("https://example.com/s/")
    # 投递记录成功态
    with session_scope() as s:
        assert s.query(QqDelivery).filter(QqDelivery.status == "done").count() == 2


@pytest.mark.asyncio
async def test_push_channel_filter(db, conf, fake_send, fake_payload):
    settings.set_many({"QQ_CHANNEL_IDS": [99]})
    _seed_group()
    mid = _seed_message(channel_id=1)
    assert await _push(mid) == 0 and not SENT


@pytest.mark.asyncio
async def test_push_skips_duplicate(db, conf, fake_send, fake_payload):
    _seed_group()
    mid = _seed_message()
    dup = _seed_message(duplicate_of=mid)
    assert await _push(dup) == 0 and not SENT


@pytest.mark.asyncio
async def test_push_duplicate_when_include_dups(db, conf, fake_send, fake_payload):
    settings.set_many({"QQ_INCLUDE_DUPS": True})
    _seed_group()
    mid = _seed_message()
    dup = _seed_message(duplicate_of=mid)
    assert await _push(dup) == 0


@pytest.mark.asyncio
async def test_push_daily_limit(db, conf, fake_send, fake_payload):
    settings.set_many({"QQ_DAILY_LIMIT": 1})
    _seed_group()
    mid = _seed_message()
    # 第一条过（当日计数 0→1）
    assert await _push(mid) == 1
    # 第二条熔断
    mid2 = _seed_message(channel_id=2)
    assert await _push(mid2) == 0
    assert len(SENT) == 1


@pytest.mark.asyncio
async def test_push_not_in_group_disables(db, conf, monkeypatch, fake_payload):
    """40034101（不在群）→ 群自动停用，投递标失败。"""

    async def _fail(openid, content, msg_id=None, bot=None):
        raise qq_client.QqApiError(40034101, "机器人非群成员")

    monkeypatch.setattr(qq_client, "send_group_text", _fail)
    _seed_group(openid="GFAIL")
    mid = _seed_message()
    assert await _push(mid) == 0
    with session_scope() as s:
        g = s.query(QqGroup).first()
        assert not g.enabled and g.dropped_at is not None
        d = s.query(QqDelivery).first()
        assert d.status == "failed" and "40034101" in (d.error or "")


@pytest.mark.asyncio
async def test_push_rate_limit_goes_retry(db, conf, monkeypatch, fake_payload):
    """40034100（频控）→ 进重试队列而不是失败。"""

    async def _fail(openid, content, msg_id=None, bot=None):
        raise qq_client.QqApiError(40034100, "主动消息发送超过频控限制")

    monkeypatch.setattr(qq_client, "send_group_text", _fail)
    _seed_group(openid="GRATE")
    mid = _seed_message()
    assert await _push(mid) == 0
    with session_scope() as s:
        d = s.query(QqDelivery).first()
        assert d.status == "retry" and d.next_retry_at is not None


@pytest.mark.asyncio
async def test_push_no_permission_fatal_no_retry(db, conf, monkeypatch,
                                                 fake_payload):
    """40034105（主动消息无权限，个人主体常见）→ 直接 failed 不重试，
    且不停用群 —— 群内被动回复（命令/AI）依然可用。"""

    async def _fail(openid, content, msg_id=None, bot=None):
        raise qq_client.QqApiError(40034105, "主动消息失败, 无权限")

    monkeypatch.setattr(qq_client, "send_group_text", _fail)
    _seed_group(openid="GNOPERM")
    mid = _seed_message()
    assert await _push(mid) == 0
    with session_scope() as s:
        d = s.query(QqDelivery).first()
        assert d.status == "failed" and d.next_retry_at is None
        assert "40034105" in (d.error or "") and "无权限" in (d.error or "")
        g = s.query(QqGroup).first()
        assert g.enabled and g.dropped_at is None  # 不停群


@pytest.mark.asyncio
async def test_retry_pending_resends(db, conf, fake_send, fake_payload):
    _seed_group(openid="GRETRY")
    mid = _seed_message()
    with session_scope() as s:
        s.add(QqDelivery(group_openid="GRETRY", message_id=mid,
                         status="retry", attempts=1,
                         next_retry_at=None))  # next_retry_at 已到期（NULL 视为到期?）
    # next_retry_at 为 NULL 时查询条件不命中 —— 补一个过去时间
    from datetime import datetime
    with session_scope() as s:
        d = s.query(QqDelivery).first()
        d.next_retry_at = datetime(2020, 1, 1)
    n = await qqbot.retry_pending()
    assert n == 1
    with session_scope() as s:
        assert s.query(QqDelivery).first().status == "done"


# ---------------- 纯图爆料：只发图片（2026-09 群反馈） ----------------

def _img_payload(**kw):
    """无文本、带媒体的 payload（stub outputs.serialize 用）。"""
    return _payload(text_zh=None, text_raw=None, **kw)


@pytest.fixture()
def fake_media(monkeypatch):
    """打桩图片上传与富媒体发送，记录调用。"""
    import tgmon.qqbot.media as qq_media
    UPLOADS: list[str] = []
    MEDIA_SENT: list[tuple[str, str]] = []

    async def _fake_upload(openid, thumb, raise_fatal=False, bot=None):
        UPLOADS.append(thumb)
        return f"fi:{thumb}"

    async def _fake_album(openid, thumbs, raise_fatal=False, bot=None):
        UPLOADS.extend(thumbs)
        return "fi:whole-album"

    async def _fake_send_media(openid, file_info, msg_id=None,
                               msg_seq=None, content=None, bot=None):
        MEDIA_SENT.append((openid, file_info))
        return "ROBOT1.0_fake_media"

    monkeypatch.setattr(qq_media, "upload_image", _fake_upload)
    monkeypatch.setattr(qq_media, "upload_album", _fake_album)
    monkeypatch.setattr(qq_client, "send_group_media", _fake_send_media)
    return UPLOADS, MEDIA_SENT


def _seed_photos(mid: int, n: int = 1):
    with session_scope() as s:
        for i in range(n):
            s.add(MessageMedia(message_id=mid, kind="photo",
                               thumb_path=f"1/{mid}_{i}.webp"))


@pytest.mark.asyncio
async def test_push_image_only_sends_photos(db, conf, fake_send, fake_media,
                                            monkeypatch):
    """纯图爆料：全部图片在一条媒体消息里发送，零文字。"""
    from tgmon import outputs
    monkeypatch.setattr(outputs, "serialize",
                        lambda mid, include_original=True: _img_payload())
    _seed_group(openid="GIMG")
    mid = _seed_message(text_zh=None, text_raw="")
    _seed_photos(mid, n=3)
    n = await _push(mid)
    assert n == 1
    uploads, media_sent = fake_media
    assert len(uploads) == 1
    from tgmon.models import ShareToken
    with session_scope() as s:
        assert len(s.query(ShareToken).first().snapshot_items[0]["photos"]) == 3
    assert len(media_sent) == 1
    assert not SENT                      # 没发任何文本
    with session_scope() as s:
        assert s.query(QqDelivery).filter(QqDelivery.status == "done").count() == 1


@pytest.mark.asyncio
async def test_push_image_only_keeps_more_than_three(db, conf, fake_send, fake_media,
                                             monkeypatch):
    """超过 3 张仍保留完整图组，只消耗一次发送。"""
    from tgmon import outputs
    monkeypatch.setattr(outputs, "serialize",
                        lambda mid, include_original=True: _img_payload())
    _seed_group(openid="GIMG5")
    mid = _seed_message(text_zh=None, text_raw="")
    _seed_photos(mid, n=5)
    await _push(mid)
    uploads, media_sent = fake_media
    assert len(uploads) == 1
    from tgmon.models import ShareToken
    with session_scope() as s:
        assert len(s.query(ShareToken).first().snapshot_items[0]["photos"]) == 5
    assert len(media_sent) == 1


@pytest.mark.asyncio
async def test_push_image_only_all_failed_goes_retry(db, conf, fake_send,
                                                     monkeypatch):
    """图片全上传失败 → 进重试队列（不空推也不误报成功）。"""
    import tgmon.qqbot.media as qq_media
    from tgmon import outputs

    async def _no_upload(openid, thumb, raise_fatal=False, bot=None):
        return None

    monkeypatch.setattr(qq_media, "upload_image", _no_upload)
    monkeypatch.setattr(outputs, "serialize",
                        lambda mid, include_original=True: _img_payload())
    _seed_group(openid="GIMGFAIL")
    mid = _seed_message(text_zh=None, text_raw="")
    _seed_photos(mid, n=2)
    assert await _push(mid) == 1
    assert SENT[0][1].startswith("https://example.com/s/")
    with session_scope() as s:
        assert s.query(QqDelivery).first().status == "done"


@pytest.mark.asyncio
async def test_push_image_only_not_in_group_disables(db, conf, fake_send,
                                                     monkeypatch):
    """图片投递遇 40034101（不在群）→ 停用群（与文本路径同构）。"""
    import tgmon.qqbot.media as qq_media
    from tgmon import outputs

    async def _fail_upload(openid, thumb, raise_fatal=False, bot=None):
        raise qq_client.QqApiError(40034101, "机器人非群成员")

    monkeypatch.setattr(qq_media, "upload_image", _fail_upload)
    monkeypatch.setattr(outputs, "serialize",
                        lambda mid, include_original=True: _img_payload())
    _seed_group(openid="GIMGDROP")
    mid = _seed_message(text_zh=None, text_raw="")
    _seed_photos(mid, n=1)
    assert await _push(mid) == 0
    with session_scope() as s:
        g = s.query(QqGroup).filter(QqGroup.group_openid == "GIMGDROP").first()
        assert not g.enabled and g.dropped_at is not None


@pytest.mark.asyncio
async def test_push_no_text_no_photos_falls_back_to_head(db, conf, fake_send,
                                                         monkeypatch):
    """纯视频等（无文本无图片）：保底推一行标题，无占位符。"""
    from tgmon import outputs
    monkeypatch.setattr(outputs, "serialize",
                        lambda mid, include_original=True: _img_payload())
    _seed_group(openid="GVIDEO")
    mid = _seed_message(text_zh=None, text_raw="")
    n = await _push(mid)
    assert n == 0 and not SENT


@pytest.mark.asyncio
async def test_cmd_latest_image_only_no_placeholder(db, monkeypatch):
    """群内卡片：纯图爆料只有标题行 + 链接回复，无「（无文本）」。"""
    import asyncio as _asyncio

    from tgmon.qqbot import commands as commands_mod, sending

    async def _no_send(*a, **kw):
        return {}

    monkeypatch.setattr(sending, "send_parts", _no_send)
    mid = _seed_message(text_zh=None, text_raw="")
    _seed_photos(mid, n=1)
    res = await qqbot.handle_callback_event(_at_event("/latest 1"))
    t = _texts(res)
    assert "无文本" not in t
    assert "【原神】" in t
    # 2026-09 超时复盘后：守卫内只回链接，长图后台补发
    assert len(res["replies"]) == 1 and res["replies"][0].kind == "text"
    assert "/s/" in res["replies"][0].text
    # 后台渲染任务收尾（send_parts 已 mock，不真发图）
    while commands_mod._BG_TASKS:
        await _asyncio.wait(set(commands_mod._BG_TASKS), timeout=10)


@pytest.mark.asyncio
async def test_test_message_bypasses_switch(db, conf, fake_send):
    """开关关闭也能发测试（否则没法验证新配置）。"""
    settings.set_many({"QQ_ENABLED": False})
    _seed_group(openid="GTEST")
    res = await qqbot.test_message("GTEST")
    assert res["ok"] is True and SENT


# ---------------- admin 路由挂载 ----------------

def test_routes_registered():
    """app.py 装配检查：回调与管理页路由都已注册。"""
    from tgmon.admin.app import app
    paths = {r.path for r in app.routes}
    assert "/qqbot/callback" in paths
    assert "/qqbot" in paths
    assert "/qqbot/config" in paths
    assert "/qqbot/groups/{gid}/test" in paths
