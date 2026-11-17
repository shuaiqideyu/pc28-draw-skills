"""开奖检测进程。只跑采集，不跑遗漏、统计、长龙。"""

from __future__ import annotations

import asyncio
import signal
from datetime import datetime, timezone

from collector.bus import DrawBus
from collector.env import load_collector_env
from collector.keno_loop import KenoCollector
from collector.store import DrawLog


async def _heartbeat(collector: KenoCollector, stopped: asyncio.Event) -> None:
    while not stopped.is_set():
        try:
            await asyncio.wait_for(stopped.wait(), 60.0)
            return
        except asyncio.TimeoutError:
            next_draw = (
                datetime.fromtimestamp(
                    collector.next_draw_at_ms / 1000, tz=timezone.utc
                ).isoformat()
                if collector.next_draw_at_ms
                else "—"
            )
            print(f"[heartbeat] last_nbr={collector.last_nbr} next_draw={next_draw}")


async def _watch_stop(collector: KenoCollector, stopped: asyncio.Event) -> None:
    await stopped.wait()
    collector.stopped = True


async def _async_main() -> None:
    env = load_collector_env()
    bus = DrawBus()
    draws = DrawLog(bus)
    collector = KenoCollector(env, draws)
    await collector.init()

    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _shutdown(sig: signal.Signals) -> None:
        print(f"[collector] 收到 {sig.name}，准备退出")
        stopped.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown, sig)

    print("[collector] 开始多源轮询")
    try:
        results = await asyncio.gather(
            collector.start(),
            _heartbeat(collector, stopped),
            _watch_stop(collector, stopped),
            return_exceptions=True,
        )
        for item in results:
            if isinstance(item, BaseException) and not isinstance(item, asyncio.CancelledError):
                print(f"[collector] 任务异常: {item!r}")
    finally:
        stopped.set()
        collector.stopped = True
        await collector.stop()
        await draws.close()


def main() -> None:
    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        pass
