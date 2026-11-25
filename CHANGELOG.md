# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Tool names and input schemas are the public
API: renaming a tool or tightening a parameter is a breaking change.

## [Unreleased]

### Fixed
- aviationweather.gov requests are paced at one per second with no burst (retries
  included); the old bucket allowed 110 requests in 50 s.
- Unexpected 200 bodies from aviationweather.gov or OpenSky are tool errors, not cached
  "no data". `get_aircraft` returns only the requested addresses, once each.
- A long OpenSky 429 is remembered per bucket; calls fail at once until the wait ends.
- `aircraft_near_airport` refuses areas that cross the 180° meridian or reach a pole
  instead of clipping them, and names a radius that works.
- Eval seed case 2 (Kiruna) expects ESNQ, not ESKN (Stockholm/Skavsta).

## [0.1.0] - 2026-11-25

### Added
- stdio MCP server on the official Python SDK (`mcp==2.3.0`, `MCPServer`), Python 3.12, uv.
- Five read-only tools: `get_metar`, `get_taf`, `aircraft_near_airport`, `get_aircraft`,
  `airport_flights`. Each returns structured content with an output schema, caps its lists
  with a truncation notice, and returns actionable tool errors.
- aviationweather.gov client with a 60/min rate limit, backoff on 429/502/504, 204 handling
  and a TTL cache.
- OpenSky client with an OAuth2 client-credentials `TokenManager` (in memory, refresh 60 s
  early, one refresh under concurrency, one retry on 401), a daily credit floor, 429
  handling and a TTL cache.
- Credentials from the environment or a 0600 file. With no credentials, the server starts
  and the flight tools say they are not configured.
- JSON-lines logging to stderr with redaction of tokens, secrets and `Authorization`.
- Offline test suite (network blocked by pytest-socket, respx fixtures), `make smoke`,
  `make live-metar`, and the tool-selection eval seed `evals/tool_selection.yaml`.
