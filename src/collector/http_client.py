"""
PlayNow / BCLC HTTP 客户端。

加拿大机房直连，curl_cffi 模拟 Chrome（TLS / HTTP2 指纹）。
BCLC 优先 IPv6，没有路由时退回 IPv4；PlayNow 没有 AAAA，固定 IPv4。
连接复用；传输失败先丢掉再连。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession
from curl_cffi.requests import exceptions as curl_exc

DEFAULT_TIMEOUT_MS = 8_000

# libcurl CURL_IPRESOLVE_V4 / V6
_IPRESOLVE_V4 = 1
_IPRESOLVE_V6 = 2


def deny_cooldown_sec(streak: int) -> float:
    """换连接后仍 403 的歇期。20 秒起翻倍，封顶 120 秒，避免对着 WAF 连打。"""
    if streak <= 1:
        return 20.0
    return min(120.0, 20.0 * (2 ** (streak - 1)))


def official_headers(site_origin: str) -> dict[str, str]:
    """Origin / Referer 跟目标站。User-Agent 与 Client Hints 交给 Chrome 指纹，避免和 TLS 不一致。"""
    origin = site_origin.rstrip("/")
    return {
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{origin}/keno/",
        "Origin": origin,
    }


class OfficialH2Client:
    """直连官方 API：Chrome 指纹 + 按站钉地址族。"""

    def __init__(
        self,
        *,
        name: str,
        site_origin: str,
        prefer_ipv6: bool = False,
    ) -> None:
        self._name = name
        self._headers = official_headers(site_origin)
        self._use_ipv6 = prefer_ipv6
        self._fell_back = False
        self._fallback_reason = ""
        self._family_gen = 0
        self._session: AsyncSession | None = None
        self._lock = asyncio.Lock()
        self._fallback_lock = asyncio.Lock()
        self._peer_logged = False
        self._cool_until = 0.0
        self._saw_403 = False
        self._deny_streak = 0
        self._req_lock = asyncio.Lock()

    def _open(self) -> AsyncSession:
        family = _IPRESOLVE_V6 if self._use_ipv6 else _IPRESOLVE_V4
        self._session = AsyncSession(
            impersonate="chrome",
            headers=self._headers,
            timeout=8,
            max_clients=4,
            trust_env=False,
            curl_options={CurlOpt.IPRESOLVE: family},
        )
        return self._session

    async def _session_get(self) -> AsyncSession:
        async with self._lock:
            if self._session is None:
                return self._open()
            return self._session

    async def warmup(self, url: str) -> bool:
        """预热 TCP/TLS/HTTP2，开奖前持有连接避免冷启动。"""
        try:
            status, _ = await self.get(url, timeout_ms=8_000)
            ok = status == 200
            print(
                f"[http.{self._name}] warmup {'ok' if ok else f'HTTP {status}'} {url.split('?')[0]}"
            )
            return ok
        except Exception as err:
            print(f"[http.{self._name}] warmup fail: {type(err).__name__}: {err}")
            await self._rebuild()
            return False

    async def get(
        self, url: str, timeout_ms: int = DEFAULT_TIMEOUT_MS
    ) -> tuple[int, Any]:
        # 主接口和 fallback 共用一个客户端。歇期内不要返回假 403，否则轮询会每几秒记一次失败。
        async with self._req_lock:
            wait = self._cool_until - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            status, data = await self._fetch(url, timeout_ms)
            if status != 403:
                self._deny_streak = 0
                self._note_recovered()
                return status, data
            # 同一条连接被 WAF 卡住时，新连接往往立刻 200。不要在坏会话上继续打。
            await self._rebuild()
            self._saw_403 = True
            print(f"[http.{self._name}] HTTP 403，换连接")
            status, data = await self._fetch(url, timeout_ms)
            if status != 403:
                self._deny_streak = 0
                self._note_recovered()
                return status, data
            self._deny_streak += 1
            cool = deny_cooldown_sec(self._deny_streak)
            self._cool_until = time.monotonic() + cool
            print(f"[http.{self._name}] 仍是 HTTP 403，{cool:.0f} 秒后再试")
            return status, data

    def _note_recovered(self) -> None:
        if not self._saw_403:
            return
        self._saw_403 = False
        print(f"[http.{self._name}] 已恢复（此前 HTTP 403）")

    async def _fetch(
        self, url: str, timeout_ms: int
    ) -> tuple[int, Any]:
        timeout = timeout_ms / 1000
        gen = self._family_gen
        try:
            return await self._request(url, timeout)
        except curl_exc.ConnectionError as err:
            # 并发预热会同时撞上 IPv6 失败：只切一次，进行中的请求改走新会话
            if self._use_ipv6 or gen != self._family_gen:
                async with self._fallback_lock:
                    if self._use_ipv6:
                        await self._switch_ipv4(err)
                return await self._request(url, timeout)
            await self._rebuild()
            raise
        except curl_exc.Timeout:
            await self._rebuild()
            raise
        except curl_exc.RequestException:
            await self._rebuild()
            return await self._request(url, timeout)

    async def _request(self, url: str, timeout: float) -> tuple[int, Any]:
        session = await self._session_get()
        resp = await session.get(url, timeout=timeout)
        self._note_peer(getattr(resp, "primary_ip", "") or "")
        status = resp.status_code
        try:
            return status, resp.json()
        except Exception:
            return status, resp.text

    async def _switch_ipv4(self, err: BaseException) -> None:
        async with self._lock:
            self._use_ipv6 = False
            self._fell_back = True
            self._fallback_reason = type(err).__name__
            self._family_gen += 1
            old = self._session
            self._session = None
        if old is not None:
            await old.close()

    def _note_peer(self, ip: str) -> None:
        if self._peer_logged or not ip:
            return
        self._peer_logged = True
        if self._fell_back:
            print(
                f"[http.{self._name}] IPv6 不可用（{self._fallback_reason}），改走 IPv4 {ip}"
            )
            return
        family = "IPv6" if ":" in ip else "IPv4"
        print(f"[http.{self._name}] {family} {ip}")

    async def _rebuild(self) -> None:
        async with self._lock:
            old = self._session
            self._session = None
        if old is not None:
            await old.close()

    async def close(self) -> None:
        await self._rebuild()


class ThrottledLogger:
    def __init__(self, name: str, every: int = 60) -> None:
        self.name = name
        self.every = every
        self.count = 0

    def fail(self, label: str, err: BaseException | object) -> None:
        self.count += 1
        if self.count == 1 or self.count % self.every == 0:
            text = str(err).strip()
            detail = f"{type(err).__name__}: {text}" if text else type(err).__name__
            print(f"[{self.name}] {label} (连续第 {self.count} 次): {detail}")

    def ok(self) -> None:
        if self.count > 0:
            print(f"[{self.name}] 已恢复 (此前连续失败 {self.count} 次)")
            self.count = 0
