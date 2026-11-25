"""OpenSky client: bearer auth with one retry on 401, credit budget, 429 handling, cache.
Fixtures are synthetic (see tests/fixtures/opensky/README.md); no call leaves the machine."""

from __future__ import annotations

import httpx
import pytest
import respx
from pydantic import SecretStr

from aviation_mcp.auth import TOKEN_URL, OpenSkyAuthError, TokenManager
from aviation_mcp.opensky import OPENSKY_BASE, OpenSkyClient, OpenSkyError
from tests.conftest import FAKE_CLIENT_ID, FAKE_CLIENT_SECRET, load_fixture

STATES = f"{OPENSKY_BASE}/states/all"
ARRIVALS = f"{OPENSKY_BASE}/flights/arrival"
DEPARTURES = f"{OPENSKY_BASE}/flights/departure"
BOX = (59.3, 17.2, 60.0, 18.6)


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
    tokens = TokenManager(http, FAKE_CLIENT_ID, SecretStr(FAKE_CLIENT_SECRET), clock=clock)
    return OpenSkyClient(http, tokens, clock=clock, sleep=sleep)


def tokens_route(*tokens: str):
    return respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": t, "expires_in": 1800}) for t in tokens
        ]
    )


def states_ok(remaining: int | None = 3999, fixture="opensky/states_near_essa.json"):
    headers = {} if remaining is None else {"X-Rate-Limit-Remaining": str(remaining)}
    return httpx.Response(200, json=load_fixture(fixture), headers=headers)


@respx.mock
async def test_states_in_box_sends_bearer_and_box(client):
    tokens_route("tok-1")
    route = respx.get(STATES).mock(return_value=states_ok(3990))
    snap = await client.states_in_box(*BOX)
    req = route.calls.last.request
    assert req.headers["authorization"] == "Bearer tok-1"
    assert dict(req.url.params) == {
        "lamin": "59.3",
        "lomin": "17.2",
        "lamax": "60.0",
        "lomax": "18.6",
    }
    assert snap.time == 1795600000
    assert len(snap.states) == 5
    assert snap.credits_remaining == 3990


@respx.mock
async def test_states_by_icao24_repeats_the_parameter(client):
    tokens_route("tok-1")
    route = respx.get(STATES).mock(return_value=states_ok(fixture="opensky/states_icao24.json"))
    snap = await client.states_by_icao24(["f0f001", "f0f099"])
    assert route.calls.last.request.url.params.get_list("icao24") == ["f0f001", "f0f099"]
    assert [s[0] for s in snap.states] == ["f0f001"]


@respx.mock
async def test_401_refreshes_token_and_retries_once(client):
    token = tokens_route("tok-old", "tok-new")
    api = respx.get(STATES).mock(side_effect=[httpx.Response(401), states_ok()])
    snap = await client.states_in_box(*BOX)
    assert len(snap.states) == 5
    assert token.call_count == 2
    assert api.calls[1].request.headers["authorization"] == "Bearer tok-new"


@respx.mock
async def test_second_401_is_a_credentials_error(client):
    tokens_route("tok-1", "tok-2")
    api = respx.get(STATES).mock(return_value=httpx.Response(401))
    with pytest.raises(OpenSkyAuthError) as exc:
        await client.states_in_box(*BOX)
    assert str(exc.value) == "OpenSky rejected the API client credentials"
    assert api.call_count == 2


@respx.mock
async def test_429_with_short_wait_sleeps_and_retries_once(client, sleep):
    tokens_route("tok-1")
    respx.get(STATES).mock(
        side_effect=[
            httpx.Response(429, headers={"X-Rate-Limit-Retry-After-Seconds": "7"}),
            states_ok(),
        ]
    )
    snap = await client.states_in_box(*BOX)
    assert len(snap.states) == 5
    assert sleep.calls == [7.0]


@respx.mock
async def test_429_with_long_wait_returns_the_wait_without_blocking(client, sleep):
    tokens_route("tok-1")
    api = respx.get(STATES).mock(
        return_value=httpx.Response(429, headers={"X-Rate-Limit-Retry-After-Seconds": "5400"})
    )
    with pytest.raises(OpenSkyError) as exc:
        await client.states_in_box(*BOX)
    assert sleep.calls == []
    assert api.call_count == 1
    msg = str(exc.value)
    assert "rate limit" in msg and "90 min" in msg


@respx.mock
async def test_429_twice_gives_up(client, sleep):
    tokens_route("tok-1")
    api = respx.get(STATES).mock(
        return_value=httpx.Response(429, headers={"X-Rate-Limit-Retry-After-Seconds": "2"})
    )
    with pytest.raises(OpenSkyError):
        await client.states_in_box(*BOX)
    assert api.call_count == 2
    assert sleep.calls == [2.0]


@respx.mock
async def test_stops_calling_a_bucket_below_the_credit_floor(client, clock):
    tokens_route("tok-1")
    states = respx.get(STATES).mock(return_value=states_ok(remaining=150))
    arrivals = respx.get(ARRIVALS).mock(
        return_value=httpx.Response(200, json=[], headers={"X-Rate-Limit-Remaining": "3000"})
    )
    await client.states_in_box(*BOX)
    clock.advance(11)  # past the 10 s states cache
    with pytest.raises(OpenSkyError) as exc:
        await client.states_in_box(*BOX)
    assert states.call_count == 1
    assert "credit" in str(exc.value) and "150" in str(exc.value)
    # The /flights bucket is independent and still usable.
    await client.flights("ESSA", "arrivals", 1795478400, 1795564800)
    assert arrivals.call_count == 1


@respx.mock
async def test_flights_maps_direction_to_endpoint_and_404_to_empty(client):
    tokens_route("tok-1")
    arr = respx.get(ARRIVALS).mock(
        return_value=httpx.Response(200, json=load_fixture("opensky/arrivals_essa.json"))
    )
    dep = respx.get(DEPARTURES).mock(return_value=httpx.Response(404))
    flights, _ = await client.flights("ESSA", "arrivals", 1795478400, 1795564800)
    assert len(flights) == 3
    assert dict(arr.calls.last.request.url.params) == {
        "airport": "ESSA",
        "begin": "1795478400",
        "end": "1795564800",
    }
    flights, _ = await client.flights("ESSA", "departures", 1795478400, 1795564800)
    assert flights == []
    assert dep.call_count == 1


@respx.mock
async def test_states_cached_10s_and_flights_cached_6h(client, clock):
    tokens_route("tok-1")
    states = respx.get(STATES).mock(return_value=states_ok())
    arr = respx.get(ARRIVALS).mock(return_value=httpx.Response(200, json=[]))
    await client.states_in_box(*BOX)
    await client.flights("ESSA", "arrivals", 1, 2)
    clock.advance(9)
    await client.states_in_box(*BOX)
    assert states.call_count == 1
    clock.advance(2)
    await client.states_in_box(*BOX)
    assert states.call_count == 2
    clock.advance(5 * 3600)
    await client.flights("ESSA", "arrivals", 1, 2)
    assert arr.call_count == 1


@pytest.mark.parametrize("status", [500, 503])
@respx.mock
async def test_server_errors_are_reported(client, status):
    tokens_route("tok-1")
    respx.get(STATES).mock(return_value=httpx.Response(status))
    with pytest.raises(OpenSkyError) as exc:
        await client.states_in_box(*BOX)
    assert str(status) in str(exc.value) and "try again" in str(exc.value).lower()


@respx.mock
async def test_network_error_is_reported(client):
    tokens_route("tok-1")
    respx.get(STATES).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(OpenSkyError) as exc:
        await client.states_in_box(*BOX)
    assert "could not reach opensky" in str(exc.value).lower()


# --- 429 cooldown is remembered per bucket ---------------------------------------------------


def exhausted(retry_after: str | None = "5400", remaining: str | None = "0"):
    headers = {}
    if retry_after is not None:
        headers["X-Rate-Limit-Retry-After-Seconds"] = retry_after
    if remaining is not None:
        headers["X-Rate-Limit-Remaining"] = remaining
    return httpx.Response(429, headers=headers)


@respx.mock
async def test_long_429_cooldown_blocks_the_next_call_without_a_request(client, clock, sleep):
    tokens_route("tok-1", "tok-2")
    api = respx.get(STATES).mock(side_effect=[exhausted(), states_ok(3999)])
    with pytest.raises(OpenSkyError):
        await client.states_in_box(*BOX)
    with pytest.raises(OpenSkyError) as exc:
        await client.states_in_box(*BOX)  # immediately again
    assert api.call_count == 1  # no second GET while OpenSky's wait runs
    assert sleep.calls == []  # and no hours-long sleep inside a tool call
    assert "rate limit" in str(exc.value) and "90 min" in str(exc.value)
    clock.advance(5399)
    with pytest.raises(OpenSkyError) as exc:
        await client.states_in_box(*BOX)
    assert api.call_count == 1
    assert "in about 1 s" in str(exc.value)  # the message counts down
    clock.advance(1)  # OpenSky's wait is over
    snap = await client.states_in_box(*BOX)
    assert api.call_count == 2 and len(snap.states) == 5
    assert snap.credits_remaining == 3999


@respx.mock
async def test_429_cooldown_leaves_the_other_bucket_usable(client):
    tokens_route("tok-1")
    respx.get(STATES).mock(return_value=exhausted())
    arrivals = respx.get(ARRIVALS).mock(
        return_value=httpx.Response(200, json=[], headers={"X-Rate-Limit-Remaining": "3000"})
    )
    with pytest.raises(OpenSkyError):
        await client.states_in_box(*BOX)
    await client.flights("ESSA", "arrivals", 1795478400, 1795564800)
    assert arrivals.call_count == 1


@respx.mock
async def test_429_without_retry_after_records_zero_credits_and_pauses(client):
    tokens_route("tok-1")
    api = respx.get(STATES).mock(return_value=exhausted(retry_after=None, remaining="0"))
    with pytest.raises(OpenSkyError):
        await client.states_in_box(*BOX)
    with pytest.raises(OpenSkyError) as exc:
        await client.states_in_box(*BOX)
    assert api.call_count == 1
    assert "(0 credits left" in str(exc.value)


@pytest.mark.parametrize("bad", ["nan", "inf", "-5", "soon"])
@respx.mock
async def test_invalid_retry_after_is_not_a_cooldown(client, sleep, bad):
    tokens_route("tok-1")
    api = respx.get(STATES).mock(return_value=exhausted(retry_after=bad, remaining=None))
    for _ in range(2):
        with pytest.raises(OpenSkyError):
            await client.states_in_box(*BOX)
    assert api.call_count == 2  # nothing to remember, so the next call may try
    assert sleep.calls == []


@respx.mock
async def test_short_429_then_success_does_not_leave_a_pause(client, clock, sleep):
    tokens_route("tok-1")
    api = respx.get(STATES).mock(
        side_effect=[exhausted(retry_after="3", remaining="0"), states_ok(3999), states_ok(3998)]
    )
    await client.states_in_box(*BOX)
    assert sleep.calls == [3.0]  # the one short-wait retry is kept
    clock.advance(11)  # past the states cache
    snap = await client.states_in_box(*BOX)
    assert api.call_count == 3 and snap.credits_remaining == 3998


@respx.mock
async def test_two_short_429s_remember_the_short_wait_only(client, clock, sleep):
    tokens_route("tok-1")
    api = respx.get(STATES).mock(
        side_effect=[exhausted("2", None), exhausted("2", None), states_ok()]
    )
    with pytest.raises(OpenSkyError):
        await client.states_in_box(*BOX)
    with pytest.raises(OpenSkyError):
        await client.states_in_box(*BOX)
    assert api.call_count == 2
    clock.advance(2)
    await client.states_in_box(*BOX)
    assert api.call_count == 3
    assert sleep.calls == [2.0]


# --- unexpected 200 bodies are errors, not "no data", and are never cached ------------------

DRIFT = "synthetic upstream format drift"


@pytest.mark.parametrize(
    "body",
    [
        {"error": DRIFT},
        [{"icao24": "f0f001", "states": DRIFT}],
        {"time": 1795600000},  # no "states" key
        {"time": DRIFT, "states": []},
        {"time": 1795600000, "states": {"icao24": DRIFT}},
        {"time": 1795600000, "states": [[1, DRIFT]]},
        {"time": 1795600000, "states": [DRIFT]},
    ],
    ids=["error-object", "list", "no-states", "bad-time", "states-object", "bad-row", "str-row"],
)
@respx.mock
async def test_states_unexpected_shape_is_an_error_and_not_cached(client, body):
    tokens_route("tok-1")
    api = respx.get(STATES).mock(side_effect=[httpx.Response(200, json=body), states_ok()])
    with pytest.raises(OpenSkyError) as exc:
        await client.states_in_box(*BOX)
    msg = str(exc.value)
    assert "unexpected format" in msg and "not an empty result" in msg and DRIFT not in msg
    snap = await client.states_in_box(*BOX)  # same query, no time has passed
    assert api.call_count == 2 and len(snap.states) == 5


@pytest.mark.parametrize(
    "body",
    [{"error": DRIFT}, DRIFT, [DRIFT], [{"callsign": DRIFT}], [{"icao24": None}]],
    ids=["error-object", "string", "str-row", "row-without-icao24", "null-icao24"],
)
@respx.mock
async def test_flights_unexpected_shape_is_an_error_and_not_cached(client, body):
    tokens_route("tok-1")
    api = respx.get(ARRIVALS).mock(
        side_effect=[
            httpx.Response(200, json=body),
            httpx.Response(200, json=load_fixture("opensky/arrivals_essa.json")),
        ]
    )
    with pytest.raises(OpenSkyError) as exc:
        await client.flights("ESSA", "arrivals", 1795478400, 1795564800)
    assert "unexpected format" in str(exc.value) and DRIFT not in str(exc.value)
    rows, _ = await client.flights("ESSA", "arrivals", 1795478400, 1795564800)
    assert api.call_count == 2 and len(rows) == 3


@respx.mock
async def test_documented_empty_answers_are_still_no_data(client):
    tokens_route("tok-1")
    respx.get(STATES).mock(
        return_value=httpx.Response(200, json={"time": 1795600000, "states": None})
    )
    arr = respx.get(ARRIVALS).mock(return_value=httpx.Response(200, json=[]))
    snap = await client.states_in_box(*BOX)
    assert snap.states == [] and snap.time == 1795600000
    rows, _ = await client.flights("ESSA", "arrivals", 1, 2)
    assert rows == []
    await client.flights("ESSA", "arrivals", 1, 2)
    assert arr.call_count == 1  # a genuine empty answer is still cached
