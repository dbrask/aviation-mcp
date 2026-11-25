"""Tool result models. Field names carry their units; these become the tools' outputSchema."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Cloud(BaseModel):
    cover: str
    base_ft: int | None = None


class MetarReport(BaseModel):
    station: str
    observed_utc: str
    raw: str
    flight_category: str | None = None
    temp_c: float | None = None
    dewpoint_c: float | None = None
    wind_dir_deg: int | Literal["VRB"] | None = None
    wind_kt: int | None = None
    gust_kt: int | None = None
    visibility_sm: str | None = Field(
        None, description="Statute miles. A trailing '+' means 'or more', e.g. '6+'."
    )
    altimeter_hpa: float | None = None
    clouds: list[Cloud] = []


class MetarResult(BaseModel):
    reports: list[MetarReport]
    missing: list[str] = Field(description="Requested stations with no report.")
    truncated: bool = False
    source: str = "aviationweather.gov"


class TafForecast(BaseModel):
    station: str
    issued_utc: str | None = None
    valid_from_utc: str | None = None
    valid_to_utc: str | None = None
    raw: str


class TafResult(BaseModel):
    forecasts: list[TafForecast]
    missing: list[str]
    source: str = "aviationweather.gov"


class Aircraft(BaseModel):
    icao24: str
    callsign: str | None = None
    distance_km: float | None = Field(None, description="From the airport; null in get_aircraft.")
    latitude: float | None = None
    longitude: float | None = None
    altitude_m: float | None = Field(None, description="Barometric altitude, metres.")
    speed_ms: float | None = Field(None, description="Ground speed, metres per second.")
    track_deg: float | None = None
    vertical_rate_ms: float | None = None
    on_ground: bool | None = None
    last_contact_utc: str | None = None


class StatesResult(BaseModel):
    time_utc: str | None = None
    airport: str | None = None
    count_total: int
    returned: int
    truncated: bool
    aircraft: list[Aircraft]
    not_found: list[str] = Field(
        default_factory=list, description="Requested icao24 addresses with no current position."
    )
    credits_remaining: int | None = None
    source: str = "OpenSky Network"


class Flight(BaseModel):
    icao24: str
    callsign: str | None = None
    first_seen_utc: str | None = None
    last_seen_utc: str | None = None
    from_airport: str | None = None
    to_airport: str | None = None


class FlightsResult(BaseModel):
    airport: str
    direction: Literal["arrivals", "departures"]
    date_utc: str
    hour_from: int
    hour_to: int
    count_total: int
    returned: int
    truncated: bool
    flights: list[Flight]
    credits_remaining: int | None = None
    source: str = "OpenSky Network"
