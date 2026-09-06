"""worker 进程入口：python -m tgmon.worker"""
from __future__ import annotations

import asyncio
import logging
import signal

from ..bootstrap import init_all
from ..util import setup_logging
from .runner import WorkerRunner

logger = logging.getLogger(__name__)


async def _amain() -> None:
    init_all()
    runner = WorkerRunner()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda s=sig: runner.request_exit(f"收到信号 {s}"))
        except (NotImplementedError, RuntimeError):
            pass

    logger.info("tgmon worker 启动")
    await runner.run()
    logger.info("tgmon worker 退出")


def main() -> None:
    setup_logging("worker")
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
