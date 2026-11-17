"""BC / 北京时间转换（采集侧）。

不列颠哥伦比亚自 2026-11-01 起不再回拨，全年夏令时，固定 UTC−7，
北京时间 +15 小时。正常路径按这一档理解，不要再维护冬令时 +16。
下面的期号步进仍保留：墙钟若相对 3.5 分钟突然再跳大约 1 小时，只改偏移，
绝对时间仍按间隔往前。维护空档大约 36 分钟，不在这扇窗里。
已经按期号定下来的时刻，不因新偏移改写。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DRAW_INTERVAL_MS = 210_000
DRAW_STEP = timedelta(milliseconds=DRAW_INTERVAL_MS)

BEIJING_TZ = ZoneInfo("Asia/Shanghai")
_OFFSETS = (timedelta(hours=-7), timedelta(hours=-8))
# 相对「间隔 × 期号差」再偏 50–70 分钟，才当一小时的时令跳变。
_JUMP_MIN = timedelta(minutes=50)
_JUMP_MAX = timedelta(minutes=70)

_MONTHS = {
    "Jan": 0,
    "Feb": 1,
    "Mar": 2,
    "Apr": 3,
    "May": 4,
    "Jun": 5,
    "Jul": 6,
    "Aug": 7,
    "Sep": 8,
    "Oct": 9,
    "Nov": 10,
    "Dec": 11,
}


def zoned_time_to_utc(local_iso_no_tz: str, time_zone: str) -> datetime:
    if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$", local_iso_no_tz):
        raise ValueError(f"本地时间格式应为 YYYY-MM-DDTHH:MM:SS: {local_iso_no_tz}")
    naive = datetime.fromisoformat(local_iso_no_tz)
    return naive.replace(tzinfo=ZoneInfo(time_zone)).astimezone(UTC)


def _clamp_offset(offset: timedelta) -> timedelta:
    hours = int(round(offset.total_seconds() / 3600))
    if hours >= -7:
        return timedelta(hours=-7)
    return timedelta(hours=-8)


def apply_lottery_offset(naive: datetime, offset: timedelta) -> datetime:
    return naive.replace(tzinfo=timezone(offset)).astimezone(UTC)


def infer_lottery_offset(naive: datetime, instant: datetime) -> timedelta:
    """用已经记下的绝对时刻，反推这一期墙钟当时的偏移。"""
    best = _OFFSETS[0]
    best_err = None
    aware = instant.astimezone(UTC) if instant.tzinfo else instant.replace(tzinfo=UTC)
    for offset in _OFFSETS:
        err = abs((apply_lottery_offset(naive, offset) - aware).total_seconds())
        if best_err is None or err < best_err:
            best, best_err = offset, err
    return best


def shifted_offset(
    prev_naive: datetime,
    naive: datetime,
    offset: timedelta,
    period_gap: int,
) -> timedelta | None:
    """期号连续、墙钟却多跳了大约 1 小时时，返回新偏移。否则 None。"""
    gap = period_gap if period_gap > 0 else 1
    shift = (naive - prev_naive) - DRAW_STEP * gap
    if not (_JUMP_MIN <= abs(shift) <= _JUMP_MAX):
        return None
    step = timedelta(hours=1 if shift > timedelta(0) else -1)
    return _clamp_offset(offset + step)


class LotteryClock:
    """记住当前偏移。只有连续期号的墙钟跳变才改。"""

    def __init__(self) -> None:
        # 占位。没有上一期时，第一次 observe 按离现在更近的 +15/+16 盖掉。
        self.offset = timedelta(hours=-7)
        self.prev_naive: datetime | None = None
        self.prev_nbr = 0

    def seed(self, draw_nbr: int, naive: datetime, instant: datetime) -> None:
        self.prev_nbr = draw_nbr
        self.prev_naive = naive
        self.offset = infer_lottery_offset(naive, instant)

    def observe(
        self, draw_nbr: int, naive: datetime, *, now: datetime | None = None
    ) -> datetime:
        if self.prev_naive is None or not self.prev_nbr:
            instant = lottery_wall_to_utc(naive, now=now)
            self.offset = infer_lottery_offset(naive, instant)
            self.prev_naive = naive
            self.prev_nbr = draw_nbr
            return instant
        if draw_nbr > self.prev_nbr:
            gap = draw_nbr - self.prev_nbr
            nxt = shifted_offset(self.prev_naive, naive, self.offset, gap)
            if nxt is not None and nxt != self.offset:
                print(
                    f"[clock] 期号 {self.prev_nbr}->{draw_nbr} 墙钟 "
                    f"{self.prev_naive:%m-%d %H:%M:%S}->{naive:%m-%d %H:%M:%S} "
                    f"偏移 {int(self.offset.total_seconds() // 3600)}->"
                    f"{int(nxt.total_seconds() // 3600)}"
                )
                self.offset = nxt
        instant = apply_lottery_offset(naive, self.offset)
        if draw_nbr >= self.prev_nbr:
            self.prev_naive = naive
            self.prev_nbr = draw_nbr
        return instant


def lottery_wall_to_utc(naive: datetime, *, now: datetime | None = None) -> datetime:
    """没有上一期时的种子：同一面墙钟按北京 +15 和 +16 各算一次，取离现在更近的一档。

    距离相同时留下已经发生的那一档。有上一期以后只走 LotteryClock，不再进这里。
    """
    current = now if now is not None else datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    else:
        current = current.astimezone(UTC)
    best = apply_lottery_offset(naive, _OFFSETS[0])
    best_key = (abs((best - current).total_seconds()), best > current)
    for offset in _OFFSETS[1:]:
        instant = apply_lottery_offset(naive, offset)
        key = (abs((instant - current).total_seconds()), instant > current)
        if key < best_key:
            best = instant
            best_key = key
    return best


def primary_naive(iso_str: str) -> datetime:
    cleaned = iso_str.split(".")[0] if "." in iso_str else iso_str
    if "T" not in cleaned and " " in cleaned:
        cleaned = cleaned.replace(" ", "T")
    if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$", cleaned):
        raise ValueError(f"primary 时间格式不符: {iso_str}")
    return datetime.fromisoformat(cleaned)


def fallback_naive(date_str: str, time_str: str) -> datetime:
    m = re.match(r"^([A-Za-z]{3})\s+(\d{1,2}),\s+(\d{4})$", date_str)
    t = re.match(r"^(\d{1,2}):(\d{2}):(\d{2})\s*(AM|PM)$", time_str, re.I)
    if not m or not t:
        raise ValueError(f"fallback 时间格式不符: {date_str} {time_str}")
    hour = int(t.group(1))
    minute = int(t.group(2))
    second = int(t.group(3))
    ap = t.group(4).upper()
    if ap == "PM" and hour < 12:
        hour += 12
    if ap == "AM" and hour == 12:
        hour = 0
    y = int(m.group(3))
    mo = _MONTHS[m.group(1)]
    day = int(m.group(2))
    return datetime(y, mo + 1, day, hour, minute, second)


def _instant_to_cn(instant: datetime) -> tuple[str, str]:
    dt = instant.astimezone(BEIJING_TZ)
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M:%S")


def _bc_local_to_cn(
    local_iso_no_tz: str,
    *,
    clock: LotteryClock | None = None,
    draw_nbr: int | None = None,
    now: datetime | None = None,
) -> tuple[str, str]:
    if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$", local_iso_no_tz):
        raise ValueError(f"本地时间格式应为 YYYY-MM-DDTHH:MM:SS: {local_iso_no_tz}")
    naive = datetime.fromisoformat(local_iso_no_tz)
    if clock is not None and draw_nbr is not None:
        instant = clock.observe(draw_nbr, naive, now=now)
    else:
        instant = lottery_wall_to_utc(naive, now=now)
    return _instant_to_cn(instant)


def parse_primary_time_bc(
    iso_str: str,
    *,
    clock: LotteryClock | None = None,
    draw_nbr: int | None = None,
    now: datetime | None = None,
) -> tuple[str, str]:
    naive = primary_naive(iso_str)
    local = naive.strftime("%Y-%m-%dT%H:%M:%S")
    return _bc_local_to_cn(local, clock=clock, draw_nbr=draw_nbr, now=now)


def parse_fallback_time_bc(
    date_str: str,
    time_str: str,
    *,
    clock: LotteryClock | None = None,
    draw_nbr: int | None = None,
    now: datetime | None = None,
) -> tuple[str, str]:
    naive = fallback_naive(date_str, time_str)
    local = naive.strftime("%Y-%m-%dT%H:%M:%S")
    return _bc_local_to_cn(local, clock=clock, draw_nbr=draw_nbr, now=now)


def beijing_wall_to_utc(draw_date: str, draw_time: str) -> datetime:
    local = f"{draw_date}T{draw_time if len(draw_time) > 5 else draw_time + ':00'}"
    return zoned_time_to_utc(local, "Asia/Shanghai")


def today_beijing() -> str:
    return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
