"""BCLC / PlayNow JSON，以及 pc28.help 备用 Keno JSON 解析。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from collector.pc28 import is_valid_keno_nums
from collector.timeutil import (
    LotteryClock,
    parse_fallback_time_bc,
    parse_primary_time_bc,
)


@dataclass
class CompletedKeno:
    draw_nbr: int
    draw_date: str
    draw_time: str
    draw_nbrs: list[int]
    draw_bonus: int | None


@dataclass
class UpcomingKeno:
    draw_nbr: int
    countdown: str


def parse_primary(
    raw: object,
    clock: LotteryClock | None = None,
) -> tuple[CompletedKeno | None, UpcomingKeno | None]:
    if not isinstance(raw, dict):
        return None, None
    draw_nbr = raw.get("draw")
    nums = raw.get("num")
    draw_date_iso = raw.get("drawDate")

    completed: CompletedKeno | None = None
    if isinstance(draw_nbr, int) and isinstance(draw_date_iso, str):
        try:
            sorted_nums = (
                sorted(int(n) for n in nums) if isinstance(nums, list) else []
            )
        except (TypeError, ValueError):
            sorted_nums = []
        if is_valid_keno_nums(sorted_nums):
            draw_date, draw_time = parse_primary_time_bc(
                draw_date_iso, clock=clock, draw_nbr=draw_nbr
            )
            bonus = raw.get("bonus")
            completed = CompletedKeno(
                draw_nbr=draw_nbr,
                draw_date=draw_date,
                draw_time=draw_time,
                draw_nbrs=sorted_nums,
                draw_bonus=bonus if isinstance(bonus, int) else None,
            )

    upcoming: UpcomingKeno | None = None
    countdown = raw.get("nextKenoDraw")
    if isinstance(draw_nbr, int) and isinstance(countdown, str) and countdown:
        upcoming = UpcomingKeno(draw_nbr=draw_nbr + 1, countdown=countdown)

    return completed, upcoming


def parse_fallback(
    raw: object,
    clock: LotteryClock | None = None,
) -> list[CompletedKeno]:
    items: list[object]
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = [raw]
    else:
        items = []

    out: list[CompletedKeno] = []
    pending: list[tuple[int, str, str | None, list[int], int | None]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        draw_nbr = item.get("drawNbr", item.get("draw"))
        if not isinstance(draw_nbr, int):
            continue
        nbrs_raw = item.get("drawNbrs", item.get("num"))
        try:
            sorted_nums = (
                sorted(int(n) for n in nbrs_raw) if isinstance(nbrs_raw, list) else []
            )
        except (TypeError, ValueError):
            continue
        if not is_valid_keno_nums(sorted_nums):
            continue
        date_str = item.get("drawDate")
        time_str = item.get("drawTime")
        if not isinstance(date_str, str):
            continue
        bonus = item.get("bonus", item.get("drawBonus"))
        pending.append(
            (
                draw_nbr,
                date_str,
                time_str if isinstance(time_str, str) else None,
                sorted_nums,
                bonus if isinstance(bonus, int) else None,
            )
        )
    pending.sort(key=lambda row: row[0])
    for draw_nbr, date_str, time_str, sorted_nums, bonus in pending:
        try:
            if isinstance(time_str, str) and re.search(r"AM|PM", time_str, re.I):
                draw_date, draw_time = parse_fallback_time_bc(
                    date_str, time_str, clock=clock, draw_nbr=draw_nbr
                )
            elif "T" in date_str:
                draw_date, draw_time = parse_primary_time_bc(
                    date_str, clock=clock, draw_nbr=draw_nbr
                )
            else:
                continue
        except ValueError:
            continue
        out.append(
            CompletedKeno(
                draw_nbr=draw_nbr,
                draw_date=draw_date,
                draw_time=draw_time,
                draw_nbrs=sorted_nums,
                draw_bonus=bonus,
            )
        )
    return out


def _help_nbr(raw: object) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, str) and raw.isdigit():
        nbr = int(raw)
        return nbr if nbr > 0 else None
    return None


def _help_nbrs(raw: object) -> list[int] | None:
    parts: list[object]
    if isinstance(raw, str):
        parts = [piece.strip() for piece in raw.split(",") if piece.strip()]
    elif isinstance(raw, list):
        parts = raw
    else:
        return None
    try:
        nums = sorted(int(n) for n in parts)
    except (TypeError, ValueError):
        return None
    if not is_valid_keno_nums(nums):
        return None
    return nums


def _help_bonus(raw: object) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None


def parse_help_keno(raw: object) -> list[CompletedKeno]:
    """pc28.help `/api/keno.json`：期号、北京墙钟、逗号分隔的 20 个号。不改时区。"""
    if not isinstance(raw, dict):
        return []
    items = raw.get("data")
    if not isinstance(items, list):
        return []
    out: list[CompletedKeno] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        draw_nbr = _help_nbr(item.get("nbr"))
        nums = _help_nbrs(item.get("nbrs"))
        date_str = item.get("date")
        time_str = item.get("time")
        if (
            draw_nbr is None
            or nums is None
            or not isinstance(date_str, str)
            or not isinstance(time_str, str)
        ):
            continue
        try:
            wall = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        out.append(
            CompletedKeno(
                draw_nbr=draw_nbr,
                draw_date=wall.strftime("%Y-%m-%d"),
                draw_time=wall.strftime("%H:%M:%S"),
                draw_nbrs=nums,
                draw_bonus=_help_bonus(item.get("bonus")),
            )
        )
    out.sort(key=lambda row: row.draw_nbr)
    return out
