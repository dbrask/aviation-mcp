"""aviationweather.gov Data API client (no auth).

Paces every request, retries included, to one per second: at most 60 per minute and no
burst (published limit is 100). Sends a custom User-Agent, retries 429/502/504 with
exponential backoff and jitter (0.5 s, then 1 s, at most 2 retries), turns 204 into
"no data", and caches: METAR 5 min, TAF 15 min, stations 24 h. A 200 whose body is not a
JSON list of station rows (objects with an icaoId) is an error and is never cached, so
format drift is not mistaken for "no data".
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx

from aviation_mcp import __version__
from aviation_mcp.limits import Clock, Sleep, TokenBucket, TtlCache

AWC_BASE = "https://aviationweather.gov/api/data"
USER_AGENT = f"aviation-mcp/{__version__} (personal learning project)"
RETRY_STATUSES = {429, 502, 504}
BACKOFF_S = (0.5, 1.0)
TTL_METAR_S = 5 * 60
TTL_TAF_S = 15 * 60
TTL_STATION_S = 24 * 3600

log = logging.getLogger(__name__)


class AwcError(Exception):
    """aviationweather.gov failed. The message is written for the model."""


class AwcClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        rate_per_minute: int = 60,
    ) -> None:
        self._http = http
        self._sleep = sleep
        self._cache = TtlCache(clock)
        # burst=1: requests are spaced evenly; a full minute's budget is never sent at once.
        self._bucket = TokenBucket(rate_per_minute, burst=1, clock=clock, sleep=sleep)

    async def metar(self, stations: list[str], hours_back: int) -> list[dict[str, Any]]:
        params = {"ids": ",".join(stations), "format": "json"}
        if hours_back:
            params["hours"] = str(hours_back)
        return await self._cached_get("metar", params, TTL_METAR_S)

    async def taf(self, stations: list[str]) -> list[dict[str, Any]]:
        return await self._cached_get(
            "taf", {"ids": ",".join(stations), "format": "json"}, TTL_TAF_S
        )

    async def station(self, icao: str) -> dict[str, Any] | None:
        rows = await self._cached_get("stationinfo", {"ids": icao, "format": "json"}, TTL_STATION_S)
        return rows[0] if rows else None

    async def _cached_get(self, endpoint: str, params: dict[str, str], ttl_s: float) -> list:
        key = (endpoint, tuple(sorted(params.items())))
        cached = self._cache.get(key)
        if cached is not None:
            log.debug("cache hit", extra={"endpoint": endpoint, "cache_hit": True})
            return cached
        rows = await self._get(endpoint, params)
        self._cache.set(key, rows, ttl_s)
        return rows

    async def _get(self, endpoint: str, params: dict[str, str]) -> list:
        url = f"{AWC_BASE}/{endpoint}"
        for attempt in range(len(BACKOFF_S) + 1):
            await self._bucket.acquire()
            try:
                resp = await self._http.get(url, params=params, headers={"User-Agent": USER_AGENT})
            except httpx.HTTPError as exc:
                raise AwcError(
                    f"Could not reach aviationweather.gov ({type(exc).__name__}). Try again later."
                ) from None
            if resp.status_code in RETRY_STATUSES and attempt < len(BACKOFF_S):
                base = BACKOFF_S[attempt]
                await self._sleep(base + random.uniform(0, base * 0.99))
                continue
            break
        if resp.status_code == 204:
            return []
        if resp.status_code != 200:
            raise AwcError(
                f"aviationweather.gov answered HTTP {resp.status_code} for {endpoint}. "
                "Try again later, or check the station codes."
            )
        try:
            data = resp.json()
        except ValueError:
            raise AwcError("aviationweather.gov returned a response that is not JSON.") from None
        if not isinstance(data, list) or not all(
            isinstance(row, dict) and isinstance(row.get("icaoId"), str) for row in data
        ):
            # The body is not echoed: it is upstream detail the model cannot act on.
            raise AwcError(
                f"aviationweather.gov returned data in an unexpected format for {endpoint}. "
                "This is an upstream problem, not missing weather. Try again later."
            )
        return data
