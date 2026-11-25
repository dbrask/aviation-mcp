"""Input validation and error messages for all five tools, through the in-memory MCP Client.

Every bad call must come back as a tool execution error (is_error=True) whose text tells
the model how to fix the call. None of these calls may reach the network: validation
happens before any upstream request, and pytest-socket would fail the test otherwise.
"""

from __future__ import annotations

from datetime import date

import pytest
from mcp import Client

TODAY = date(2026, 11, 25)


async def call(server, tool: str, args: dict):
    async with Client(server) as client:
        return await client.call_tool(tool, args)


def text_of(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "text", None))


async def assert_error(server, tool, args, *needles):
    result = await call(server, tool, args)
    assert result.is_error, f"{tool}({args}) should be an error, got {text_of(result)}"
    text = text_of(result)
    for needle in needles:
        assert needle in text, f"{needle!r} not in error text:\n{text}"
    return text


# --- get_metar ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args,needles",
    [
        ({"stations": ["ARN"]}, ["'ARN' is not a 4-letter ICAO", "ESSA", "IATA"]),
        ({"stations": ["ES5A"]}, ["'ES5A' is not a 4-letter ICAO"]),
        ({"stations": ["ESSA;DROP"]}, ["is not a 4-letter ICAO"]),
        ({"stations": []}, ["stations", "at least 1"]),
        ({"stations": ["ESSA", "ESGG", "EKCH", "ENGM", "EFHK", "EGLL"]}, ["at most 5"]),
        ({"stations": ["ESSA"], "hours_back": 13}, ["hours_back", "less than or equal to 12"]),
        ({"stations": ["ESSA"], "hours_back": -1}, ["hours_back", "greater than or equal to 0"]),
        ({"stations": "ESSA"}, ["stations", "valid list"]),
        ({}, ["stations", "required"]),
    ],
)
async def test_get_metar_rejects_bad_input(server, args, needles):
    await assert_error(server, "get_metar", args, *needles)


# --- get_taf -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args,needles",
    [
        ({"stations": ["LHR"]}, ["'LHR' is not a 4-letter ICAO", "IATA"]),
        ({"stations": ["ESSA", "ESGG", "EKCH", "ENGM"]}, ["at most 3"]),
        ({"stations": []}, ["at least 1"]),
    ],
)
async def test_get_taf_rejects_bad_input(server, args, needles):
    await assert_error(server, "get_taf", args, *needles)


# --- aircraft_near_airport ---------------------------------------------------------------


@pytest.mark.parametrize(
    "args,needles",
    [
        ({"airport": "BMA"}, ["'BMA' is not a 4-letter ICAO"]),
        ({"airport": "ESSB", "radius_km": 500}, ["radius_km", "less than or equal to 150"]),
        ({"airport": "ESSB", "radius_km": 1}, ["radius_km", "greater than or equal to 5"]),
        ({"airport": "ESSB", "limit": 0}, ["limit", "greater than or equal to 1"]),
        ({"airport": "ESSB", "limit": 51}, ["limit", "less than or equal to 50"]),
        ({"airport": "ESSB", "radius_km": "far"}, ["radius_km", "valid integer"]),
    ],
)
async def test_aircraft_near_airport_rejects_bad_input(server, args, needles):
    await assert_error(server, "aircraft_near_airport", args, *needles)


# --- get_aircraft ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args,needles",
    [
        ({"icao24": ["SAS123"]}, ["'SAS123' is not an icao24", "hex", "aircraft_near_airport"]),
        ({"icao24": ["SE-RSA"]}, ["is not an icao24", "registration"]),
        ({"icao24": ["4ca7b5z"]}, ["is not an icao24"]),
        ({"icao24": []}, ["at least 1"]),
        ({"icao24": ["4ca7b5"] * 6}, ["at most 5"]),
    ],
)
async def test_get_aircraft_rejects_bad_input(server, args, needles):
    await assert_error(server, "get_aircraft", args, *needles)


# --- airport_flights ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "args,needles",
    [
        ({"airport": "ARN", "direction": "arrivals"}, ["is not a 4-letter ICAO"]),
        ({"airport": "ESSA", "direction": "landings"}, ["direction", "'arrivals' or 'departures'"]),
        (
            {"airport": "ESSA", "direction": "arrivals", "date_utc": "2026-11-25"},
            ["not available", "today", "overnight", "2026-11-24"],
        ),
        (
            {"airport": "ESSA", "direction": "arrivals", "date_utc": "2026-12-01"},
            ["future", "2026-11-24"],
        ),
        (
            {"airport": "ESSA", "direction": "arrivals", "date_utc": "2026-11-17"},
            ["7 days", "2026-11-18"],
        ),
        (
            {"airport": "ESSA", "direction": "arrivals", "date_utc": "yesterday"},
            ["date_utc", "YYYY-MM-DD"],
        ),
        (
            {"airport": "ESSA", "direction": "arrivals", "hour_from": 17, "hour_to": 5},
            ["hour_to (5) must be greater than hour_from (17)"],
        ),
        ({"airport": "ESSA", "direction": "arrivals", "hour_to": 25}, ["hour_to"]),
        ({"airport": "ESSA", "direction": "arrivals", "limit": 100}, ["limit"]),
    ],
)
async def test_airport_flights_rejects_bad_input(server, args, needles):
    await assert_error(server, "airport_flights", args, *needles)


# --- OpenSky not configured --------------------------------------------------------------


@pytest.mark.parametrize(
    "tool,args",
    [
        ("aircraft_near_airport", {"airport": "ESSB"}),
        ("get_aircraft", {"icao24": ["4ca7b5"]}),
        ("airport_flights", {"airport": "ESSA", "direction": "arrivals"}),
    ],
)
async def test_flight_tools_say_opensky_is_not_configured(unconfigured_server, tool, args):
    text = await assert_error(
        unconfigured_server,
        tool,
        args,
        "OpenSky is not configured",
        "get_metar",  # tells the model what still works
        "tell the user",
    )
    assert "Traceback" not in text


async def test_unconfigured_server_still_lists_all_five_tools(unconfigured_server):
    async with Client(unconfigured_server) as client:
        tools = await client.list_tools()
    assert sorted(t.name for t in tools.tools) == [
        "aircraft_near_airport",
        "airport_flights",
        "get_aircraft",
        "get_metar",
        "get_taf",
    ]


# --- rejected values are not echoed (slice 2 review R4) -----------------------------------

LONG_VALUE = "eyJhbGciOiJSUzI1NiJ9.DEMO-ONLY-pasted-text-that-must-not-come-back.c2ln"


def test_every_tool_hides_rejected_input_in_validation_errors(server):
    for tool in server._tool_manager.list_tools():
        assert tool.fn_metadata.arg_model.model_config.get("hide_input_in_errors"), tool.name


@pytest.mark.parametrize(
    "tool,args",
    [
        ("get_metar", {"stations": [LONG_VALUE]}),
        ("get_metar", {"stations": ["ESSA"], "hours_back": LONG_VALUE}),
        ("get_taf", {"stations": [LONG_VALUE]}),
        ("aircraft_near_airport", {"airport": LONG_VALUE}),
        ("get_aircraft", {"icao24": [LONG_VALUE]}),
        ("airport_flights", {"airport": "ESSA", "direction": "arrivals", "date_utc": LONG_VALUE}),
        ("airport_flights", {"airport": "ESSA", "direction": LONG_VALUE}),
    ],
)
async def test_long_rejected_values_are_not_echoed(server, tool, args):
    text = await assert_error(server, tool, args)
    assert "input_value" not in text
    assert "DEMO-ONLY-pasted" not in text and "eyJhbGci" not in text
