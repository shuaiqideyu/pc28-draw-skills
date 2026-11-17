"""多源 Keno 采集主循环。

BCLC 优先 IPv6，PlayNow 固定 IPv4。两侧各自轮询，慢的一侧不挡住另一侧。
极热时 fallback / 按期号探测另起循环，不拖主接口的下一枪。
pc28.help 单独循环同时问，不跟在官方超时后面。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Coroutine
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from collector.store import DrawInsert, DrawLog
from collector.env import CollectorEnv
from collector.http_client import OfficialH2Client, ThrottledLogger
from collector.parse_bclc import CompletedKeno, parse_fallback, parse_help_keno, parse_primary
from collector.pc28 import is_valid_keno_nums, transform_keno_to_pc28
from collector.timeutil import DRAW_INTERVAL_MS, LotteryClock, beijing_wall_to_utc, fallback_naive

POLL_IDLE_MS = 1_000
POLL_HOT_MS = 80
POLL_BURST_MS = 50
HOT_BEFORE_MS = 8_000
HOT_AFTER_MS = 25_000
BURST_BEFORE_MS = 8_000
BURST_AFTER_MS = 12_000
HOT_TIMEOUT_MS = 4_000
IDLE_TIMEOUT_MS = 8_000
# 和官方一起看。1 秒一轮，不跟 50ms 热区抢。
HELP_POLL_MS = 1_000
HELP_TIMEOUT_S = 4.0

def gap_missing_nbr(last_nbr: int, incoming: int) -> int | None:
    """官方卡奖最多跳一期：水位 N 来了 N+2，中间 N+1 用指定接口一定能查到。"""
    if last_nbr <= 0 or incoming != last_nbr + 2:
        return None
    return last_nbr + 1


def parse_official_countdown_sec(raw: str) -> int | None:
    m = re.match(r"^(\d{1,2}):(\d{2}):(\d{2})$", raw.strip())
    if not m:
        return None
    h, mi, sec = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return h * 3600 + mi * 60 + sec



def _origin(url: str) -> str:
    u = urlsplit(url)
    return f"{u.scheme}://{u.netloc}".lower()

class KenoCollector:
    def __init__(self, env: CollectorEnv, repos: DrawLog) -> None:
        self.env = env
        self.repos = repos
        self.last_nbr = 0
        self._bg_tasks: set[asyncio.Task[None]] = set()
        self.next_draw_at_ms = 0
        # 上一期墙钟 + 210s。主接口倒计时不能把它往后推，否则 fallback 进不了热区。
        self._theory_next_ms = 0
        self.clock = LotteryClock()
        self.stopped = False
        self._accept_lock = asyncio.Lock()
        self._hot_warmed_nbr = 0  # 已为哪一期做过进热区预热
        # PlayNow 无 AAAA；BCLC 有 IPv6 且更快，没有路由时客户端自己退回 IPv4
        self._playnow_origin = _origin(str(env.PLAYNOW_PRIMARY_URL))
        self.official_playnow = OfficialH2Client(
            name="playnow",
            site_origin=self._playnow_origin,
            prefer_ipv6=False,
        )
        self.official_bclc = OfficialH2Client(
            name="bclc",
            site_origin=_origin(str(env.BCLC_PRIMARY_URL)),
            prefer_ipv6=True,
        )
        self.logs = {
            "bclc_primary": ThrottledLogger("bclc.primary"),
            "bclc_fallback": ThrottledLogger("bclc.fallback"),
            "playnow_primary": ThrottledLogger("playnow.primary"),
            "playnow_fallback": ThrottledLogger("playnow.fallback"),
            "probe": ThrottledLogger("probe"),
            "help": ThrottledLogger("help"),
        }
        self._help = httpx.AsyncClient(
            timeout=httpx.Timeout(HELP_TIMEOUT_S),
            follow_redirects=True,
            trust_env=False,
            headers={"Accept": "application/json"},
        )

    async def init(self) -> None:
        keno_nbr = await self.repos.latest_filled_keno_nbr() or 0
        draw_nbr = await self.repos.latest_draw_nbr() or 0
        self.last_nbr = max(keno_nbr, draw_nbr)
        drawn_at = await self.repos.latest_drawn_at()
        if drawn_at:
            ms = drawn_at.timestamp() * 1000
            if ms == ms:  # finite
                self._theory_next_ms = ms + DRAW_INTERVAL_MS
                self.next_draw_at_ms = self._theory_next_ms
        next_hint = (
            f" next_draw={datetime.fromtimestamp(self.next_draw_at_ms / 1000, tz=timezone.utc).isoformat()}"
            if self.next_draw_at_ms
            else ""
        )
        print(f"[collector] 水位 draw_nbr={self.last_nbr}{next_hint}")
        await self._seed_clock(drawn_at)
        await self._warmup_links(reason="startup")

    async def stop(self) -> None:
        self.stopped = True
        await self.official_playnow.close()
        await self.official_bclc.close()
        await self._help.aclose()

    def _client_for(self, url: str) -> OfficialH2Client:
        return (
            self.official_playnow
            if _origin(url) == self._playnow_origin
            else self.official_bclc
        )

    async def _warmup_links(self, *, reason: str) -> None:
        """持有连接：对主接口各打一枪，完成 TLS/H2 握手后复用。"""
        print(f"[collector] warmup ({reason})")
        probe = self.env.BCLC_DRAW_URL_TEMPLATE.replace(
            "{draw_nbr}", str(max(self.last_nbr, 1))
        )
        await asyncio.gather(
            self.official_playnow.warmup(str(self.env.PLAYNOW_PRIMARY_URL)),
            self._client_for(str(self.env.BCLC_PRIMARY_URL)).warmup(
                str(self.env.BCLC_PRIMARY_URL)
            ),
            self._client_for(probe).warmup(probe),
        )
        if self.last_nbr > 0:
            self._hot_warmed_nbr = self.last_nbr + 1

    async def _maybe_prewarm_hot(self) -> None:
        """进入热区前再预热一次（按期号只做一次，避免 countdown 漂移反复打断连接）。"""
        if self.next_draw_at_ms <= 0 or self.last_nbr <= 0:
            return
        target = self.last_nbr + 1
        if self._hot_warmed_nbr == target:
            return
        delta = _now_ms() - self.next_draw_at_ms
        if -HOT_BEFORE_MS <= delta < -1_000:
            await self._warmup_links(reason=f"pre-hot #{target}")
            self._hot_warmed_nbr = target

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _fill_keno_bg(self, c: CompletedKeno) -> None:
        try:
            await self.repos.fill_keno(c)
        except Exception as err:
            print(f"[keno.fill] bg fail {c.draw_nbr}: {err}")

    async def _upsert_upcoming_bg(self, draw_nbr: int, countdown: str) -> None:
        try:
            await self.repos.upsert_upcoming(draw_nbr, countdown)
        except Exception as err:
            print(f"[keno.upcoming] bg fail {draw_nbr}: {err}")

    async def start(self) -> None:
        await asyncio.gather(
            self._loop("bclc", self._tick_primary_bclc),
            self._loop("playnow", self._tick_primary_playnow),
            self._loop("bclc-fallback", self._tick_fallback_bclc_burst),
            self._loop("playnow-fallback", self._tick_fallback_playnow_burst),
            self._loop("probe", self._tick_probe_burst),
            self._loop("help", self._tick_help, HELP_POLL_MS),
            self._prewarm_loop(),
        )

    async def _prewarm_loop(self) -> None:
        while not self.stopped:
            try:
                await self._maybe_prewarm_hot()
            except Exception as err:
                print(f"[collector] prewarm error {err}")
            await asyncio.sleep(0.5)

    def _is_hot(self, now: float | None = None) -> bool:
        now_ms = now if now is not None else _now_ms()
        if self.next_draw_at_ms <= 0:
            return True
        delta = now_ms - self.next_draw_at_ms
        return -HOT_BEFORE_MS <= delta <= HOT_AFTER_MS

    def _is_burst(self, now: float | None = None) -> bool:
        now_ms = now if now is not None else _now_ms()
        if self.next_draw_at_ms <= 0:
            return True
        delta = now_ms - self.next_draw_at_ms
        return -BURST_BEFORE_MS <= delta <= BURST_AFTER_MS

    def _is_overdue(self, now: float | None = None) -> bool:
        """理论开奖点已过，主接口仍把倒计时拨到未来。"""
        if self._theory_next_ms <= 0:
            return False
        now_ms = now if now is not None else _now_ms()
        return now_ms >= self._theory_next_ms

    def _should_catch_result(self, now: float | None = None) -> bool:
        return self._is_burst(now) or self._is_overdue(now)

    def _poll_interval_ms(self) -> int:
        if self._is_burst():
            return POLL_BURST_MS
        if self._is_hot():
            return POLL_HOT_MS
        return POLL_IDLE_MS

    def _request_timeout_ms(self) -> int:
        return HOT_TIMEOUT_MS if self._is_hot() else IDLE_TIMEOUT_MS

    def _sync_official_countdown(self, countdown: str) -> None:
        sec = parse_official_countdown_sec(countdown)
        if sec is None:
            return
        proposed = _now_ms() + sec * 1000
        # 理论点已过还把下一期推到未来，fallback 会一直停在热区外面。
        if self._is_overdue() and proposed > self._theory_next_ms:
            return
        self.next_draw_at_ms = proposed

    async def _loop(
        self,
        name: str,
        tick,
        fixed_interval_ms: int | None = None,
    ) -> None:
        while not self.stopped:
            started = _now_ms()
            found = False
            try:
                found = await tick()
            except Exception as err:
                print(f"[{name}] tick error {err}")
            if found:
                await asyncio.sleep(0.05)
                continue
            target = (
                fixed_interval_ms
                if fixed_interval_ms is not None
                else self._poll_interval_ms()
            )
            elapsed = _now_ms() - started
            await asyncio.sleep(max(0, (target - elapsed) / 1000))

    async def _tick_primary_bclc(self) -> bool:
        return await self._tick_primary(
            str(self.env.BCLC_PRIMARY_URL), self.logs["bclc_primary"], "bclc"
        )

    async def _tick_primary_playnow(self) -> bool:
        return await self._tick_primary(
            str(self.env.PLAYNOW_PRIMARY_URL), self.logs["playnow_primary"], "playnow"
        )

    async def _tick_fallback_bclc_burst(self) -> bool:
        if not self._should_catch_result():
            return False
        return await self._tick_fallback(
            str(self.env.BCLC_FALLBACK_URL), self.logs["bclc_fallback"], "bclc-fallback"
        )

    async def _tick_fallback_playnow_burst(self) -> bool:
        if not self._should_catch_result():
            return False
        return await self._tick_fallback(
            str(self.env.PLAYNOW_FALLBACK_URL),
            self.logs["playnow_fallback"],
            "playnow-fallback",
        )

    async def _tick_probe_burst(self) -> bool:
        if not self._should_catch_result():
            return False
        return await self._tick_probe()

    async def _tick_help(self) -> bool:
        url = str(self.env.HELP_KENO_URL)
        log = self.logs["help"]
        try:
            resp = await self._help.get(url)
        except Exception as err:
            log.fail("fetch", err)
            return False
        if resp.status_code != 200:
            log.fail(f"HTTP {resp.status_code}", Exception(str(resp.status_code)))
            return False
        try:
            data = resp.json()
        except Exception as err:
            log.fail("json", err)
            return False
        log.ok()
        found = False
        for c in parse_help_keno(data):
            if await self._accept_keno(c, "keno", "help"):
                found = True
        return found

    async def _tick_primary(self, url: str, log: ThrottledLogger, via: str) -> bool:
        try:
            status, data = await self._client_for(url).get(
                url, timeout_ms=self._request_timeout_ms()
            )
            if status != 200:
                log.fail(f"HTTP {status}", Exception(str(status)))
                return False
            log.ok()
            completed, upcoming = parse_primary(data, self.clock)
            if upcoming and upcoming.countdown:
                self._sync_official_countdown(upcoming.countdown)
            if completed:
                ok = await self._accept_keno(completed, "keno", via)
                if upcoming and upcoming.countdown:
                    self._spawn(
                        self._upsert_upcoming_bg(upcoming.draw_nbr, upcoming.countdown)
                    )
                return ok
            if upcoming and upcoming.countdown:
                if self._is_burst():
                    self._spawn(
                        self._upsert_upcoming_bg(upcoming.draw_nbr, upcoming.countdown)
                    )
                else:
                    await self.repos.upsert_upcoming(
                        upcoming.draw_nbr, upcoming.countdown
                    )
            return False
        except Exception as err:
            log.fail("fetch", err)
            return False

    async def _tick_fallback(self, url: str, log: ThrottledLogger, via: str) -> bool:
        try:
            status, data = await self._client_for(url).get(
                url, timeout_ms=self._request_timeout_ms()
            )
            if status != 200:
                log.fail(f"HTTP {status}", Exception(str(status)))
                return False
            log.ok()
            rows = parse_fallback(data, self.clock)
            found = False
            for c in sorted(rows, key=lambda r: r.draw_nbr):
                if await self._accept_keno(c, "keno", via):
                    found = True
            return found
        except Exception as err:
            log.fail("fetch", err)
            return False

    async def _tick_probe(self) -> bool:
        if self.last_nbr <= 0:
            return False
        url = self.env.BCLC_DRAW_URL_TEMPLATE.replace(
            "{draw_nbr}", str(self.last_nbr + 1)
        )
        try:
            status, data = await self._client_for(url).get(
                url, timeout_ms=self._request_timeout_ms()
            )
            if status == 400:
                return False
            if status != 200:
                self.logs["probe"].fail(f"HTTP {status}", Exception(str(status)))
                return False
            self.logs["probe"].ok()
            rows = parse_fallback(data, self.clock)
            found = False
            for c in sorted(rows, key=lambda r: r.draw_nbr):
                if await self._accept_keno(c, "keno", "probe"):
                    found = True
            return found
        except Exception as err:
            self.logs["probe"].fail("fetch", err)
            return False

    async def _fetch_completed(self, nbr: int) -> CompletedKeno | None:
        url = self.env.BCLC_DRAW_URL_TEMPLATE.replace("{draw_nbr}", str(nbr))
        status, data = await self._client_for(url).get(
            url, timeout_ms=IDLE_TIMEOUT_MS
        )
        if status != 200:
            print(f"[gap] {nbr} HTTP {status}")
            return None
        for c in parse_fallback(data, self.clock):
            if c.draw_nbr == nbr and is_valid_keno_nums(c.draw_nbrs):
                return c
        return None

    async def _seed_clock(self, drawn_at: datetime | None) -> None:
        """用库里最新一期的官方墙钟，对齐当前 UTC−7 / UTC−8。"""
        self.clock.prev_nbr = self.last_nbr
        if drawn_at is None or self.last_nbr <= 0:
            return
        url = self.env.BCLC_DRAW_URL_TEMPLATE.replace("{draw_nbr}", str(self.last_nbr))
        try:
            status, data = await self._client_for(url).get(url, timeout_ms=IDLE_TIMEOUT_MS)
        except Exception as err:
            print(f"[clock] 水位墙钟没读到 {err}")
            return
        if status != 200 or not isinstance(data, list) or not data:
            return
        item = data[0] if isinstance(data[0], dict) else None
        if item is None:
            return
        date_str = item.get("drawDate")
        time_str = item.get("drawTime")
        if not isinstance(date_str, str) or not isinstance(time_str, str):
            return
        try:
            naive = fallback_naive(date_str, time_str)
        except ValueError:
            return
        instant = drawn_at if drawn_at.tzinfo else drawn_at.replace(tzinfo=timezone.utc)
        self.clock.seed(self.last_nbr, naive, instant)
        hours = int(self.clock.offset.total_seconds() // 3600)
        print(f"[clock] 水位 {self.last_nbr} 墙钟 {naive:%m-%d %H:%M:%S} 偏移 {hours}")

    async def _insert_completed(self, c: CompletedKeno, source: str, via: str) -> bool:
        if not is_valid_keno_nums(c.draw_nbrs):
            return False
        num1, num2, num3, draw_sum = transform_keno_to_pc28(c.draw_nbrs)
        inserted = await self.repos.insert_draw(
            DrawInsert(
                draw_nbr=c.draw_nbr,
                draw_date=c.draw_date,
                draw_time=c.draw_time,
                num1=num1,
                num2=num2,
                num3=num3,
                draw_sum=draw_sum,
                source=source,
            )
        )
        if inserted:
            drawn_at = beijing_wall_to_utc(c.draw_date, c.draw_time)
            lag_s = _now_ms() / 1000 - drawn_at.timestamp()
            print(
                f"[draw] {c.draw_nbr} {c.draw_date} {c.draw_time} "
                f"{num1}+{num2}+{num3}={draw_sum} +{lag_s:.1f}s {via}"
            )
            self._note_accepted(c)
            self._spawn(self._fill_keno_bg(c))
        else:
            await self.repos.fill_keno(c)
        return inserted

    async def _fill_gaps_before(self, incoming: int) -> None:
        nbr = gap_missing_nbr(self.last_nbr, incoming)
        if nbr is None:
            return
        missing = await self.repos.missing_draw_nbrs(nbr, nbr)
        if not missing:
            return
        row = await self._fetch_completed(nbr)
        if row is None:
            return
        await self._insert_completed(row, "keno-gap", "gap")

    async def _accept_keno(self, c: CompletedKeno, source: str, via: str) -> bool:
        async with self._accept_lock:
            if c.draw_nbr <= self.last_nbr:
                return False
            await self._fill_gaps_before(c.draw_nbr)
            return await self._insert_completed(c, source, via)

    def _note_accepted(self, row) -> None:
        if row.draw_nbr > self.last_nbr:
            self.last_nbr = row.draw_nbr
        drawn_at = beijing_wall_to_utc(row.draw_date, row.draw_time)
        self._theory_next_ms = drawn_at.timestamp() * 1000 + DRAW_INTERVAL_MS
        self.next_draw_at_ms = self._theory_next_ms


def _now_ms() -> float:
    return datetime.now(tz=timezone.utc).timestamp() * 1000
