"""相册合并（去重第 1.5 层）测试。

复现场景（2026-09-04 反馈）：3 张图的爆料因实时防抖切分 / 补拉 limit
边界切分被拆成多个批次，无文本的部分批次绕过全部判重层各自入库 ——
消息页出现 1图/2图/3图 递增的三条记录。修复后同
(channel_id, grouped_id) 的后续批次并入首条记录（anchor），不再新建。
"""
from __future__ import annotations

import pytest

from tgmon import pipeline
from tgmon.db import engine, session_scope
from tgmon.media import MediaOut
from tgmon.models import (
    AppSetting, Base, Channel, MessageMedia, MonitorMessage, Task,
)

GID = "770011"


class FakeMsg:
    """Telethon Message 的最小替身：pipeline 只读这几个属性。"""

    def __init__(self, mid: int, grouped_id=None, text="", has_media=True):
        self.id = mid
        self.grouped_id = grouped_id
        self.message = text
        self.media = "photo" if has_media else None
        self.date = None
        self.post_author = None
        self.entities = None


def _out(mid: int) -> MediaOut:
    """按 media.py 的命名规则造缩略图输出（thumb = 频道/消息id.webp）。"""
    return MediaOut(kind="photo", thumb_path=f"1/{mid}.webp",
                    thumb_bytes=100, width=100, height=100,
                    orig_bytes=1000, mime="image/webp",
                    phash=f"{mid:016x}", dhash=f"{mid:016x}")


@pytest.fixture()
def db():
    Base.metadata.create_all(engine)
    yield
    with session_scope() as s:
        for t in (Task, MessageMedia, MonitorMessage, Channel):
            s.query(t).delete()
        for r in s.query(AppSetting).all():
            s.delete(r)


@pytest.fixture()
def ch(db):
    with session_scope() as s:
        c = Channel(id=1, title="Seele Leaks", tg_id="-1001234",
                    enabled=True, translate=True)
        s.add(c)


@pytest.fixture()
def fake_media(monkeypatch):
    """媒体处理打桩：不下真图，按消息 id 产出缩略图（与真实命名一致）。"""
    async def _fake(client, message, channel_id, **kw):
        if not getattr(message, "media", None):
            return None
        return _out(message.id)
    monkeypatch.setattr("tgmon.media.process", _fake)


@pytest.fixture()
def fake_translate(monkeypatch):
    from tgmon import translate as tr

    async def _fake(text, prompt, game, entities_data, hits=None):
        return tr.TranslateResult(text_zh="译好了", status="ok")
    monkeypatch.setattr(tr, "translate", _fake)


def _counts() -> dict:
    with session_scope() as s:
        return {
            "msgs": s.query(MonitorMessage).count(),
            "media": s.query(MessageMedia).count(),
            "tasks": s.query(Task).filter(Task.kind == "retranslate").count(),
        }


@pytest.mark.asyncio
async def test_partial_then_full_merges(ch, fake_media):
    """1图批次先入库，全量批次（带 caption）到达 → 并入，不新建记录。"""
    # 第一批：防抖切出的单图（无文本）
    mid = await pipeline.ingest(None, 1, [FakeMsg(502, grouped_id=GID)])
    assert mid is not None
    assert _counts() == {"msgs": 1, "media": 1, "tasks": 0}

    # 第二批：对账补拉拿到的全量相册（caption 在首条上）
    batch = [FakeMsg(500, grouped_id=GID, text="新角色立绘爆料，含技能展示"),
             FakeMsg(501, grouped_id=GID),
             FakeMsg(502, grouped_id=GID)]
    ret = await pipeline.ingest(None, 1, batch)
    assert ret is None                     # 并入 anchor，不新建、不重推
    assert _counts() == {"msgs": 1, "media": 3, "tasks": 1}

    with session_scope() as s:
        m = s.query(MonitorMessage).one()
        assert m.tg_message_id == "502"    # 首批记录就是 anchor
        assert m.grouped_id == GID
        assert m.text_raw == "新角色立绘爆料，含技能展示"
        assert m.translate_status == "pending"   # 补了文本，等重译任务
        assert m.has_media
        thumbs = sorted(x.thumb_path for x in m.media)
        assert thumbs == ["1/500.webp", "1/501.webp", "1/502.webp"]
        t = s.query(Task).filter(Task.kind == "retranslate").one()
        assert t.payload["message_id"] == m.id


@pytest.mark.asyncio
async def test_merge_is_idempotent(ch, fake_media):
    """同一批次重复投递 → 媒体不重复挂、任务不重复排、记录不新增。"""
    await pipeline.ingest(None, 1, [FakeMsg(502, grouped_id=GID)])
    batch = [FakeMsg(500, grouped_id=GID, text="爆料文本" * 5),
             FakeMsg(501, grouped_id=GID),
             FakeMsg(502, grouped_id=GID)]
    await pipeline.ingest(None, 1, batch)
    # 第三次：再送一遍全量
    await pipeline.ingest(None, 1, batch)
    assert _counts() == {"msgs": 1, "media": 3, "tasks": 1}


@pytest.mark.asyncio
async def test_race_persist_recheck_merges(ch, fake_media, fake_translate,
                                           monkeypatch):
    """并发兜底：读侧预检没查到（模拟竞态窗口），_persist 写事务内
    复查发现 anchor → 并入且直接复用本批已完成的翻译（不再排任务）。"""
    # 手工预置 anchor（同 grouped_id，另一条 tg 消息、无文本、1 图）
    with session_scope() as s:
        s.add(MonitorMessage(channel_id=1, tg_message_id="499",
                             grouped_id=GID, translate_status="skipped"))
        s.add(MessageMedia(message_id=1, kind="photo", thumb_path="1/502.webp"))

    monkeypatch.setattr(pipeline, "_find_album_anchor",
                        lambda cid, gid: None)   # 读侧预检失效
    batch = [FakeMsg(500, grouped_id=GID, text="race caption " + "x" * 30),
             FakeMsg(501, grouped_id=GID),
             FakeMsg(502, grouped_id=GID)]
    ret = await pipeline.ingest(None, 1, batch)
    assert ret is None
    assert _counts() == {"msgs": 1, "media": 3, "tasks": 0}

    with session_scope() as s:
        m = s.query(MonitorMessage).one()
        assert m.tg_message_id == "499"
        assert "race caption" in (m.text_raw or "")
        assert m.text_zh == "译好了"         # 本批翻译结果直接复用
        assert m.translate_status == "ok"
        assert len(m.media) == 3


@pytest.mark.asyncio
async def test_singles_without_group_unchanged(ch, fake_media):
    """无 grouped_id 的单图消息：行为不变，各入各的记录。"""
    a = await pipeline.ingest(None, 1, [FakeMsg(601, text="第一条")])
    b = await pipeline.ingest(None, 1, [FakeMsg(602, text="第二条")])
    assert a and b and a != b
    assert _counts()["msgs"] == 2


@pytest.mark.asyncio
async def test_full_album_first_then_partial(ch, fake_media, fake_translate):
    """全量先到（正常路径），后到的部分批次并入不产生重复。"""
    full = [FakeMsg(500, grouped_id=GID,
                    text="full album caption with new kit details " + "y" * 30),
            FakeMsg(501, grouped_id=GID),
            FakeMsg(502, grouped_id=GID)]
    mid = await pipeline.ingest(None, 1, full)
    assert mid is not None
    assert _counts() == {"msgs": 1, "media": 3, "tasks": 0}

    # 实时通道迟到的单图事件
    ret = await pipeline.ingest(None, 1, [FakeMsg(502, grouped_id=GID)])
    assert ret is None
    assert _counts() == {"msgs": 1, "media": 3, "tasks": 0}

    with session_scope() as s:
        m = s.query(MonitorMessage).one()
        assert m.text_zh == "译好了"          # 纯英文 caption，整条送翻
        assert len(m.media) == 3


# ---------------- 历史修复脚本（scripts/repair_albums.py） ----------------

def _load_repair():
    """按文件路径加载脚本模块（scripts 不是包）。"""
    import importlib.util
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "scripts" / "repair_albums.py"
    spec = importlib.util.spec_from_file_location("repair_albums", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed_split_album():
    """复现截图场景：同一相册 1图/2图/3图 递增三条记录（无 grouped 修复的存量形态）。"""
    with session_scope() as s:
        a = MonitorMessage(channel_id=1, tg_message_id="502", grouped_id=GID,
                           translate_status="skipped", published_at=__import__(
                               "datetime").datetime(2026, 9, 4, 15, 38))
        b = MonitorMessage(channel_id=1, tg_message_id="501", grouped_id=GID,
                           translate_status="skipped")
        c = MonitorMessage(channel_id=1, tg_message_id="500", grouped_id=GID,
                           text_raw="new character kit leak",
                           text_zh="新角色套件爆料", translate_status="ok")
        d = MonitorMessage(channel_id=1, tg_message_id="490",
                           text_raw="无关单条", translate_status="ok")
        s.add_all([a, b, c, d])
        s.flush()
        for mid, paths in ((a.id, ["1/502.webp"]),
                           (b.id, ["1/501.webp", "1/502.webp"]),
                           (c.id, ["1/500.webp", "1/501.webp", "1/502.webp"])):
            for p in paths:
                s.add(MessageMedia(message_id=mid, kind="photo", thumb_path=p))
        return a.id, b.id, c.id, d.id


def test_repair_albums_dry_run_then_apply(ch):
    """dry-run 不落库；apply 收敛为一条 anchor，标记 album 重复，幂等。"""
    mod = _load_repair()
    a_id, b_id, c_id, d_id = _seed_split_album()

    # dry-run：统计正确且不落库
    stats = mod.repair(commit=False)
    assert stats["groups"] == 1
    assert stats["marked_dup"] == 2
    assert stats["dropped_dup_media"] == 3   # B 的两张 + A 的一张都是重复图
    with session_scope() as s:
        assert s.query(MonitorMessage).filter(
            MonitorMessage.duplicate_of.isnot(None)).count() == 0
        assert s.query(MessageMedia).count() == 6

    # apply
    stats = mod.repair(commit=True)
    assert stats["groups"] == 1 and stats["marked_dup"] == 2
    with session_scope() as s:
        # 记录不删，只有 C（有文本、媒体最全）是 anchor
        assert s.query(MonitorMessage).count() == 4
        anchor = s.get(MonitorMessage, c_id)
        assert anchor.duplicate_of is None
        assert len(anchor.media) == 3          # 500/501/502 各一张
        for rid in (a_id, b_id):
            r = s.get(MonitorMessage, rid)
            assert r.duplicate_of == c_id and r.dup_reason == "album"
            assert not r.media                 # 媒体已移交/丢弃
        assert s.get(MonitorMessage, d_id).duplicate_of is None   # 无关单条不动
        # 丢弃的是「anchor 已有同图」的行，保留 3 行
        assert s.query(MessageMedia).count() == 3

    # 幂等：再跑一遍无事发生
    stats = mod.repair(commit=True)
    assert stats["groups"] == 0 and stats["already_ok"] == 1


def test_repair_albums_phash_marked_sibling_untouched(ch):
    """组内已被标 phash 重复的兄弟：不参与收敛也不被改写。"""
    mod = _load_repair()
    a_id, b_id, c_id, _ = _seed_split_album()
    with session_scope() as s:
        outside = MonitorMessage(channel_id=1, tg_message_id="400",
                                 text_raw="更早的同图贴", translate_status="ok")
        s.add(outside)
        s.flush()
        s.add(MessageMedia(message_id=outside.id, kind="photo",
                           thumb_path="1/400.webp"))
        s.get(MonitorMessage, a_id).duplicate_of = outside.id
        s.get(MonitorMessage, a_id).dup_reason = "phash"

    stats = mod.repair(commit=True)
    assert stats["groups"] == 1 and stats["marked_dup"] == 1   # 只收敛 B
    with session_scope() as s:
        a = s.get(MonitorMessage, a_id)
        assert a.duplicate_of != c_id and a.dup_reason == "phash"   # 保持原判
        assert s.get(MonitorMessage, b_id).duplicate_of == c_id
        anchor = s.get(MonitorMessage, c_id)
        assert len(anchor.media) == 3
