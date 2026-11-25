"""All five tools end to end through the in-memory MCP Client, upstreams replayed by respx."""

from __future__ import annotations

import json
import math

import httpx
import pytest
import respx
from mcp import Client

from aviation_mcp.auth import TOKEN_URL
from aviation_mcp.awc import AWC_BASE
from aviation_mcp.opensky import OPENSKY_BASE
from tests.conftest import load_fixture

DAY_2026_11_24 = 1795478400  # 2026-11-24T00:00:00Z; tests run with "today" = 2026-11-25


async def call(server, tool, args):
    async with Client(server) as client:
        return await client.call_tool(tool, args)


def text_of(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "text", None))


async def ok(server, tool, args) -> dict:
    result = await call(server, tool, args)
    assert not result.is_error, text_of(result)
    # The spec recommends returning the same JSON as text next to structuredContent.
    assert json.loads(text_of(result)) == result.structured_content
    return result.structured_content


def awc(endpoint, fixture=None, status=200):
    body = load_fixture(fixture) if fixture else None
    return respx.get(f"{AWC_BASE}/{endpoint}").mock(
        return_value=httpx.Response(status, json=body) if body is not None else httpx.Response(status)
    )


def opensky_token():
    return respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 1800})
    )


# --- get_metar ---------------------------------------------------------------------------


@respx.mock
async def test_get_metar_decodes_reports_with_units(server):
    awc("metar", "awc/metar_esgg_essa_latest.json")
    out = await ok(server, "get_metar", {"stations": ["esgg", "ESSA"]})
    assert out["missing"] == [] and out["truncated"] is False
    assert out["source"] == "aviationweather.gov"
    esgg = out["reports"][0]
    assert esgg == {
        "station": "ESGG",
        "observed_utc": "2026-11-25T11:20:00Z",
        "raw": "METAR ESGG 251120Z 04008KT 9999 SCT014 M01/M03 Q1027",
        "flight_category": "VFR",
        "temp_c": -1,
        "dewpoint_c": -3,
        "wind_dir_deg": 40,
        "wind_kt": 8,
        "gust_kt": None,
        "visibility_sm": "6+",
        "altimeter_hpa": 1027,
        "clouds": [{"cover": "SCT", "base_ft": 1400}],
    }


@respx.mock
async def test_get_metar_lists_stations_without_a_report_as_missing(server):
    awc("metar", "awc/metar_esgg_essa_latest.json")
    out = await ok(server, "get_metar", {"stations": ["ESGG", "ESSA", "ZZZZ"]})
    assert [r["station"] for r in out["reports"]] == ["ESGG", "ESSA"]
    assert out["missing"] == ["ZZZZ"]


@respx.mock
async def test_get_metar_204_is_not_an_error(server):
    awc("metar", status=204)
    out = await ok(server, "get_metar", {"stations": ["ZZZZ"]})
    assert out["reports"] == [] and out["missing"] == ["ZZZZ"]


@respx.mock
async def test_get_metar_history_newest_first(server):
    route = awc("metar", "awc/metar_esgg_3h.json")
    out = await ok(server, "get_metar", {"stations": ["ESGG"], "hours_back": 3})
    assert route.calls.last.request.url.params["hours"] == "3"
    times = [r["observed_utc"] for r in out["reports"]]
    assert len(times) == 6 and times == sorted(times, reverse=True)


@respx.mock
async def test_get_metar_caps_reports_per_station_and_says_so(server):
    row = load_fixture("awc/metar_esgg_3h.json")[0]
    many = [dict(row, obsTime=row["obsTime"] - i * 1800) for i in range(25)]
    respx.get(f"{AWC_BASE}/metar").mock(return_value=httpx.Response(200, json=many))
    out = await ok(server, "get_metar", {"stations": ["ESGG", "ESSA"], "hours_back": 12})
    assert out["truncated"] is True
    assert len(out["reports"]) == 15  # 30 reports in total, split evenly per station


@respx.mock
async def test_get_metar_upstream_failure_is_a_tool_error(server):
    awc("metar", status=400)
    result = await call(server, "get_metar", {"stations": ["ESGG"]})
    assert result.is_error
    assert "aviationweather.gov answered HTTP 400" in text_of(result)


# --- get_taf -----------------------------------------------------------------------------


@respx.mock
async def test_get_taf_returns_raw_and_validity(server):
    awc("taf", "awc/taf_esgg_essa.json")
    out = await ok(server, "get_taf", {"stations": ["ESSA", "ESGG", "EKCH"]})
    by = {f["station"]: f for f in out["forecasts"]}
    assert list(by) == ["ESSA", "ESGG"]  # in the order asked for
    essa = by["ESSA"]
    assert essa["raw"].startswith("TAF ESSA 251130Z 2512/2612")
    assert essa["issued_utc"] == "2026-11-25T11:30:00Z"
    assert essa["valid_from_utc"] == "2026-11-25T12:00:00Z"
    assert essa["valid_to_utc"] == "2026-11-26T12:00:00Z"
    assert out["missing"] == ["EKCH"]


# --- aircraft_near_airport ---------------------------------------------------------------


@respx.mock
async def test_aircraft_near_airport_sorted_by_distance_within_radius(server):
    awc("stationinfo", "awc/stationinfo_essa.json")
    opensky_token()
    states = respx.get(f"{OPENSKY_BASE}/states/all").mock(
        return_value=httpx.Response(
            200,
            json=load_fixture("opensky/states_near_essa.json"),
            headers={"X-Rate-Limit-Remaining": "3987"},
        )
    )
    out = await ok(server, "aircraft_near_airport", {"airport": "essa"})
    assert out["airport"] == "ESSA"
    assert [a["icao24"] for a in out["aircraft"]] == ["f0f001", "f0f002"]
    first = out["aircraft"][0]
    assert first["callsign"] == "TEST1"
    assert first["distance_km"] == pytest.approx(0.87, abs=0.05)
    assert first["altitude_m"] == 450.0 and first["speed_ms"] == 75.2
    assert first["vertical_rate_ms"] == -3.5 and first["track_deg"] == 10.0
    assert first["on_ground"] is False
    assert out["count_total"] == 2 and out["returned"] == 2 and out["truncated"] is False
    assert out["credits_remaining"] == 3987
    assert out["time_utc"] == "2026-11-25T09:46:40Z"

    p = states.calls.last.request.url.params
    lat_span = float(p["lamax"]) - float(p["lamin"])
    lon_span = float(p["lomax"]) - float(p["lomin"])
    assert lat_span == pytest.approx(2 * 40 / 111.32, rel=0.01)
    assert lon_span == pytest.approx(lat_span / math.cos(math.radians(59.664)), rel=0.01)


@respx.mock
async def test_aircraft_near_airport_truncates_to_limit(server):
    awc("stationinfo", "awc/stationinfo_essa.json")
    opensky_token()
    respx.get(f"{OPENSKY_BASE}/states/all").mock(
        return_value=httpx.Response(200, json=load_fixture("opensky/states_near_essa.json"))
    )
    out = await ok(server, "aircraft_near_airport", {"airport": "ESSA", "radius_km": 150, "limit": 2})
    assert out["count_total"] == 4  # f0f004 has no position and is left out
    assert out["returned"] == 2 and out["truncated"] is True
    assert [a["icao24"] for a in out["aircraft"]] == ["f0f001", "f0f002"]


@respx.mock
async def test_max_radius_box_stays_in_the_one_credit_tier_at_high_latitude(server):
    respx.get(f"{AWC_BASE}/stationinfo").mock(
        return_value=httpx.Response(200, json=[{"icaoId": "ENTC", "lat": 69.68, "lon": 18.92}])
    )
    opensky_token()
    states = respx.get(f"{OPENSKY_BASE}/states/all").mock(
        return_value=httpx.Response(200, json={"time": 1, "states": []})
    )
    await ok(server, "aircraft_near_airport", {"airport": "ENTC", "radius_km": 150})
    p = states.calls.last.request.url.params
    area = (float(p["lamax"]) - float(p["lamin"])) * (float(p["lomax"]) - float(p["lomin"]))
    assert area <= 25.0


@respx.mock
async def test_aircraft_near_unknown_airport_says_how_to_fix(server):
    awc("stationinfo", status=204)
    result = await call(server, "aircraft_near_airport", {"airport": "ZZZZ"})
    assert result.is_error
    assert "ZZZZ" in text_of(result) and "ICAO" in text_of(result)


# --- get_aircraft ------------------------------------------------------------------------


@respx.mock
async def test_get_aircraft_returns_positions_and_not_found(server):
    opensky_token()
    route = respx.get(f"{OPENSKY_BASE}/states/all").mock(
        return_value=httpx.Response(200, json=load_fixture("opensky/states_icao24.json"))
    )
    out = await ok(server, "get_aircraft", {"icao24": ["F0F001", "f0f099"]})
    assert route.calls.last.request.url.params.get_list("icao24") == ["f0f001", "f0f099"]
    [a] = out["aircraft"]
    assert a["icao24"] == "f0f001" and a["latitude"] == 59.67 and a["longitude"] == 17.93
    assert a["distance_km"] is None
    assert out["not_found"] == ["f0f099"]
    assert out["airport"] is None


def state_row(icao24: str, lat: float = 59.67, lon: float = 17.93, callsign: str = "TEST1   "):
    return [icao24, callsign, "Sweden", 1795599999, 1795599999, lon, lat, 450.0, False,
            75.2, 10.0, -3.5, None, 480.0, "1001", False, 0, 0]  # fmt: skip


@respx.mock
async def test_get_aircraft_ignores_unrelated_rows_even_if_upstream_does(server):
    """The upstream filter is not trusted: 60 rows for other addresses come back for one
    requested address. Only the requested one may reach the model."""
    opensky_token()
    rows = [state_row(f"a{i:05x}") for i in range(59)] + [state_row("f0f001")]
    respx.get(f"{OPENSKY_BASE}/states/all").mock(
        return_value=httpx.Response(200, json={"time": 1795600000, "states": rows})
    )
    out = await ok(server, "get_aircraft", {"icao24": ["f0f001"]})
    assert [a["icao24"] for a in out["aircraft"]] == ["f0f001"]
    assert out["count_total"] == 1 and out["returned"] == 1 and out["truncated"] is False
    assert out["not_found"] == []


@respx.mock
async def test_get_aircraft_keeps_one_row_per_address_in_request_order(server):
    opensky_token()
    rows = [
        state_row("f0f002", callsign="SECOND  "),
        state_row("f0f001", lat=60.0, callsign="FIRST1  "),
        state_row("F0F001", lat=61.0, callsign="DUPE    "),  # repeat, different case
        state_row("abcdef"),
    ]
    respx.get(f"{OPENSKY_BASE}/states/all").mock(
        return_value=httpx.Response(200, json={"time": 1795600000, "states": rows})
    )
    out = await ok(server, "get_aircraft", {"icao24": ["f0f001", "f0f002", "f0f099"]})
    assert [(a["icao24"], a["callsign"]) for a in out["aircraft"]] == [
        ("f0f001", "FIRST1"),
        ("f0f002", "SECOND"),
    ]
    assert out["count_total"] == 2 and out["returned"] == 2
    assert out["not_found"] == ["f0f099"]


@respx.mock
async def test_get_aircraft_only_unrelated_rows_means_not_found(server):
    opensky_token()
    respx.get(f"{OPENSKY_BASE}/states/all").mock(
        return_value=httpx.Response(
            200, json={"time": 1795600000, "states": [state_row("abcdef"), state_row("123456")]}
        )
    )
    out = await ok(server, "get_aircraft", {"icao24": ["f0f001", "f0f002"]})
    assert out["aircraft"] == [] and out["count_total"] == 0
    assert out["not_found"] == ["f0f001", "f0f002"]


# --- upstream format drift is a tool error, not "no data" -----------------------------------

DRIFT = {"error": "synthetic upstream format drift"}


@respx.mock
async def test_get_metar_format_drift_is_a_tool_error_and_retried_next_time(server):
    route = respx.get(f"{AWC_BASE}/metar").mock(
        side_effect=[
            httpx.Response(200, json=DRIFT),
            httpx.Response(200, json=load_fixture("awc/metar_esgg_essa_latest.json")),
        ]
    )
    result = await call(server, "get_metar", {"stations": ["ESSA"]})
    assert result.is_error
    assert "unexpected format" in text_of(result) and "synthetic" not in text_of(result)
    out = await ok(server, "get_metar", {"stations": ["ESSA"]})
    assert route.call_count == 2 and out["missing"] == []


@respx.mock
async def test_airport_flights_format_drift_is_a_tool_error_and_retried_next_time(server):
    opensky_token()
    route = respx.get(f"{OPENSKY_BASE}/flights/arrival").mock(
        side_effect=[
            httpx.Response(200, json=DRIFT),
            httpx.Response(200, json=load_fixture("opensky/arrivals_essa.json")),
        ]
    )
    args = {"airport": "ESSA", "direction": "arrivals"}
    result = await call(server, "airport_flights", args)
    assert result.is_error
    assert "unexpected format" in text_of(result) and "synthetic" not in text_of(result)
    out = await ok(server, "airport_flights", args)
    assert route.call_count == 2 and out["count_total"] == 3


# --- date line and poles: reject, never clip ------------------------------------------------


def station_at(lat: float, lon: float, icao: str = "ZZDL"):
    return respx.get(f"{AWC_BASE}/stationinfo").mock(
        return_value=httpx.Response(200, json=[{"icaoId": icao, "lat": lat, "lon": lon}])
    )


@pytest.mark.parametrize("lon", [179.9, -179.9], ids=["east", "west"])
@respx.mock
async def test_aircraft_near_airport_rejects_a_date_line_crossing(server, lon):
    """The review's case: airport at (0, ±179.9), radius 40 km, an aircraft 22 km away on
    the other side of the date line. A clipped box would report a complete empty list."""
    station_at(0.0, lon)
    opensky_token()
    states = respx.get(f"{OPENSKY_BASE}/states/all").mock(
        return_value=httpx.Response(200, json={"time": 1, "states": [state_row("f0f001")]})
    )
    result = await call(server, "aircraft_near_airport", {"airport": "ZZDL", "radius_km": 40})
    assert result.is_error
    msg = text_of(result)
    assert "date line" in msg and "ZZDL" in msg
    assert "Retry with radius_km=11 or less" in msg
    assert states.call_count == 0  # rejected before any OpenSky request


@respx.mock
async def test_suggested_radius_near_the_date_line_works_and_stays_in_range(server):
    station_at(0.0, 179.9)
    opensky_token()
    states = respx.get(f"{OPENSKY_BASE}/states/all").mock(
        return_value=httpx.Response(200, json={"time": 1, "states": []})
    )
    await ok(server, "aircraft_near_airport", {"airport": "ZZDL", "radius_km": 11})
    p = states.calls.last.request.url.params
    assert -180 <= float(p["lomin"]) < float(p["lomax"]) <= 180


@respx.mock
async def test_aircraft_near_a_pole_is_rejected_without_a_retry_hint(server):
    station_at(-90.0, 0.0, icao="NZSP")
    opensky_token()
    states = respx.get(f"{OPENSKY_BASE}/states/all").mock(
        return_value=httpx.Response(200, json={"time": 1, "states": []})
    )
    result = await call(server, "aircraft_near_airport", {"airport": "NZSP", "radius_km": 5})
    assert result.is_error
    assert "Do not retry" in text_of(result)
    assert states.call_count == 0


@pytest.mark.parametrize("lat", [-85.0, -60.0, -30.0, 0.0, 30.0, 59.664, 69.68, 85.0])
@pytest.mark.parametrize("lon", [-179.0, -100.0, 0.0, 17.92, 100.0, 179.0])
@pytest.mark.parametrize("radius", [5, 40, 150])
def test_accepted_box_contains_the_whole_circle(lat, lon, radius):
    """Every point exactly radius_km away (spherical, as haversine_km measures) is inside
    the box. If the box cannot contain it without crossing, bounding_box must refuse."""
    from aviation_mcp import results

    try:
        lamin, lomin, lamax, lomax = results.bounding_box(lat, lon, radius)
    except results.UnsupportedArea:
        return
    assert -90 < lamin < lamax < 90 and -180 <= lomin < lomax <= 180
    d = radius / results.EARTH_RADIUS_KM
    p1, l1 = math.radians(lat), math.radians(lon)
    for bearing in range(0, 360, 2):
        b = math.radians(bearing)
        p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
        l2 = l1 + math.atan2(
            math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2)
        )
        plat, plon = math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180
        assert results.haversine_km(lat, lon, plat, plon) == pytest.approx(radius, rel=1e-6)
        assert lamin - 1e-9 <= plat <= lamax + 1e-9
        assert lomin - 1e-9 <= plon <= lomax + 1e-9


# --- airport_flights ---------------------------------------------------------------------


@respx.mock
async def test_airport_flights_defaults_to_yesterday_and_sorts_by_time(server):
    opensky_token()
    route = respx.get(f"{OPENSKY_BASE}/flights/arrival").mock(
        return_value=httpx.Response(200, json=load_fixture("opensky/arrivals_essa.json"))
    )
    out = await ok(server, "airport_flights", {"airport": "ESSA", "direction": "arrivals"})
    p = route.calls.last.request.url.params
    assert (p["airport"], int(p["begin"]), int(p["end"])) == (
        "ESSA",
        DAY_2026_11_24,
        DAY_2026_11_24 + 86400,
    )
    assert out["date_utc"] == "2026-11-24"
    assert [f["icao24"] for f in out["flights"]] == ["f0f012", "f0f013", "f0f011"]
    first = out["flights"][0]
    assert first == {
        "icao24": "f0f012",
        "callsign": "TEST12",
        "first_seen_utc": "2026-11-24T03:13:20Z",
        "last_seen_utc": "2026-11-24T04:03:20Z",
        "from_airport": "ENGM",
        "to_airport": "ESSA",
    }
    assert out["count_total"] == 3 and out["truncated"] is False


@respx.mock
async def test_airport_flights_hour_window_and_limit(server):
    opensky_token()
    route = respx.get(f"{OPENSKY_BASE}/flights/departure").mock(
        return_value=httpx.Response(200, json=load_fixture("opensky/arrivals_essa.json"))
    )
    out = await ok(
        server,
        "airport_flights",
        {
            "airport": "ESSA",
            "direction": "departures",
            "date_utc": "2026-11-20",
            "hour_from": 17,
            "hour_to": 24,
            "limit": 1,
        },
    )
    p = route.calls.last.request.url.params
    day = DAY_2026_11_24 - 4 * 86400
    assert (int(p["begin"]), int(p["end"])) == (day + 17 * 3600, day + 24 * 3600)
    assert out["returned"] == 1 and out["count_total"] == 3 and out["truncated"] is True


@respx.mock
async def test_airport_flights_none_found_is_empty_not_error(server):
    opensky_token()
    respx.get(f"{OPENSKY_BASE}/flights/arrival").mock(return_value=httpx.Response(404))
    out = await ok(server, "airport_flights", {"airport": "ESSB", "direction": "arrivals"})
    assert out["flights"] == [] and out["count_total"] == 0


@respx.mock
async def test_opensky_bad_credentials_is_a_tool_error_without_detail(server):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(401, json={"error": "invalid_client"}))
    result = await call(server, "get_aircraft", {"icao24": ["f0f001"]})
    assert result.is_error
    assert "OpenSky rejected the API client credentials" in text_of(result)
    assert "invalid_client" not in text_of(result)


# --- listing -----------------------------------------------------------------------------


async def test_tools_are_read_only_and_have_output_schemas(server):
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
    for t in tools:
        assert t.annotations.read_only_hint is True, t.name
        assert t.output_schema is not None, t.name
        assert t.description and len(t.description) > 40, t.name
