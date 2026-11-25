"""aviationweather.gov client: recorded real responses replayed with respx."""

from __future__ import annotations

import asyncio
from itertools import pairwise

import httpx
import pytest
import respx

from aviation_mcp.awc import AWC_BASE, AwcClient, AwcError
from aviation_mcp.limits import TokenBucket
from tests.conftest import load_fixture

METAR = f"{AWC_BASE}/metar"
TAF = f"{AWC_BASE}/taf"
STATION = f"{AWC_BASE}/stationinfo"


class RecordingSleep:
    def __init__(self, clock):
        self.clock = clock
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds)


@pytest.fixture
def sleep(clock):
    return RecordingSleep(clock)


@pytest.fixture
def client(http, clock, sleep):
    return AwcClient(http, clock=clock, sleep=sleep)


@respx.mock
async def test_metar_latest_sends_ids_and_format_without_hours(client):
    route = respx.get(METAR).mock(
        return_value=httpx.Response(200, json=load_fixture("awc/metar_esgg_essa_latest.json"))
    )
    rows = await client.metar(["ESGG", "ESSA"], hours_back=0)
    assert [r["icaoId"] for r in rows] == ["ESGG", "ESSA"]
    req = route.calls.last.request
    assert dict(req.url.params) == {"ids": "ESGG,ESSA", "format": "json"}
    assert req.headers["user-agent"].startswith("aviation-mcp/")


@respx.mock
async def test_metar_history_sends_hours(client):
    route = respx.get(METAR).mock(
        return_value=httpx.Response(200, json=load_fixture("awc/metar_esgg_3h.json"))
    )
    rows = await client.metar(["ESGG"], hours_back=3)
    assert len(rows) == 6
    assert route.calls.last.request.url.params["hours"] == "3"


@respx.mock
async def test_204_means_no_data(client):
    respx.get(METAR).mock(return_value=httpx.Response(204))
    assert await client.metar(["ZZZZ"], hours_back=0) == []


@respx.mock
async def test_metar_is_cached_for_five_minutes(client, clock):
    route = respx.get(METAR).mock(
        return_value=httpx.Response(200, json=load_fixture("awc/metar_esgg_essa_latest.json"))
    )
    await client.metar(["ESGG", "ESSA"], hours_back=0)
    clock.advance(299)
    await client.metar(["ESGG", "ESSA"], hours_back=0)
    assert route.call_count == 1
    clock.advance(2)
    await client.metar(["ESGG", "ESSA"], hours_back=0)
    assert route.call_count == 2


@respx.mock
async def test_taf_sends_ids(client):
    route = respx.get(TAF).mock(
        return_value=httpx.Response(200, json=load_fixture("awc/taf_esgg_essa.json"))
    )
    rows = await client.taf(["ESGG", "ESSA"])
    assert {r["icaoId"] for r in rows} == {"ESGG", "ESSA"}
    assert route.calls.last.request.url.params["ids"] == "ESGG,ESSA"


@respx.mock
async def test_station_returns_coordinates_and_is_cached(client, clock):
    route = respx.get(STATION).mock(
        return_value=httpx.Response(200, json=load_fixture("awc/stationinfo_essa.json"))
    )
    st = await client.station("ESSA")
    assert (st["lat"], st["lon"]) == (59.664, 17.92)
    clock.advance(23 * 3600)
    await client.station("ESSA")
    assert route.call_count == 1


@respx.mock
async def test_unknown_station_is_none(client):
    respx.get(STATION).mock(return_value=httpx.Response(200, json=[]))
    assert await client.station("ZZZZ") is None


@pytest.mark.parametrize("status", [429, 502, 504])
@respx.mock
async def test_transient_errors_back_off_and_retry(client, clock, sleep, status):
    stamps: list[float] = []
    replies = iter(
        [
            httpx.Response(status),
            httpx.Response(status),
            httpx.Response(200, json=load_fixture("awc/metar_esgg_essa_latest.json")),
        ]
    )

    def handler(request):
        stamps.append(clock())
        return next(replies)

    route = respx.get(METAR).mock(side_effect=handler)
    rows = await client.metar(["ESGG", "ESSA"], hours_back=0)
    assert len(rows) == 2
    assert route.call_count == 3
    # Backoff sleeps come first after each failure (base + jitter); the pacer may top up.
    assert 0.5 <= sleep.calls[0] < 1.0
    assert 1.0 <= sleep.calls[-1] < 2.0
    gaps = [b - a for a, b in pairwise(stamps)]
    assert gaps[0] >= 1.0 - 1e-9 and gaps[1] >= 1.0  # retries are paced too


@respx.mock
async def test_gives_up_after_two_retries_with_actionable_error(client):
    route = respx.get(METAR).mock(return_value=httpx.Response(502))
    with pytest.raises(AwcError) as exc:
        await client.metar(["ESGG"], hours_back=0)
    assert route.call_count == 3
    assert "aviationweather.gov" in str(exc.value) and "502" in str(exc.value)
    assert "try again" in str(exc.value).lower()


@respx.mock
async def test_client_error_is_not_retried(client):
    route = respx.get(METAR).mock(return_value=httpx.Response(400, text="bad parameter"))
    with pytest.raises(AwcError):
        await client.metar(["ESGG"], hours_back=0)
    assert route.call_count == 1


@respx.mock
async def test_network_error_becomes_awc_error(client):
    respx.get(METAR).mock(side_effect=httpx.ConnectTimeout("timed out"))
    with pytest.raises(AwcError) as exc:
        await client.metar(["ESGG"], hours_back=0)
    assert "could not reach aviationweather.gov" in str(exc.value).lower()


# --- rate limit: measured on dispatch timestamps, not on accumulated sleep -----------------

AWC_PUBLISHED_LIMIT_PER_MIN = 100
INTENDED_PER_MIN = 60


def max_in_window(stamps: list[float], window: float = 60.0) -> int:
    """Most dispatches inside any half-open window (t - window, t]."""
    stamps = sorted(stamps)
    best = j = 0
    for i, t in enumerate(stamps):
        while stamps[j] <= t - window:
            j += 1
        best = max(best, i - j + 1)
    return best


def stamping(clock, stamps, reply=lambda n: httpx.Response(200, json=[])):
    def handler(request):
        stamps.append(clock())
        return reply(len(stamps))

    return handler


def assert_paced(stamps: list[float]) -> None:
    assert max_in_window(stamps) <= INTENDED_PER_MIN < AWC_PUBLISHED_LIMIT_PER_MIN
    gaps = [b - a for a, b in pairwise(stamps)]
    assert min(gaps) >= 1.0 - 1e-9  # burst = 1: never two requests within a second


async def test_window_measure_detects_a_burst(clock):
    """Negative control: the generic limiter's default burst (a full minute's tokens) is the
    behaviour the review caught, 110 dispatches in 50 s. The measure must flag it."""

    async def fake_sleep(s):
        clock.advance(s)

    bucket = TokenBucket(60, clock=clock, sleep=fake_sleep)  # burst defaults to 60
    stamps: list[float] = []
    while not stamps or clock() - stamps[0] < 50:
        await bucket.acquire()
        stamps.append(clock())
    assert max_in_window(stamps) > AWC_PUBLISHED_LIMIT_PER_MIN


@respx.mock
async def test_sequential_cache_misses_stay_under_the_limit_in_every_window(client, clock):
    stamps: list[float] = []
    respx.get(STATION).mock(side_effect=stamping(clock, stamps))
    for i in range(150):
        await client.station(f"Z{i:03d}")  # distinct ids, so no cache hits
    assert len(stamps) == 150
    assert_paced(stamps)


@respx.mock
async def test_retries_count_against_the_limit(client, clock):
    def reply(n: int) -> httpx.Response:
        # Every other dispatch fails with 502, so half of all requests are retries.
        return httpx.Response(502) if n % 2 else httpx.Response(200, json=[])

    stamps: list[float] = []
    respx.get(STATION).mock(side_effect=stamping(clock, stamps, reply))
    for i in range(75):
        await client.station(f"Z{i:03d}")
    assert len(stamps) == 150
    assert_paced(stamps)


@respx.mock
async def test_concurrent_cache_misses_stay_under_the_limit(client, clock):
    stamps: list[float] = []
    respx.get(STATION).mock(side_effect=stamping(clock, stamps))
    await asyncio.gather(*(client.station(f"Z{i:03d}") for i in range(150)))
    assert len(stamps) == 150
    assert_paced(stamps)


# --- unexpected 200 bodies are errors, not "no data", and are never cached ------------------


@pytest.mark.parametrize(
    "body",
    [
        {"error": "synthetic upstream format drift"},
        "synthetic upstream format drift",
        [1, 2],
        [{"no_icao_id": "synthetic upstream format drift"}],
        [{"icaoId": None}],
    ],
    ids=["object", "string", "scalars", "row-without-icaoId", "null-icaoId"],
)
@respx.mock
async def test_unexpected_shape_is_an_error_and_not_cached(client, body):
    route = respx.get(METAR).mock(
        side_effect=[
            httpx.Response(200, json=body),
            httpx.Response(200, json=load_fixture("awc/metar_esgg_essa_latest.json")),
        ]
    )
    with pytest.raises(AwcError) as exc:
        await client.metar(["ESGG", "ESSA"], hours_back=0)
    msg = str(exc.value)
    assert "unexpected format" in msg and "not missing weather" in msg
    assert "synthetic upstream format drift" not in msg  # body is not echoed
    rows = await client.metar(["ESGG", "ESSA"], hours_back=0)
    assert route.call_count == 2  # the bad answer was not cached
    assert [r["icaoId"] for r in rows] == ["ESGG", "ESSA"]


@respx.mock
async def test_valid_empty_list_is_still_no_data(client):
    route = respx.get(METAR).mock(return_value=httpx.Response(200, json=[]))
    assert await client.metar(["ZZZZ"], hours_back=0) == []
    assert await client.metar(["ZZZZ"], hours_back=0) == []
    assert route.call_count == 1  # genuine no-data is cached as before
