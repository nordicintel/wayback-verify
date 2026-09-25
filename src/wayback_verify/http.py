"""Throttling, bounded retries with exponential backoff, and Retry-After."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Literal

import aiohttp
from multidict import CIMultiDictProxy

from .config import RateLimit, RetryPolicy
from .models import utcnow

Sleep = Callable[[float], Awaitable[None]]


class RateLimiter:
    """Concurrency cap plus a minimum spacing between request starts.

    ``pause(seconds)`` holds back every later request (bulk SPN backoff).
    """

    def __init__(self, limit: RateLimit, *, sleep: Sleep = asyncio.sleep) -> None:
        self.limit = limit
        self._sem = asyncio.Semaphore(max(1, limit.concurrency))
        self._lock = asyncio.Lock()
        self._next_start = 0.0
        self._paused_until = 0.0
        self._sleep = sleep

    def pause(self, seconds: float) -> None:
        self._paused_until = max(self._paused_until, time.monotonic() + seconds)

    @property
    def paused_for(self) -> float:
        return max(0.0, self._paused_until - time.monotonic())

    async def __aenter__(self) -> None:
        await self._sem.acquire()
        try:
            async with self._lock:
                while True:
                    now = time.monotonic()
                    wait = max(self._next_start, self._paused_until) - now
                    if wait <= 0:
                        break
                    await self._sleep(wait)
                self._next_start = time.monotonic() + self.limit.min_interval
        except BaseException:
            self._sem.release()
            raise

    async def __aexit__(self, *exc: object) -> None:
        self._sem.release()


def parse_retry_after(value: str | None) -> float | None:
    """Seconds from a Retry-After header (delta-seconds or HTTP-date)."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (when - utcnow()).total_seconds())


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    url: str
    headers: CIMultiDictProxy[str]
    body: bytes = b""
    # URLs of the redirects followed before ``url``.
    history: tuple[str, ...] = ()
    attempts: int = 1

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    @property
    def retry_after(self) -> float | None:
        return parse_retry_after(self.headers.get("Retry-After"))

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class TransportError(Exception):
    """A request failed after all retries (network error or HTTP status)."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class Transport:
    """Issues requests through a limiter with bounded retries."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        retry: RetryPolicy,
        *,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.session = session
        self.retry = retry
        self._sleep = sleep

    def backoff(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, self.retry.max_delay)
        delay = self.retry.base_delay * 2 ** (attempt - 1)
        return float(min(self.retry.max_delay, delay * random.uniform(0.8, 1.2)))

    async def request(
        self,
        method: str,
        url: str,
        *,
        limiter: RateLimiter,
        params: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        allow_redirects: bool = True,
        read: Literal["body", "none"] = "body",
        retry_statuses: frozenset[int] | None = None,
    ) -> Response:
        """Send a request, retrying transient failures.

        Retried statuses that persist are returned as the final ``Response``;
        network errors that persist raise ``TransportError``. With
        ``read="none"`` the connection is closed without reading the body.
        """
        statuses = self.retry.retry_statuses
        if retry_statuses is not None:
            statuses = retry_statuses
        attempts = max(1, self.retry.max_attempts)
        for attempt in range(1, attempts + 1):
            try:
                async with limiter:
                    resp = await self.session.request(
                        method,
                        url,
                        params=params,
                        data=data,
                        headers=headers,
                        allow_redirects=allow_redirects,
                    )
                    try:
                        body = await resp.read() if read == "body" else b""
                    finally:
                        if read == "none":
                            resp.close()
                        else:
                            resp.release()
                    result = Response(
                        status=resp.status,
                        url=str(resp.url),
                        headers=resp.headers,
                        body=body,
                        history=tuple(str(h.url) for h in resp.history),
                        attempts=attempt,
                    )
            except (aiohttp.ClientError, TimeoutError) as exc:
                if attempt == attempts:
                    raise TransportError(
                        f"{method} {url}: {type(exc).__name__}: {exc}"
                    ) from exc
                await self._sleep(self.backoff(attempt, None))
                continue
            if result.status in statuses and attempt < attempts:
                await self._sleep(self.backoff(attempt, result.retry_after))
                continue
            return result
        raise AssertionError("unreachable")
