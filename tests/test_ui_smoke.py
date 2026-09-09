# -*- coding: utf-8 -*-
"""channels._list 数据装配 + channel_rows/overview_stats 模板渲染冒烟。"""
if __name__ != "__main__":
    import pytest
    pytest.skip("Manual UI smoke script; import must not write the database",
                allow_module_level=True)

import os, tempfile, uuid
os.environ.setdefault("TGMON_DB", os.path.join(
    tempfile.gettempdir(), f"tgmon_ui_smoke_{uuid.uuid4().hex}.db"))

from datetime import datetime, timedelta

from tgmon.db import session_scope, engine
from tgmon.models import Base, Channel, MonitorMessage, Task

Base.metadata.create_all(engine)
now = datetime.utcnow()
with session_scope() as s:
    s.add(Channel(id=1, tg_id="-1001", title="Seele Leaks", enabled=True,
                  source_type="telegram", last_tg_id=100,
                  last_message_at=now - timedelta(minutes=10)))
    s.add(Channel(id=2, tg_id="-1002", title="Galaxy", enabled=True,
                  source_type="telegram", last_tg_id=200,
                  last_catchup_at=now - timedelta(minutes=2)))
    s.add(MonitorMessage(channel_id=1, tg_message_id="98", text_raw="a",
                         text_zh="a", published_at=now))
    s.add(Task(kind="catchup_round", status="done", payload={}, result={
        "done": 2, "total": 2, "aligned": 1, "skipped": 1,
        "channels": {"1": {"title": "Seele Leaks", "status": "aligned",
                           "gap": 2, "ingested": 2, "error": None},
                     "2": {"title": "Galaxy", "status": "error", "gap": None,
                           "ingested": None, "error": "ValueError: bad"}}}))

# 1) _list 数据装配
from tgmon.admin.routes import channels as ch_routes
rows = ch_routes._list()
r1 = next(r for r in rows if r["id"] == 1)
r2 = next(r for r in rows if r["id"] == 2)
assert r1["catchup_gap"] == 2, r1
assert r1["last_round"]["status"] == "aligned", r1["last_round"]
assert r1["last_round"]["ingested"] == 2
assert r2["last_round"]["status"] == "error"
print(f"_list OK: ch1 gap={r1['catchup_gap']} last_round={r1['last_round']}")
print(f"          ch2 last_round.status={r2['last_round']['status']} err={r2['last_round']['error']}")

# 2) 模板渲染
from tgmon.admin.deps import templates
html1 = templates.get_template("fragments/channel_rows.html").render(
    channels=rows, catchup_max_gap=50, games=[])
assert "上轮补 2 条" in html1, "channel_rows 应显示上轮补 2 条"
assert "上轮失败" in html1, "channel_rows 应显示上轮失败"
print("channel_rows.html 渲染 OK（含上轮状态）")

from tgmon.admin.routes import overview as ov_routes
stats = ov_routes._stats()
assert "channel_health" in stats and stats["channel_health"], stats.keys()
assert stats["catchup_every"] >= 60
print(f"_stats OK: health={[c['title'] for c in stats['channel_health']]} "
      f"every={stats['catchup_every']}s last_at={stats['last_catchup_at']}")

html2 = templates.get_template("fragments/overview_stats.html").render(
    s=stats,
    worker={"online": True, "status": "online", "tg_user": "u",
            "current_action": "", "detail": "", "queue_depth": 0,
            "heartbeat_at": now})
assert "频道拉取健康度" in html2, "overview_stats 应含健康度卡片"
assert "落后 2" in html2, "overview_stats 应显示落后条数"
print("overview_stats.html 渲染 OK（含健康度卡片）")
print("\n全部通过")
