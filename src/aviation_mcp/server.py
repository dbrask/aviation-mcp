"""The MCP server: five read-only tools over aviationweather.gov and OpenSky.

Tool names, parameters and docstrings are the public API a model sees. Inputs are
validated by type hints (regex, bounds, enums) before any network call; every error
comes back as a tool execution error (isError) that says how to fix the call.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
import time
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import AfterValidator, BeforeValidator, Field

from aviation_mcp import __version__, results
from aviation_mcp.auth import OpenSkyAuthError, TokenManager
from aviation_mcp.awc import AwcClient, AwcError
from aviation_mcp.config import DEFAULT_CREDENTIALS_FILE, CredentialsError, load_opensky_credentials
from aviation_mcp.logs import configure_logging, http_event_hooks
from aviation_mcp.models import FlightsResult, MetarResult, StatesResult, TafResult
from aviation_mcp.opensky import OpenSkyClient, OpenSkyError

TOOL_DEADLINE_S = 20.0
FLIGHTS_MAX_DAYS_BACK = 7
MIN_RADIUS_KM = 5
log = logging.getLogger("aviation_mcp.tools")

NOT_CONFIGURED = (
    "OpenSky is not configured on this server, so flight data (aircraft_near_airport, "
    "get_aircraft, airport_flights) is unavailable. Do not retry this call. Weather still "
    "works: use get_metar or get_taf. If the user asked for flight data, tell the user that "
    "the server owner must add OpenSky API client credentials (OPENSKY_CLIENT_ID and "
    "OPENSKY_CLIENT_SECRET, or --credentials-file)."
)


# --- argument types ----------------------------------------------------------------------


def _icao(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z]{4}", value):
        raise ValueError(
            f"{value!r} is not a 4-letter ICAO airport code. Use the ICAO code "
            "(e.g. ESSA for Stockholm Arlanda, EGLL for London Heathrow), not the 3-letter "
            "IATA code"
        )
    return value.upper()


def _icao24(value: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        raise ValueError(
            f"{value!r} is not an icao24 transponder address. Use the 6-character hex "
            "address (e.g. 4ca7b5) from an aircraft_near_airport result; callsigns and "
            "registrations are not supported"
        )
    return value.lower()


def _date_format(value: Any) -> Any:
    if isinstance(value, str) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(
            f"date_utc must be a UTC date in YYYY-MM-DD format (e.g. 2026-11-24), got {value!r}"
        )
    return value


ICAO = Annotated[
    str,
    AfterValidator(_icao),
    Field(json_schema_extra={"pattern": "^[A-Za-z]{4}$"}, description="4-letter ICAO code"),
]
ICAO24 = Annotated[
    str,
    AfterValidator(_icao24),
    Field(json_schema_extra={"pattern": "^[0-9a-fA-F]{6}$"}, description="6-char hex address"),
]

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)


def _utc_today() -> date:
    return datetime.now(UTC).date()


# --- server ------------------------------------------------------------------------------


def build_server(
    *,
    awc: AwcClient,
    opensky: OpenSkyClient | None,
    today: Callable[[], date] = _utc_today,
) -> MCPServer:
    server = MCPServer(
        "aviation-mcp",
        version=__version__,
        instructions=(
            "Airport weather (METAR now, TAF forecast) from aviationweather.gov and flight "
            "data from the OpenSky Network. Airports are 4-letter ICAO codes. Times are UTC."
        ),
    )

    def require_opensky() -> OpenSkyClient:
        if opensky is None:
            raise ToolError(NOT_CONFIGURED)
        return opensky

    @server.tool(annotations=READ_ONLY)
    async def get_metar(
        stations: Annotated[list[ICAO], Field(min_length=1, max_length=5)],
        hours_back: Annotated[int, Field(ge=0, le=12)] = 0,
    ) -> MetarResult:
        """Current observed weather (METAR) at 1-5 airports, by 4-letter ICAO code
        (ESSA = Stockholm Arlanda, not the IATA code ARN). hours_back=0 returns only the
        latest report per airport. Units: temp_c, wind_kt, gust_kt, visibility_sm
        (statute miles, '6+' = 6 or more), altimeter_hpa. raw is the original METAR text."""

        async def run() -> MetarResult:
            rows = await awc.metar(stations, hours_back)
            reports, missing, truncated = results.metar_reports(rows, stations)
            return MetarResult(reports=reports, missing=missing, truncated=truncated)

        return await _run("get_metar", run())

    @server.tool(annotations=READ_ONLY)
    async def get_taf(
        stations: Annotated[list[ICAO], Field(min_length=1, max_length=3)],
    ) -> TafResult:
        """Terminal aerodrome forecast (TAF) for 1-3 airports by ICAO code. Use for
        'will it...', 'tomorrow', 'later today'. Returns the raw TAF plus validity window (UTC)."""

        async def run() -> TafResult:
            forecasts, missing = results.taf_forecasts(await awc.taf(stations), stations)
            return TafResult(forecasts=forecasts, missing=missing)

        return await _run("get_taf", run())

    @server.tool(annotations=READ_ONLY)
    async def aircraft_near_airport(
        airport: ICAO,
        radius_km: Annotated[int, Field(ge=MIN_RADIUS_KM, le=150)] = 40,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
    ) -> StatesResult:
        """Live aircraft currently near an airport (OpenSky, ADS-B). Sorted by distance.
        Units: altitude_m (barometric), speed_ms (ground speed), vertical_rate_ms, track_deg.
        A search area that would cross the 180° line or reach a pole is refused with an
        error naming the largest radius_km that works."""
        client = require_opensky()

        async def run() -> StatesResult:
            station = await awc.station(airport)
            if station is None or station.get("lat") is None or station.get("lon") is None:
                raise ToolError(
                    f"Airport {airport} was not found. Check that it is a valid 4-letter ICAO "
                    "code (e.g. ESSB for Stockholm Bromma)."
                )
            lat, lon = float(station["lat"]), float(station["lon"])
            try:
                box = results.bounding_box(lat, lon, radius_km)
            except results.UnsupportedArea:
                raise ToolError(_unsupported_area_message(airport, lat, lon, radius_km)) from None
            snap = await client.states_in_box(*box)
            found = results.aircraft_near(snap.states, lat, lon, radius_km)
            return StatesResult(
                time_utc=results.iso_utc(snap.time),
                airport=airport,
                count_total=len(found),
                returned=min(len(found), limit),
                truncated=len(found) > limit,
                aircraft=found[:limit],
                credits_remaining=snap.credits_remaining,
            )

        return await _run("aircraft_near_airport", run())

    @server.tool(annotations=READ_ONLY)
    async def get_aircraft(
        icao24: Annotated[list[ICAO24], Field(min_length=1, max_length=5)],
    ) -> StatesResult:
        """Current position of specific aircraft by 24-bit transponder address (hex, e.g.
        4ca7b5), typically taken from an aircraft_near_airport result. Not a callsign or a
        registration."""
        client = require_opensky()
        wanted = list(dict.fromkeys(icao24))  # de-duplicate, keep order

        async def run() -> StatesResult:
            snap = await client.states_by_icao24(wanted)
            # Do not trust the upstream filter: keep requested addresses only, once each.
            found = results.aircraft_by_icao24(snap.states, wanted)
            seen = {a.icao24 for a in found}
            return StatesResult(
                time_utc=results.iso_utc(snap.time),
                count_total=len(found),
                returned=len(found),
                truncated=False,
                aircraft=found,
                not_found=[a for a in wanted if a not in seen],
                credits_remaining=snap.credits_remaining,
            )

        return await _run("get_aircraft", run())

    @server.tool(annotations=READ_ONLY)
    async def airport_flights(
        airport: ICAO,
        direction: Literal["arrivals", "departures"],
        date_utc: Annotated[
            date | None,
            BeforeValidator(_date_format),
            Field(description="UTC day, YYYY-MM-DD. Default: yesterday. Today is not available."),
        ] = None,
        hour_from: Annotated[int, Field(ge=0, le=23)] = 0,
        hour_to: Annotated[int, Field(ge=1, le=24)] = 24,
        limit: Annotated[int, Field(ge=1, le=50)] = 25,
    ) -> FlightsResult:
        """Arrivals or departures at an airport on a PAST UTC day (OpenSky). Data for today is
        not available yet: OpenSky processes flights overnight. Times are UTC."""
        day = _check_flight_day(date_utc, today())
        if hour_to <= hour_from:
            raise ToolError(
                f"hour_to ({hour_to}) must be greater than hour_from ({hour_from}); hours are "
                "UTC, e.g. hour_from=17, hour_to=24 for the evening."
            )
        client = require_opensky()
        start = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())
        begin, end = start + hour_from * 3600, start + hour_to * 3600

        async def run() -> FlightsResult:
            rows, credits = await client.flights(airport, direction, begin, end)
            flights = results.flights_sorted(rows, direction)
            return FlightsResult(
                airport=airport,
                direction=direction,
                date_utc=day.isoformat(),
                hour_from=hour_from,
                hour_to=hour_to,
                count_total=len(flights),
                returned=min(len(flights), limit),
                truncated=len(flights) > limit,
                flights=flights[:limit],
                credits_remaining=credits,
            )

        return await _run("airport_flights", run())

    return server


def _unsupported_area_message(airport: str, lat: float, lon: float, radius_km: int) -> str:
    max_r = results.largest_supported_radius_km(lat, lon, below=radius_km, floor=MIN_RADIUS_KM)
    head = (
        f"A {radius_km} km search around {airport} would cross the 180° longitude line "
        "(date line) or reach a pole. aircraft_near_airport does not support that yet, and "
        "it will not return a partial area as if it were complete."
    )
    if max_r is not None:
        return f"{head} Retry with radius_km={max_r} or less."
    return f"{head} Do not retry; tell the user nearby aircraft cannot be listed for {airport}."


def _check_flight_day(requested: date | None, today: date) -> date:
    yesterday = today - timedelta(days=1)
    oldest = today - timedelta(days=FLIGHTS_MAX_DAYS_BACK)
    if requested is None:
        return yesterday
    if requested == today:
        raise ToolError(
            f"OpenSky flight lists for today ({today}) are not available yet: OpenSky "
            f"processes flights overnight. Use date_utc={yesterday} or earlier (back to "
            f"{oldest}), or tell the user flight lists exist only from yesterday back."
        )
    if requested > today:
        raise ToolError(
            f"date_utc {requested} is in the future. Flight lists exist only for past UTC "
            f"days: use {yesterday} or earlier (back to {oldest})."
        )
    if requested < oldest:
        raise ToolError(
            f"date_utc {requested} is more than {FLIGHTS_MAX_DAYS_BACK} days back. Use a date "
            f"from {oldest} to {yesterday}."
        )
    return requested


async def _run[T](tool: str, coro: Coroutine[Any, Any, T]) -> T:
    """Run one tool call under the 20 s deadline. Upstream failures become tool execution
    errors (isError) with a message for the model; one structured log line per call."""
    t0 = time.perf_counter()
    outcome = "ok"
    try:
        async with asyncio.timeout(TOOL_DEADLINE_S):
            return await coro
    except TimeoutError:
        outcome = "timeout"
        raise ToolError(
            f"The upstream service did not answer within {TOOL_DEADLINE_S:.0f} s. Try again "
            "later."
        ) from None
    except (AwcError, OpenSkyError, OpenSkyAuthError) as exc:
        outcome = "upstream_error"
        raise ToolError(str(exc)) from None
    except ToolError:
        outcome = "tool_error"
        raise
    finally:
        log.info(
            "tool call",
            extra={
                "tool": tool,
                "outcome": outcome,
                "latency_ms": round((time.perf_counter() - t0) * 1000),
            },
        )


# --- entry point -------------------------------------------------------------------------


@dataclass
class App:
    server: MCPServer
    http: httpx.AsyncClient
    opensky: OpenSkyClient | None

    async def aclose(self) -> None:
        await self.http.aclose()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="aviation-mcp", description="Read-only aviation MCP server (stdio)."
    )
    parser.add_argument(
        "--credentials-file",
        type=Path,
        default=DEFAULT_CREDENTIALS_FILE,
        help="OpenSky KEY=VALUE file, mode 0600 (default: %(default)s). "
        "OPENSKY_CLIENT_ID/OPENSKY_CLIENT_SECRET in the environment take precedence.",
    )
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    return parser.parse_args(argv)


def create_app(args: argparse.Namespace, env: Mapping[str, str] | None = None) -> App:
    try:
        creds = load_opensky_credentials(env=env, path=args.credentials_file)
    except CredentialsError as exc:
        print(f"aviation-mcp: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    http = httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=5.0), event_hooks=http_event_hooks()
    )
    opensky = None
    if creds is None:
        log.warning(
            "OpenSky not configured; flight tools will return a not-configured error",
            extra={"credentials_file": str(args.credentials_file)},
        )
    else:
        tokens = TokenManager(http, creds.client_id, creds.client_secret)
        opensky = OpenSkyClient(http, tokens)
    server = build_server(awc=AwcClient(http), opensky=opensky)
    return App(server=server, http=http, opensky=opensky)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    # Before MCPServer exists: the SDK's logging.basicConfig is then a no-op, and every
    # log line goes through the redacting JSON formatter to stderr (stdout is the protocol).
    configure_logging(level=args.log_level)
    app = create_app(args)

    async def serve() -> None:
        try:
            await app.server.run_stdio_async()
        finally:
            await app.aclose()

    asyncio.run(serve())
