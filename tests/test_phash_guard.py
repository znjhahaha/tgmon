# -*- coding: utf-8 -*-
"""图片判重「无区分度指纹」修复验证：

1. informative()：全零/近零/None 无效，正常哈希有效
2. hashes_for()：纯色图 → (None, None)；正常图 → 正常哈希
3. _find_image_dup()：黑首帧（全零指纹）不判重；正常同图仍判重；
   一边无效时只用另一边
"""
if __name__ != "__main__":
    import pytest
    pytest.skip("Manual image smoke script; pytest regressions live in test_general_rebuild.py",
                allow_module_level=True)

import os, tempfile, uuid
os.environ.setdefault("TGMON_DB", os.path.join(
    tempfile.gettempdir(), f"tgmon_phash_{uuid.uuid4().hex}.db"))

from datetime import datetime, timedelta

from tgmon.db import session_scope, engine
from tgmon.models import Base, Channel, MonitorMessage, MessageMedia
from tgmon import dedup
from tgmon.dedup import informative

fails = []

# --- 1. informative ---
cases = [
    (None, False), ("", False), ("0000000000000000", False),
    ("0000010000000000", False),           # 1 个 1（#262 的 dhash）
    ("ffffffffffffffff", False),           # 全 1
    ("4b4bb4b4b44a4b4b", True),            # 正常（#262 的 phash）
    ("aaaaaaaaaaaaaaaa", True),            # 32 个 1 正常
]
for h, want in cases:
    got = informative(h)
    ok = got == want
    if not ok:
        fails.append(f"informative({h!r})={got} 应 {want}")
    print(f"[{'PASS' if ok else 'FAIL'}] informative({h!r}) = {got}")

# --- 2. hashes_for：纯色图 vs 正常图 ---
# 纯色图：dhash 是确定性全零（相邻像素梯度全同向），必须被过滤为 None。
# phash 可能因编码噪声产生随机指纹 —— 噪声指纹 popcount 正常、两张不同
# 纯色图的噪声距离 ~32（远超阈值），不会误判，无需过滤。真正致命的是
# 确定性全零（线上黑首帧 webp 的实测值），由 informative() 挡住。
import numpy as np
from PIL import Image
tmp = tempfile.mkdtemp()
black = os.path.join(tmp, "black.png")
Image.new("RGB", (320, 180), (0, 0, 0)).save(black)
normal = os.path.join(tmp, "normal.png")
arr = (np.random.default_rng(42).random((320, 180, 3)) * 255).astype("uint8")
Image.fromarray(arr).save(normal)

from tgmon.imghash import hashes_for
bh, bd = hashes_for(black)
nh, nd = hashes_for(normal)
ok = bd is None
if not ok:
    fails.append(f"纯色图 dhash 应 None，得 {bd}")
print(f"[{'PASS' if ok else 'FAIL'}] 纯色图 dhash -> {bd}（确定性全零已过滤），phash={bh}")
ok = informative(nh) and informative(nd)
if not ok:
    fails.append(f"正常图哈希应有效，得 ({nh}, {nd})")
print(f"[{'PASS' if ok else 'FAIL'}] 随机图 hashes_for -> ({nh}, {nd})")

# --- 3. _find_image_dup 行为（DB 场景） ---
Base.metadata.create_all(engine)
now = datetime.utcnow()
with session_scope() as s:
    s.query(MessageMedia).delete()
    s.query(MonitorMessage).delete()
    s.query(Channel).delete()
    s.add(Channel(id=1, tg_id="-1001", title="A", enabled=True))
    # 候选1：黑首帧视频（全零指纹，模拟 #315）
    m1 = MonitorMessage(channel_id=1, tg_message_id="1", text_raw="过场动画",
                        text_zh="过场动画", published_at=now - timedelta(hours=2))
    s.add(m1); s.flush()
    s.add(MessageMedia(message_id=m1.id, kind="video", phash="0000000000000000",
                       dhash="0000000000000000", thumb_path="1/1.webp"))
    # 候选2：正常图片消息（有效指纹）
    m2 = MonitorMessage(channel_id=1, tg_message_id="2", text_raw="正常图",
                        text_zh="正常图", published_at=now - timedelta(hours=1))
    s.add(m2); s.flush()
    s.add(MessageMedia(message_id=m2.id, kind="photo", phash=nh, dhash=nd,
                       thumb_path="1/2.webp"))

from tgmon.pipeline import _find_image_dup
from tgmon import settings as st
st.set_many({"DEDUP_ENABLED": True, "DEDUP_WINDOW_DAYS": 7, "PHASH_DISTANCE": 5})

# 场景A：新黑首帧视频（全零） vs 候选 —— 不应判重（修复核心）
r = _find_image_dup([("0000000000000000", "0000000000000000")])
ok = r is None
if not ok:
    fails.append(f"黑首帧互撞仍判重: {r}")
print(f"[{'PASS' if ok else 'FAIL'}] 黑首帧（全零）不判重 -> {r}")

# 场景B：与库里正常图相同的指纹 —— 仍应判重（同图召回保留）
r = _find_image_dup([(nh, nd)])
ok = r is not None and r[1] == "phash" and r[0] == m2.id
if not ok:
    fails.append(f"正常同图判重失效: {r}")
print(f"[{'PASS' if ok else 'FAIL'}] 正常同图仍判重 -> {r}")

# 场景C：phash 无效但 dhash 有效且相同 —— 单边有效仍判重
r = _find_image_dup([(None, nd)])
ok = r is not None
if not ok:
    fails.append(f"单边有效判重失效: {r}")
print(f"[{'PASS' if ok else 'FAIL'}] 单边（dhash）有效仍判重 -> {r}")

print()
print("全部通过" if not fails else f"{len(fails)} 个失败: {fails}")
raise SystemExit(1 if fails else 0)
