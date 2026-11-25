"""OpenSky Network REST client (OAuth2 client credentials via TokenManager).

- 401: drop the token, fetch a new one, retry once. A second 401 is a credentials error.
- Credits: three independent buckets (states, flights, tracks). X-Rate-Limit-Remaining is
  read into `credits_remaining`; below `credit_floor` the server stops calling that bucket
  for an hour by itself, to keep a reserve.
- 429: X-Rate-Limit-Retry-After-Seconds <= 10 s -> sleep and retry once; longer -> error
  that states the wait. It never blocks a chat for hours. The wait is remembered per
  bucket: until it is over, uncached calls to that bucket fail at once without a request.
  X-Rate-Limit-Remaining on a 429 is recorded too.
- A 200 whose body does not have the documented shape is an error, never cached, so format
  drift is not mistaken for "no aircraft" or "no flights".
- Cache: states 10 s, flights 6 h.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from aviation_mcp.auth import OpenSkyAuthError, TokenManager
from aviation_mcp.limits import Clock, Sleep, TtlCache

OPENSKY_BASE = "https://opensky-network.org/api"
MAX_SHORT_WAIT_S = 10.0
DEFAULT_CREDIT_FLOOR = 200
FLOOR_PAUSE_S = 3600.0
TTL_STATES_S = 10
TTL_FLIGHTS_S = 6 * 3600

Bucket = Literal["states", "flights"]
log = logging.getLogger(__name__)


class OpenSkyError(Exception):
    """OpenSky call failed. The message is written for the model."""


@dataclass(frozen=True)
class StatesSnapshot:
    time: int | None
    states: list[list[Any]]
    credits_remaining: int | None


class OpenSkyClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        tokens: TokenManager,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        credit_floor: int = DEFAULT_CREDIT_FLOOR,
    ) -> None:
        self._http = http
        self._tokens = tokens
        self._clock = clock
        self._sleep = sleep
        self._floor = credit_floor
        self._cache = TtlCache(clock)
        self._credits: dict[str, int] = {}
        self._paused_until: dict[str, float] = {}
        self._cooldown_until: dict[str, float] = {}  # from a 429's Retry-After

    # --- public API ------------------------------------------------------------------------

    async def states_in_box(
        self, lamin: float, lomin: float, lamax: float, lomax: float
    ) -> StatesSnapshot:
        params = [("lamin", lamin), ("lomin", lomin), ("lamax", lamax), ("lomax", lomax)]
        return await self._states([(k, str(round(v, 4))) for k, v in params])

    async def states_by_icao24(self, icao24: list[str]) -> StatesSnapshot:
        return await self._states([("icao24", a) for a in icao24])

    async def flights(
        self, airport: str, direction: Literal["arrivals", "departures"], begin: int, end: int
    ) -> tuple[list[dict[str, Any]], int | None]:
        endpoint = "arrival" if direction == "arrivals" else "departure"
        params = [("airport", airport), ("begin", str(begin)), ("end", str(end))]
        key = ("flights", endpoint, tuple(params))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        resp = await self._get("flights", f"/flights/{endpoint}", params)
        # OpenSky answers 404 when no flights are found in the interval.
        rows = [] if resp.status_code == 404 else self._json(resp)
        if not isinstance(rows, list) or not all(
            isinstance(r, dict) and isinstance(r.get("icao24"), str) for r in rows
        ):
            raise OpenSkyError(_unexpected_format_message("flights"))
        result = (rows, self._credits.get("flights"))
        self._cache.set(key, result, TTL_FLIGHTS_S)
        return result

    # --- internals -------------------------------------------------------------------------

    async def _states(self, params: list[tuple[str, str]]) -> StatesSnapshot:
        key = ("states", tuple(params))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        resp = await self._get("states", "/states/all", params)
        if resp.status_code == 404:
            snap_time, states = None, []
        else:
            body = self._json(resp)
            if not _valid_states_body(body):
                raise OpenSkyError(_unexpected_format_message("states"))
            snap_time, states = body["time"], body["states"] or []  # null = no aircraft
        snap = StatesSnapshot(
            time=snap_time,
            states=states,
            credits_remaining=self._credits.get("states"),
        )
        self._cache.set(key, snap, TTL_STATES_S)
        return snap

    def _check_budget(self, bucket: Bucket) -> None:
        now = self._clock()
        cooldown = self._cooldown_until.get(bucket)
        if cooldown is not None and now < cooldown:
            raise OpenSkyError(_rate_limited_message(bucket, cooldown - now))
        until = self._paused_until.get(bucket)
        if until is not None and now < until:
            left = self._credits.get(bucket)
            raise OpenSkyError(
                f"The OpenSky daily credit budget for /{bucket} is nearly used up ({left} "
                f"credits left, reserve floor {self._floor}). This server has paused calls to "
                "it; do not retry now. Tell the user flight data is temporarily unavailable."
            )

    def _record_credits(
        self, bucket: Bucket, resp: httpx.Response, *, pause_below_floor: bool = True
    ) -> None:
        raw = resp.headers.get("X-Rate-Limit-Remaining")
        if raw is None:
            return
        try:
            remaining = int(raw)
        except ValueError:
            return
        self._credits[bucket] = remaining
        if pause_below_floor and remaining < self._floor:
            self._paused_until[bucket] = self._clock() + FLOOR_PAUSE_S
            log.warning(
                "opensky credit floor reached",
                extra={"bucket": bucket, "credits_remaining": remaining},
            )

    async def _get(
        self, bucket: Bucket, path: str, params: list[tuple[str, str]]
    ) -> httpx.Response:
        self._check_budget(bucket)
        url = OPENSKY_BASE + path
        retried_401 = retried_429 = False
        while True:
            token = await self._tokens.get_token()
            try:
                resp = await self._http.get(
                    url, params=params, headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.HTTPError as exc:
                raise OpenSkyError(
                    f"Could not reach OpenSky ({type(exc).__name__}). Try again later."
                ) from None
            if resp.status_code == 401:
                if retried_401:
                    raise OpenSkyAuthError("OpenSky rejected the API client credentials")
                self._tokens.invalidate(token)
                retried_401 = True
                continue
            if resp.status_code == 429:
                wait = _retry_after(resp)
                if wait is not None and wait <= MAX_SHORT_WAIT_S and not retried_429:
                    retried_429 = True
                    await self._sleep(wait)
                    continue
                # Remember OpenSky's wait so the next call does not spend a request on it.
                # A valid Retry-After governs the pause; without one, the credit floor does.
                self._record_credits(bucket, resp, pause_below_floor=wait is None)
                if wait is not None:
                    self._cooldown_until[bucket] = max(
                        self._cooldown_until.get(bucket, 0.0), self._clock() + wait
                    )
                    log.warning(
                        "opensky rate limited",
                        extra={"bucket": bucket, "retry_after_s": wait},
                    )
                raise OpenSkyError(_rate_limited_message(bucket, wait))
            break
        self._record_credits(bucket, resp)
        if resp.status_code not in (200, 404):
            raise OpenSkyError(
                f"OpenSky answered HTTP {resp.status_code} for /{bucket}. Try again later."
            )
        return resp

    @staticmethod
    def _json(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except ValueError:
            raise OpenSkyError("OpenSky returned a response that is not JSON.") from None


def _retry_after(resp: httpx.Response) -> float | None:
    """The 429 wait in seconds, or None when the header is missing or not a sane number."""
    raw = resp.headers.get("X-Rate-Limit-Retry-After-Seconds")
    try:
        wait = float(raw) if raw is not None else None
    except ValueError:
        return None
    return wait if wait is not None and math.isfinite(wait) and wait >= 0 else None


def _valid_states_body(body: Any) -> bool:
    """Documented /states/all shape: {"time": int, "states": null | [[icao24, ...], ...]}."""
    if not isinstance(body, dict) or "states" not in body:
        return False
    t, states = body.get("time"), body["states"]
    if not isinstance(t, int) or isinstance(t, bool):
        return False
    if states is None:
        return True
    return isinstance(states, list) and all(
        isinstance(s, list) and s and isinstance(s[0], str) for s in states
    )


def _unexpected_format_message(bucket: str) -> str:
    # The body is not echoed: it is upstream detail the model cannot act on.
    return (
        f"OpenSky returned data in an unexpected format for /{bucket}. This is an upstream "
        "problem, not an empty result. Try again later."
    )


def _rate_limited_message(bucket: str, wait: float | None) -> str:
    if wait is None:
        when = "later"
    elif wait < 120:
        when = f"in about {wait:.0f} s"
    else:
        when = f"in about {wait / 60:.0f} min"
    return (
        f"OpenSky rate limit reached for /{bucket} (credits used up). OpenSky says to try "
        f"again {when}. Do not retry now; tell the user flight data is temporarily unavailable."
    )
