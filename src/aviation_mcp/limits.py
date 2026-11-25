"""Small building blocks shared by the upstream clients: a TTL cache and a token bucket.
Both take an injectable clock (and sleep), so tests run on fake time."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Hashable
from typing import Any

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class TtlCache:
    def __init__(self, clock: Clock = time.monotonic, max_entries: int = 512) -> None:
        self._clock = clock
        self._max = max_entries
        self._data: dict[Hashable, tuple[float, Any]] = {}

    def get(self, key: Hashable) -> Any | None:
        hit = self._data.get(key)
        if hit is None:
            return None
        expires_at, value = hit
        if self._clock() >= expires_at:
            del self._data[key]
            return None
        return value

    def set(self, key: Hashable, value: Any, ttl_s: float) -> None:
        if len(self._data) >= self._max:
            self._data.pop(next(iter(self._data)))  # drop the oldest insert
        self._data[key] = (self._clock() + ttl_s, value)


class TokenBucket:
    """At most `rate_per_minute` acquisitions per minute, with bursts up to `burst`."""

    def __init__(
        self,
        rate_per_minute: float,
        *,
        burst: float | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._rate = rate_per_minute / 60.0
        self._capacity = burst if burst is not None else rate_per_minute
        self._tokens = self._capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate)
                self._updated = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await self._sleep((1 - self._tokens) / self._rate)
