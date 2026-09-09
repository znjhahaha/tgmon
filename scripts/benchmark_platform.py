"""Repeatable local workload; all model and delivery effects stay mocked."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
import tracemalloc


async def run():
    from tgmon.bootstrap import init_all
    from tgmon import settings
    from tgmon.db import session_scope
    from tgmon.models import Channel, MonitorMessage, QqGroup
    from tgmon.qqbot import commands
    init_all()
    settings.set_many({"EMBEDDING_ENABLED": False, "QQ_AI_ENABLED": False,
        "QQ_ENABLED": False, "WEBHOOK_ENABLED": False, "RETRIEVAL_ENABLED": False,
        "BASE_URL": "http://127.0.0.1:8766"})
    with session_scope() as s:
        channel = Channel(tg_id="benchmark", title="Benchmark", game="原神", enabled=True)
        s.add(channel)
        s.flush()
        now = datetime.utcnow()
        s.add_all([MonitorMessage(channel_id=channel.id, tg_message_id=str(index),
            text_raw=f"Sample report {index}: value {index + 1000}", text_zh=f"消息 {index} 数值 {index + 1000}",
            translate_status="ok", game_detected="原神", published_at=now - timedelta(seconds=index))
            for index in range(500)])
        s.add(QqGroup(group_openid="benchmark-group", enabled=True))
    commands._schedule_bundle_cards = lambda *args, **kwargs: None
    samples = []
    tracemalloc.start()
    for index in range(60):
        started = time.perf_counter()
        replies, _ = await commands.handle_group_message({"id": f"bench-{index}",
            "group_openid": "benchmark-group", "author": {"member_openid": "benchmark-person"},
            "content": "/latest 3" if index % 2 else "/status"})
        assert replies
        samples.append((time.perf_counter() - started) * 1000)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {"samples": len(samples), "messages": 500,
            "command_median_ms": round(statistics.median(samples), 2),
            "command_p95_ms": round(sorted(samples)[int(len(samples) * .95) - 1], 2),
            "peak_python_mb": round(peak / 1048576, 2)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--output")
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.repo).resolve()))
    with tempfile.TemporaryDirectory(prefix="tgmon-benchmark-") as temporary:
        for key, value in {"DB": "benchmark.sqlite3", "MEDIA": "media", "SESSIONS": "sessions",
                           "LOGS": "logs", "SECRET_FILE": "secret.key", "EXTENSIONS": "extensions"}.items():
            os.environ[f"TGMON_{key}"] = str(Path(temporary) / value)
        result = asyncio.run(run())
        from tgmon.db import engine
        engine.dispose()
    content = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(content, encoding="utf-8")
    print(content)


if __name__ == "__main__":
    main()
