"""多机器人接入测试。

QQ 平台的 group_openid / member_openid 按机器人发放（同一物理群不同
机器人拿到的 ID 不同），所以数据层天然隔离；多机器人的关键行为是：
1. 凭据解析：resolve_bot 的单机器人回落 / 多机器人必须带上下文
2. token 缓存按 app_id 隔离，互不覆盖
3. Webhook 按 X-Bot-Appid 路由 + 用对应密钥验签
4. 进群事件记录群归属（QqGroup.bot_id）
5. 推送扇出只给「归属机器人可用」的群，发送用归属机器人凭据
6. WS 桥按报文 bot_appid 路由
"""
from __future__ import annotations

import json

import pytest

from tgmon import settings
from tgmon.db import engine, session_scope
from tgmon.models import (
    Base, Channel, MonitorMessage, QqBot, QqBotCapability, QqDelivery, QqGroup,
    QqPending,
)
from tgmon.qqbot import client as qq_client

SECRET_A = "botA-secret-0000000000000000"
SECRET_B = "botB-secret-0000000000000000"


@pytest.fixture()
def db():
    Base.metadata.create_all(engine)
    settings.set_many({"BASE_URL": "https://example.com",
                       "QQ_BRIDGE_TOKEN": "bridge-tkn"})
    yield
    with session_scope() as s:
        for t in (QqPending, QqDelivery, QqGroup, MonitorMessage, Channel, QqBot):
            s.query(t).delete()
        from tgmon.models import AppSetting
        for r in s.query(AppSetting).all():
            s.delete(r)
    settings.invalidate()
    qq_client.invalidate_token()


def _add_bot(app_id: str, secret: str, nickname: str = "",
             enabled: bool = True) -> int:
    from tgmon.crypto import encrypt
    with session_scope() as s:
        row = QqBot(app_id=app_id, nickname=nickname, enabled=enabled,
                    app_secret_enc=encrypt(secret))
        s.add(row)
        s.flush()
        return row.id


def _seed_group(openid: str, bot_id: int | None = None,
                enabled: bool = True) -> int:
    with session_scope() as s:
        g = QqGroup(group_openid=openid, bot_id=bot_id, enabled=enabled)
        s.add(g)
        s.flush()
        return g.id


# ---------------- 凭据解析 ----------------

def test_resolve_single_bot_row(db):
    bid = _add_bot("10001", SECRET_A, "甲")
    bot = qq_client.resolve_bot()
    assert bot["id"] == bid and bot["app_id"] == "10001"
    assert bot["app_secret"] == SECRET_A  # 解密后的明文


def test_resolve_legacy_settings_fallback(db):
    settings.set_many({"QQ_APP_ID": "90001", "QQ_APP_SECRET": "legacy-secret"})
    bot = qq_client.resolve_bot()
    assert bot["id"] == 0 and bot["app_id"] == "90001"
    assert bot["app_secret"] == "legacy-secret"


def test_resolve_multiple_requires_context(db):
    _add_bot("10001", SECRET_A)
    _add_bot("10002", SECRET_B)
    with pytest.raises(qq_client.QqApiError):
        qq_client.resolve_bot()


def test_resolve_by_app_id(db):
    _add_bot("10001", SECRET_A)
    _add_bot("10002", SECRET_B)
    bot = qq_client.resolve_bot(app_id="10002")
    assert bot["app_secret"] == SECRET_B


def test_resolve_disabled_bot_rejected(db):
    _add_bot("10001", SECRET_A, enabled=False)
    with pytest.raises(qq_client.QqApiError):
        qq_client.resolve_bot(bot_id=1)
        # 也不用回落：没有启用的机器人且旧配置为空
    with pytest.raises(qq_client.QqApiError):
        qq_client.resolve_bot()


@pytest.mark.asyncio
async def test_token_cache_per_bot(db, monkeypatch):
    """两个机器人各自的 token 独立缓存，换凭据互不影响。"""
    _add_bot("10001", SECRET_A)
    _add_bot("10002", SECRET_B)
    fetched: list[str] = []

    async def _fake_fetch(app_id, app_secret):
        fetched.append(app_id)
        return f"token-{app_id}", 9999999999.0

    monkeypatch.setattr(qq_client, "_fetch_token", _fake_fetch)
    a1 = qq_client.resolve_bot(app_id="10001")
    b1 = qq_client.resolve_bot(app_id="10002")
    assert await qq_client._get_token(a1) == "token-10001"
    assert await qq_client._get_token(b1) == "token-10002"
    # 缓存命中：第二次不再触发网络请求
    assert await qq_client._get_token(a1) == "token-10001"
    assert fetched == ["10001", "10002"]
    # 失效单个机器人后，只有它重新换取
    qq_client.invalidate_token(a1)
    assert await qq_client._get_token(a1) == "token-10001"
    assert fetched == ["10001", "10002", "10001"]


# ---------------- 群归属 ----------------

@pytest.mark.asyncio
async def test_group_add_records_owner(db):
    bid = _add_bot("10001", SECRET_A, "甲")
    from tgmon import qqbot
    await qqbot.handle_callback_event({
        "type": "GROUP_ADD_ROBOT", "d": {"group_openid": "GGGG0001"}},
        bot={"id": bid, "app_id": "10001", "app_secret": SECRET_A})
    with session_scope() as s:
        g = s.query(QqGroup).filter_by(group_openid="GGGG0001").first()
        assert g is not None and g.bot_id == bid and g.enabled


@pytest.mark.asyncio
async def test_group_add_without_bot_keeps_null(db):
    """无机器人上下文（单机器人旧链路）进群：归属留空，单机器人时照推。"""
    from tgmon import qqbot
    await qqbot.handle_callback_event({
        "type": "GROUP_ADD_ROBOT", "d": {"group_openid": "GGGG0002"}})
    with session_scope() as s:
        g = s.query(QqGroup).filter_by(group_openid="GGGG0002").first()
        assert g is not None and g.bot_id is None


# ---------------- 推送扇出 ----------------

def _seed_message(mid_suffix: str) -> int:
    with session_scope() as s:
        ch = s.get(Channel, 1)
        if ch is None:
            s.add(Channel(id=1, title="Ch", tg_id="-1001"))
        import itertools
        seq = next(itertools.count(9000))
        m = MonitorMessage(channel_id=1, tg_message_id=f"m{mid_suffix}{seq}",
                           text_zh="正文", text_raw="raw")
        s.add(m)
        s.flush()
        return m.id


def _payload(mid):
    return {"channel": {"id": 1, "title": "Seele Leaks", "game": ""},
            "game_detected": "原神", "id": mid, "game": "原神",
            "text": "正文", "text_zh": "正文", "photos": [], "has_media": False}


def test_enqueue_skips_unowned_groups_when_multi_bot(db, monkeypatch):
    """两个机器人时：归属明确的群排队，无归属的存量群跳过。"""
    b1 = _add_bot("10001", SECRET_A, "甲")
    b2 = _add_bot("10002", SECRET_B, "乙")
    _seed_group("GOWNED1", bot_id=b1)
    _seed_group("GOWNED2", bot_id=b2)
    _seed_group("GLEGACY", bot_id=None)
    settings.set_many({"QQ_ENABLED": True, "QQ_CHANNEL_IDS": []})

    mid = _seed_message("a")
    import tgmon.qqbot.batches as batches_mod
    monkeypatch.setattr(batches_mod, "by_ids",
                        lambda ids: [_payload(ids[0])] if ids else [])
    count = batches_mod.enqueue(mid)
    with session_scope() as s:
        targets = {r.group_openid for r in s.query(QqPending).all()}
    assert count == 2
    assert targets == {"GOWNED1", "GOWNED2"}


def test_enqueue_unowned_group_single_bot(db, monkeypatch):
    """单机器人：无归属存量群照常推送（旧部署零变化）。"""
    _add_bot("10001", SECRET_A, "甲")
    _seed_group("GLEGACY", bot_id=None)
    settings.set_many({"QQ_ENABLED": True, "QQ_CHANNEL_IDS": []})
    import tgmon.qqbot.batches as batches_mod
    monkeypatch.setattr(batches_mod, "by_ids",
                        lambda ids: [_payload(ids[0])] if ids else [])
    mid = _seed_message("b")
    assert batches_mod.enqueue(mid) == 1


def test_enqueue_skips_bot_with_disabled_proactive_delivery(db, monkeypatch):
    """A bot that cannot send proactive messages must stop generating attempts."""
    b1 = _add_bot("10001", SECRET_A, "甲")
    b2 = _add_bot("10002", SECRET_B, "乙")
    with session_scope() as s:
        s.add(QqBotCapability(bot_id=b1, proactive_enabled=False))
    _seed_group("GDISABLED", bot_id=b1)
    _seed_group("GENABLED", bot_id=b2)
    settings.set_many({"QQ_ENABLED": True, "QQ_CHANNEL_IDS": []})
    import tgmon.qqbot.batches as batches_mod
    monkeypatch.setattr(batches_mod, "by_ids",
                        lambda ids: [_payload(ids[0])] if ids else [])
    mid = _seed_message("proactive")
    assert batches_mod.enqueue(mid) == 1
    with session_scope() as s:
        assert {r.group_openid for r in s.query(QqPending).all()} == {"GENABLED"}


# ---------------- Webhook 路由 ----------------

def _sign(secret: str, ts: str, body: bytes) -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    seed = qq_client._seed_from_secret(secret)
    priv = Ed25519PrivateKey.from_private_bytes(seed)
    return priv.sign(ts.encode() + body).hex()


def _client():
    from fastapi.testclient import TestClient
    from tgmon.admin.app import app
    return TestClient(app)


def test_webhook_routes_by_appid_header(db):
    """带 X-Bot-Appid 的已签名事件 → 用对应机器人的密钥验签通过。"""
    _add_bot("10001", SECRET_A, "甲")
    _add_bot("10002", SECRET_B, "乙")
    body = json.dumps({"op": 0, "t": "GROUP_ADD_ROBOT",
                       "d": {"group_openid": "WHGROUP0001"}}).encode()
    ts = "1725442399"
    sig = _sign(SECRET_B, ts, body)  # 用乙的密钥签
    r = _client().post("/qqbot/callback", content=body, headers={
        "Content-Type": "application/json",
        "X-Bot-Appid": "10002",
        "X-Signature-Ed25519": sig,
        "X-Signature-Timestamp": ts,
    })
    assert r.status_code == 200 and r.json() == {"op": 12, "d": 0}
    with session_scope() as s:
        g = s.query(QqGroup).filter_by(group_openid="WHGROUP0001").first()
        assert g is not None and g.bot_id == 2  # 归属乙


def test_webhook_wrong_secret_rejected(db):
    """AppID 头指向乙、但签名用甲的密钥 → 拒绝。"""
    _add_bot("10001", SECRET_A, "甲")
    _add_bot("10002", SECRET_B, "乙")
    body = json.dumps({"op": 0, "t": "GROUP_DEL_ROBOT",
                       "d": {"group_openid": "X"}}).encode()
    ts = "1725442399"
    sig = _sign(SECRET_A, ts, body)
    r = _client().post("/qqbot/callback", content=body, headers={
        "Content-Type": "application/json",
        "X-Bot-Appid": "10002",
        "X-Signature-Ed25519": sig,
        "X-Signature-Timestamp": ts,
    })
    assert r.status_code == 403


def test_webhook_no_header_tries_all_bots(db):
    """无 AppID 头的多机器人部署：逐个密钥试，对上即放行。"""
    _add_bot("10001", SECRET_A, "甲")
    _add_bot("10002", SECRET_B, "乙")
    body = json.dumps({"op": 0, "t": "GROUP_ADD_ROBOT",
                       "d": {"group_openid": "NOHDR0001"}}).encode()
    ts = "1725442399"
    sig = _sign(SECRET_B, ts, body)
    r = _client().post("/qqbot/callback", content=body, headers={
        "Content-Type": "application/json",
        "X-Signature-Ed25519": sig,
        "X-Signature-Timestamp": ts,
    })
    assert r.status_code == 200
    with session_scope() as s:
        g = s.query(QqGroup).filter_by(group_openid="NOHDR0001").first()
        assert g is not None and g.bot_id == 2


def test_op13_per_bot_secret(db):
    """op13 校验请求带 AppID 头 → 用对应机器人的密钥签名。"""
    _add_bot("10001", SECRET_A, "甲")
    _add_bot("10002", SECRET_B, "乙")
    body = json.dumps({"op": 13, "d": {"plain_token": "PT", "event_ts": "123"}}).encode()
    r = _client().post("/qqbot/callback", content=body, headers={
        "Content-Type": "application/json",
        "X-Bot-Appid": "10002",
    })
    assert r.status_code == 200
    expect = qq_client.sign_validation(SECRET_B, "123", "PT")
    assert r.json()["signature"] == expect


def test_op13_multi_bot_no_header_rejected(db):
    """无头 + 多机器人：签名身份无法确定，明确拒绝而不是赌一个。"""
    _add_bot("10001", SECRET_A, "甲")
    _add_bot("10002", SECRET_B, "乙")
    body = json.dumps({"op": 13, "d": {"plain_token": "PT", "event_ts": "123"}}).encode()
    r = _client().post("/qqbot/callback", content=body, headers={
        "Content-Type": "application/json"})
    assert r.status_code == 400


# ---------------- WS 桥路由 ----------------

def test_bridge_routes_by_bot_appid(db, monkeypatch):
    """桥事件带 bot_appid → 用对应机器人处理；进群归属正确。"""
    _add_bot("10001", SECRET_A, "甲")
    bid_b = _add_bot("10002", SECRET_B, "乙")
    body = json.dumps({"op": 0, "t": "GROUP_ADD_ROBOT",
                       "d": {"group_openid": "BRGROUP0001"},
                       "bot_appid": "10002"}).encode()
    r = _client().post("/qqbot/bridge", content=body, headers={
        "Content-Type": "application/json",
        "X-Bridge-Token": "bridge-tkn"})
    assert r.status_code == 200
    with session_scope() as s:
        g = s.query(QqGroup).filter_by(group_openid="BRGROUP0001").first()
        assert g is not None and g.bot_id == bid_b


def test_bridge_unknown_appid_rejected(db):
    _add_bot("10001", SECRET_A, "甲")
    body = json.dumps({"op": 0, "t": "GROUP_ADD_ROBOT",
                       "d": {"group_openid": "X"},
                       "bot_appid": "99999"}).encode()
    r = _client().post("/qqbot/bridge", content=body, headers={
        "Content-Type": "application/json",
        "X-Bridge-Token": "bridge-tkn"})
    assert r.status_code == 403


def test_bridge_heartbeat_touches_bot(db):
    """心跳按机器人记录 bridge_last_seen。"""
    _add_bot("10001", SECRET_A, "甲")
    body = json.dumps({"op": 14, "t": "BRIDGE_PING", "d": {},
                       "bot_appid": "10001"}).encode()
    r = _client().post("/qqbot/bridge", content=body, headers={
        "Content-Type": "application/json",
        "X-Bridge-Token": "bridge-tkn"})
    assert r.status_code == 200
    with session_scope() as s:
        row = s.query(QqBot).filter_by(app_id="10001").first()
        assert row.bridge_last_seen is not None


# ---------------- 投递用归属机器人凭据 ----------------

@pytest.mark.asyncio
async def test_deliver_uses_group_owner_credentials(db, monkeypatch):
    """群归属乙 → 发送调用带乙的凭据上下文。"""
    _add_bot("10001", SECRET_A, "甲")
    bid_b = _add_bot("10002", SECRET_B, "乙")
    _seed_group("GDELIVER", bot_id=bid_b)
    settings.set_many({"QQ_ENABLED": True})
    seen: list[dict] = []

    async def _fake_send(target, replies, *, key, private=False,
                         msg_id=None, bot=None, seq_start=1):
        seen.append(bot)
        from datetime import datetime
        return {"1": {"status": "done"}}

    import tgmon.qqbot.batches as batches_mod
    import tgmon.qqbot.sending as sending_mod
    monkeypatch.setattr(sending_mod, "send_parts", _fake_send)
    monkeypatch.setattr(batches_mod, "create_bundle", lambda mids: 777)
    monkeypatch.setattr(batches_mod, "bundle_cards",
                        lambda sid: ([], "https://example.com/s/777", [{"id": 1}]))

    # 直接构造投递行，走 deliver 的归属解析路径
    with session_scope() as s:
        d = QqDelivery(group_openid="GDELIVER", message_id=1, bundle_id=777,
                       dedup_key="k1", status="pending", parts={})
        s.add(d)
        s.flush()
        did = d.id
        s.add(QqPending(group_openid="GDELIVER", message_id=1, game="原神",
                        status="queued", delivery_id=did,
                        ready_at=__import__("datetime").datetime.utcnow()))
    await batches_mod.deliver(did)
    assert seen and seen[0] is not None
    assert seen[0]["app_id"] == "10002" and seen[0]["app_secret"] == SECRET_B
