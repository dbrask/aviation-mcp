"""Pure functions that turn upstream JSON into the tool result models.
No I/O here, so the mapping can be read and tested on its own."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from aviation_mcp.models import (
    Aircraft,
    Cloud,
    Flight,
    MetarReport,
    TafForecast,
)

MAX_METAR_REPORTS = 30
EARTH_RADIUS_KM = 6371.0


def iso_utc(value: Any) -> str | None:
    """Unix seconds or an ISO string -> 'YYYY-MM-DDTHH:MM:SSZ'."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value, UTC)
    else:
        try:
            dt = datetime.fromisoformat(str(value)).astimezone(UTC)
        except ValueError:
            return str(value)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(value: Any) -> float | int | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _int(value: Any) -> int | None:
    n = _num(value)
    return None if n is None else round(n)


# --- METAR / TAF -------------------------------------------------------------------------


def metar_report(row: dict[str, Any]) -> MetarReport:
    wdir = row.get("wdir")
    visib = row.get("visib")
    return MetarReport(
        station=row.get("icaoId", "").upper(),
        observed_utc=iso_utc(row.get("obsTime") or row.get("reportTime")) or "",
        raw=row.get("rawOb", ""),
        flight_category=row.get("fltCat"),
        temp_c=_num(row.get("temp")),
        dewpoint_c=_num(row.get("dewp")),
        wind_dir_deg="VRB" if wdir == "VRB" else _int(wdir),
        wind_kt=_int(row.get("wspd")),
        gust_kt=_int(row.get("wgst")),
        visibility_sm=None if visib in (None, "") else str(visib),
        altimeter_hpa=_num(row.get("altim")),
        clouds=[
            Cloud(cover=c.get("cover", ""), base_ft=_int(c.get("base")))
            for c in row.get("clouds") or []
        ],
    )


def metar_reports(
    rows: list[dict[str, Any]], stations: list[str]
) -> tuple[list[MetarReport], list[str], bool]:
    """Group by station in the order asked for, newest first, capped per station."""
    per_station = max(1, MAX_METAR_REPORTS // len(stations))
    by_station: dict[str, list[MetarReport]] = {s: [] for s in stations}
    for row in rows:
        report = metar_report(row)
        if report.station in by_station:
            by_station[report.station].append(report)
    reports: list[MetarReport] = []
    truncated = False
    for station in stations:
        items = sorted(by_station[station], key=lambda r: r.observed_utc, reverse=True)
        truncated |= len(items) > per_station
        reports.extend(items[:per_station])
    missing = [s for s in stations if not by_station[s]]
    return reports, missing, truncated


def taf_forecasts(
    rows: list[dict[str, Any]], stations: list[str]
) -> tuple[list[TafForecast], list[str]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        station = row.get("icaoId", "").upper()
        if station in stations and station not in latest:
            latest[station] = row
    forecasts = [
        TafForecast(
            station=s,
            issued_utc=iso_utc(latest[s].get("issueTime")),
            valid_from_utc=iso_utc(latest[s].get("validTimeFrom")),
            valid_to_utc=iso_utc(latest[s].get("validTimeTo")),
            raw=latest[s].get("rawTAF", ""),
        )
        for s in stations
        if s in latest
    ]
    return forecasts, [s for s in stations if s not in latest]


# --- geometry ----------------------------------------------------------------------------


class UnsupportedArea(ValueError):
    """The search box would cross the 180° meridian or reach a pole. One OpenSky box cannot
    cover that, and clipping it would silently drop aircraft inside the radius."""


def bounding_box(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """(lamin, lomin, lamax, lomax) of the square around a circle. At 150 km and 60°N
    this is about 2.7° x 5.4°, which stays in OpenSky's 1-credit tier (<= 25 sq°).

    Exact on the sphere used by haversine_km, so every point within radius_km lies inside
    the box. Raises UnsupportedArea instead of clipping when the box would cross the 180°
    meridian (date line) or reach a pole."""
    dlat = math.degrees(radius_km / EARTH_RADIUS_KM)
    if dlat >= 90.0 - abs(lat):
        raise UnsupportedArea(f"{radius_km} km around ({lat}, {lon}) reaches a pole")
    # Widest longitude the circle reaches; it is at a latitude poleward of the centre.
    s = math.sin(radius_km / EARTH_RADIUS_KM) / math.cos(math.radians(lat))
    if s >= 1.0:
        raise UnsupportedArea(f"{radius_km} km around ({lat}, {lon}) reaches a pole")
    dlon = math.degrees(math.asin(s))
    if dlon > 180.0 - abs(lon):
        raise UnsupportedArea(f"{radius_km} km around ({lat}, {lon}) crosses the date line")
    return (lat - dlat, lon - dlon, lat + dlat, lon + dlon)


def largest_supported_radius_km(lat: float, lon: float, below: int, floor: int) -> int | None:
    """Largest whole radius in [floor, below) that bounding_box accepts, or None."""
    for r in range(below - 1, floor - 1, -1):
        try:
            bounding_box(lat, lon, r)
        except UnsupportedArea:
            continue
        return r
    return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


# --- OpenSky -----------------------------------------------------------------------------


def aircraft(state: list[Any], distance_km: float | None = None) -> Aircraft:
    """One OpenSky state vector (documented array layout) -> Aircraft."""

    def at(i: int) -> Any:
        return state[i] if len(state) > i else None

    callsign = at(1)
    return Aircraft(
        icao24=str(at(0)).lower(),
        callsign=callsign.strip() or None if isinstance(callsign, str) else None,
        distance_km=None if distance_km is None else round(distance_km, 2),
        latitude=_num(at(6)),
        longitude=_num(at(5)),
        altitude_m=_num(at(7)),
        speed_ms=_num(at(9)),
        track_deg=_num(at(10)),
        vertical_rate_ms=_num(at(11)),
        on_ground=at(8) if isinstance(at(8), bool) else None,
        last_contact_utc=iso_utc(at(4)),
    )


def aircraft_near(
    states: list[list[Any]], lat: float, lon: float, radius_km: float
) -> list[Aircraft]:
    """Aircraft with a position inside the radius, nearest first."""
    found = []
    for s in states:
        a_lat, a_lon = (s[6], s[5]) if len(s) > 6 else (None, None)
        if _num(a_lat) is None or _num(a_lon) is None:
            continue
        d = haversine_km(lat, lon, a_lat, a_lon)
        if d <= radius_km:
            found.append((d, s))
    found.sort(key=lambda pair: pair[0])
    return [aircraft(s, d) for d, s in found]


def aircraft_by_icao24(states: list[list[Any]], wanted: list[str]) -> list[Aircraft]:
    """The states for the requested addresses only: one per address, in the order asked
    for. Rows for other addresses and repeats are dropped, so the output never exceeds the
    request even if the upstream filter is ignored."""
    first: dict[str, Aircraft] = {}
    for s in states:
        a = aircraft(s)
        if a.icao24 in wanted and a.icao24 not in first:
            first[a.icao24] = a
    return [first[w] for w in wanted if w in first]


def flight(row: dict[str, Any]) -> Flight:
    callsign = row.get("callsign")
    return Flight(
        icao24=str(row.get("icao24", "")).lower(),
        callsign=callsign.strip() or None if isinstance(callsign, str) else None,
        first_seen_utc=iso_utc(row.get("firstSeen")),
        last_seen_utc=iso_utc(row.get("lastSeen")),
        from_airport=row.get("estDepartureAirport"),
        to_airport=row.get("estArrivalAirport"),
    )


def flights_sorted(rows: list[dict[str, Any]], direction: str) -> list[Flight]:
    """Arrivals by landing time, departures by take-off time."""
    key = "lastSeen" if direction == "arrivals" else "firstSeen"
    rows = sorted(rows, key=lambda r: r.get(key) or 0)
    return [flight(r) for r in rows]
