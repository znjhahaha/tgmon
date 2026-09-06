#!/usr/bin/env python
"""一次性修复：相册拆分产生的历史重复记录（2026-09-04 反馈）。

背景：相册合并（pipeline 第 1.5 层）上线前，同一相册被实时防抖切分 /
补拉 limit 边界切分成多个批次各自入库，消息页出现「3 张图 3 条记录」
（1图/2图/3图 递增）。本脚本把存量数据收敛回一条：

  按 (channel_id, grouped_id) 聚合 → 选 anchor（有文本优先、媒体多优先、
  id 小优先）→ 兄弟记录的媒体按 thumb_path 去重移交给 anchor → 文本
  缺失则补 → 兄弟记录标记 duplicate_of=anchor, dup_reason="album"
  （不物理删：QqDelivery / ShareToken 等外键引用不受影响，去重复查页
  可见、可翻案）。

用法（VPS /opt/tgmon 下）：
  docker compose exec -T worker python scripts/repair_albums.py --dry-run
  docker compose exec -T worker python scripts/repair_albums.py --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func                              # noqa: E402

from tgmon.db import SessionLocal                        # noqa: E402
from tgmon.models import (                               # noqa: E402
    MessageMedia, MonitorMessage,
)


def _pick_anchor(rows: list[MonitorMessage]) -> MonitorMessage:
    """anchor 选择：有文本 > 媒体多 > 已有译文 > id 小（首发语义）。"""
    def score(m: MonitorMessage) -> tuple:
        return (
            1 if (m.text_raw or "").strip() else 0,
            len(m.media),
            1 if m.translate_status == "ok" else 0,
            -m.id,
        )
    return sorted(rows, key=score, reverse=True)[0]


def repair(commit: bool) -> dict:
    """单事务跑完全部修复；commit=False 时回滚（dry-run 统计与 apply 一致）。"""
    stats = {"groups": 0, "merged_media": 0, "dropped_dup_media": 0,
             "filled_text": 0, "marked_dup": 0, "already_ok": 0}
    s = SessionLocal()
    try:
        # 同组多条记录的 (channel_id, grouped_id)
        dup_groups = (s.query(MonitorMessage.channel_id,
                              MonitorMessage.grouped_id)
                      .filter(MonitorMessage.grouped_id.isnot(None))
                      .group_by(MonitorMessage.channel_id,
                                MonitorMessage.grouped_id)
                      .having(func.count(MonitorMessage.id) > 1)
                      .all())
        for cid, gid in dup_groups:
            rows = (s.query(MonitorMessage)
                    .filter(MonitorMessage.channel_id == cid,
                            MonitorMessage.grouped_id == gid)
                    .all())
            # 只处理「组内有多条非重复记录」的组 —— 已修复过的
            # （兄弟已标 duplicate_of=anchor）跳过，保证幂等
            live = [r for r in rows if r.duplicate_of is None]
            if len(live) < 2:
                stats["already_ok"] += 1
                continue
            stats["groups"] += 1
            anchor = _pick_anchor(live)
            others = [r for r in live if r.id != anchor.id]
            existing = {x.thumb_path for x in anchor.media if x.thumb_path}
            for r in others:
                for x in list(r.media):
                    if x.thumb_path and x.thumb_path in existing:
                        # anchor 已有同图（拆分批次的累积重复）：丢弃行
                        stats["dropped_dup_media"] += 1
                        s.delete(x)
                        continue
                    x.message_id = anchor.id        # 移交
                    existing.add(x.thumb_path)
                    stats["merged_media"] += 1
                # 文本：anchor 缺、兄弟有 → 补（译文不搬，交给重译按钮）
                if (not (anchor.text_raw or "").strip()
                        and (r.text_raw or "").strip()):
                    anchor.text_raw = r.text_raw
                    if anchor.translate_status == "skipped":
                        anchor.translate_status = "pending"
                    stats["filled_text"] += 1
                if r.media:
                    anchor.has_media = True
                r.duplicate_of = anchor.id
                r.dup_reason = "album"
                stats["marked_dup"] += 1
        if commit:
            s.commit()
        else:
            s.rollback()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description="相册拆分历史修复")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="只统计不落库")
    g.add_argument("--apply", action="store_true", help="执行修复")
    args = ap.parse_args()

    stats = repair(commit=args.apply)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] 修复结果：")
    print(f"  待修复相册组数   : {stats['groups']}")
    print(f"  移交媒体行数     : {stats['merged_media']}")
    print(f"  丢弃重复媒体行数 : {stats['dropped_dup_media']}")
    print(f"  补文本条数       : {stats['filled_text']}")
    print(f"  标记重复记录数   : {stats['marked_dup']}")
    print(f"  已修复跳过组数   : {stats['already_ok']}")
    if not args.apply:
        print("（dry-run 未落库；确认数字后跑 --apply）")


if __name__ == "__main__":
    main()
