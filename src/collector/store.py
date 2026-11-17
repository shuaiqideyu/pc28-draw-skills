"""进程内按期号去重。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from collector.bus import DrawBus
from collector.parse_bclc import CompletedKeno
from collector.timeutil import beijing_wall_to_utc


@dataclass
class DrawInsert:
    draw_nbr: int
    draw_date: str
    draw_time: str
    num1: int
    num2: int
    num3: int
    draw_sum: int
    source: str


class DrawLog:
    def __init__(self, bus: DrawBus) -> None:
        self.bus = bus
        self._draws: dict[int, DrawInsert] = {}
        self._keno: dict[int, CompletedKeno] = {}
        self._drawn_at: dict[int, datetime] = {}
        self._upcoming: dict[int, str | None] = {}

    async def close(self) -> None:
        return None

    async def latest_filled_keno_nbr(self) -> int | None:
        filled = [nbr for nbr, row in self._keno.items() if row.draw_nbrs]
        return max(filled) if filled else None

    async def latest_draw_nbr(self) -> int | None:
        return max(self._draws) if self._draws else None

    async def latest_drawn_at(self) -> datetime | None:
        if not self._drawn_at:
            return None
        return self._drawn_at[max(self._drawn_at)]

    async def upsert_upcoming(self, draw_nbr: int, countdown: str | None) -> None:
        self._upcoming[draw_nbr] = countdown

    async def fill_keno(self, row: CompletedKeno) -> bool:
        current = self._keno.get(row.draw_nbr)
        if current is not None and current.draw_nbrs:
            return False
        self._keno[row.draw_nbr] = row
        return True

    async def insert_draw(self, row: DrawInsert) -> bool:
        if row.draw_nbr in self._draws:
            return False
        self._draws[row.draw_nbr] = row
        self._drawn_at[row.draw_nbr] = beijing_wall_to_utc(row.draw_date, row.draw_time)
        self.bus.emit_new_draw(row.draw_nbr)
        return True

    async def missing_draw_nbrs(self, lo: int, hi: int) -> list[int]:
        if hi < lo:
            return []
        return [nbr for nbr in range(lo, hi + 1) if nbr not in self._draws]
