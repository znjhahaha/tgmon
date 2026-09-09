# -*- coding: utf-8 -*-
"""distinct_stories 可见性验证：

全局视图（原语义回归）：
- A 同频道连发同标签（短文本）不同图 → 全部可见
- B 跨频道同长文本转发 → 只显示最早一条
- C 同频道同长文本重发 → 只显示最早一条

频道视图（2026-09 修复：按频道筛选时只藏同频道重复）：
- D 跨频道判重（dup_of 指向别的频道，#315/#316 场景）→ 可见
- E 同频道判重（dup_of 指向本频道）→ 仍隐藏
- F 跨频道同文首发遮蔽 → 频道视图下可见（全局仍隐藏）
"""
from datetime import datetime, timedelta

if __name__ != "__main__":
    import pytest
    pytest.skip("Manual database smoke script; pytest regressions live in test_general_rebuild.py",
                allow_module_level=True)

import os, tempfile, uuid
os.environ.setdefault("TGMON_DB", os.path.join(
    tempfile.gettempdir(), f"tgmon_verify_{uuid.uuid4().hex}.db"))

from tgmon.db import session_scope, engine
from tgmon.models import Base, Channel, MonitorMessage, MessageMedia
from tgmon.message_query import query


def _mk_channel(s, cid, title):
    s.add(Channel(id=cid, tg_id=f"-100{cid}", title=title, enabled=True))


def _mk_msg(s, mid, cid, text, text_hash, published, media_n=0, dup_of=None):
    m = MonitorMessage(id=mid, channel_id=cid, tg_message_id=str(mid),
                       text_raw=text, text_zh=text, text_hash=text_hash,
                       published_at=published, translate_status="ok",
                       duplicate_of=dup_of)
    s.add(m)
    for i in range(media_n):
        s.add(MessageMedia(message_id=mid, kind="photo",
                           thumb_path=f"{cid}/{mid}_{i}.webp"))
    return m


Base.metadata.create_all(engine)
base = datetime(2026, 9, 7, 12, 0, 0)
with session_scope() as s:
    _mk_channel(s, 1, "Seele Leaks")
    _mk_channel(s, 2, "Galaxy_Leak")
    # A: 同频道连发同标签短文本（8字符 < 20），图片不同
    _mk_msg(s, 101, 1, "「HI3rd 9.1」", "hashA", base, media_n=1)
    _mk_msg(s, 102, 1, "「HI3rd 9.1」", "hashA", base + timedelta(minutes=4), media_n=4)
    _mk_msg(s, 103, 1, "「HI3rd 9.1」", "hashA", base + timedelta(minutes=5), media_n=2)
    # B: 跨频道同长文本转发
    long_text = "This is a long enough leak text for dedup window test."
    _mk_msg(s, 201, 1, long_text, "hashB", base, media_n=1)
    _mk_msg(s, 202, 2, long_text, "hashB", base + timedelta(hours=1), media_n=1)
    # C: 同频道同长文本重发
    _mk_msg(s, 203, 1, long_text, "hashC", base)
    _mk_msg(s, 204, 1, long_text, "hashC", base + timedelta(hours=2))
    # D: 跨频道判重 —— #315/#316 场景：首发 262 在频道2，转发 dup_of=262 在频道1
    _mk_msg(s, 262, 2, "「ZZZ 3.2」Full Yuri cutscene video leak",
            "hashD", base - timedelta(days=1), media_n=1)
    _mk_msg(s, 315, 1, "「ZZZ 3.2」Full Yuri cutscene video leak",
            "hashD1", base + timedelta(hours=6), media_n=1, dup_of=262)
    _mk_msg(s, 316, 1, "「ZZZ 3.2」Full Yuri cutscene video leak",
            "hashD2", base + timedelta(hours=6, minutes=20), media_n=1, dup_of=262)
    # E: 同频道判重 —— dup 指向本频道
    _mk_msg(s, 210, 1, "「GI 7.1」", "hashE", base - timedelta(days=2), media_n=1)
    _mk_msg(s, 211, 1, "「GI 7.1」", "hashE1", base - timedelta(days=1), media_n=1, dup_of=210)
    # F: 跨频道同文长文本（无显式 dup 标记，靠 distinct_stories 遮蔽）
    ft = "Another long enough leak text across channels here."
    _mk_msg(s, 221, 2, ft, "hashF", base - timedelta(days=3))
    _mk_msg(s, 222, 1, ft, "hashF", base - timedelta(days=2))


def _vis(**kw):
    with session_scope() as s:
        return sorted(m.id for m in query(s, **kw).all())


fails = []

def check(name, got, want):
    ok = got == sorted(want)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: 可见 {got} / 应 {sorted(want)}")
    if not ok:
        fails.append(name)


# --- 全局视图（不按频道）---
check("A 全局·同频道短标签连发", _vis(duplicates="hide"), [101, 102, 103, 201, 203, 210, 221, 262])
check("B/C 全局·跨频道/同频道长文本只显首发（202/204/211/222 藏）",
      _vis(duplicates="hide"), [101, 102, 103, 201, 203, 210, 221, 262])

# --- 频道视图（channel_ids=[1] = Seele Leaks）---
v1 = _vis(duplicates="hide", channel_ids=[1])
check("D 频道1·跨频道判重可见（#315/#316 恢复，#211 同频道判重藏）", v1,
      [101, 102, 103, 201, 203, 210, 222, 315, 316])
check("E 频道1·同频道判重仍隐藏（#211 藏）", v1,
      [101, 102, 103, 201, 203, 210, 222, 315, 316])
check("F 频道1·跨频道同文首发不遮蔽（#222 可见）", v1,
      [101, 102, 103, 201, 203, 210, 222, 315, 316])

# --- 频道视图（channel_ids=[2] = Galaxy_Leak）---
v2 = _vis(duplicates="hide", channel_ids=[2])
check("频道2·首发全可见", v2, [202, 221, 262])

# --- 「只看重复」不受影响 ---
check("only·全部重复", _vis(duplicates="only"), [211, 315, 316])

print()
print("全部通过" if not fails else f"{len(fails)} 个失败: {fails}")
raise SystemExit(1 if fails else 0)
