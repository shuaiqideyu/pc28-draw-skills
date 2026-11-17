"""进程内新期唤醒总线。"""

from __future__ import annotations

from collections.abc import Callable


class DrawBus:
    def __init__(self) -> None:
        self._handlers: set[Callable[[int], None]] = set()

    def emit_new_draw(self, draw_nbr: int) -> None:
        for handler in list(self._handlers):
            try:
                handler(draw_nbr)
            except Exception as exc:
                print(f"[derive.bus] handler error {exc}")

    def subscribe(self, handler: Callable[[int], None]) -> Callable[[], None]:
        self._handlers.add(handler)

        def unsubscribe() -> None:
            self._handlers.discard(handler)

        return unsubscribe
