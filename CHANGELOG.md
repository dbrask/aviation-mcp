# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Tool names and input schemas are the public
API: renaming a tool or tightening a parameter is a breaking change.

## [Unreleased]

### Changed
- `my_saved_airports` returns at most 50 airports and adds `count_total` and `truncated`
  next to `returned` (output schema change). No paging and no new argument.
- A 200 from the logbook API whose `airports`/`flights` field is missing or not a list is
  a tool error ("unexpected format … not an empty result"), no longer an empty result.

### Fixed
- The per-tool 403 `insufficient_scope` now also answers a `tools/call` whose `Mcp-Name`
  header is sent in the SDK's `=?base64?…?=` form; before, such a call got a tool error
  (still refused) instead of the step-up challenge.
- Argument validation errors no longer repeat the rejected value unless it is short and
  plain, and pydantic's `input_value` is hidden for every tool, so a token pasted into an
  argument is not echoed into the model-visible error.
- `make demo-obo` sends `resource` on the token request as well as the authorization
  request. The README and OBO.md state what the scripted sign-in does not do (discovery,
  S256 capability check, `state`/`iss` validation). DESIGN.md marks CI, `make ci` and
  OpenTelemetry as planned, not implemented.

## [0.2.0] - 2026-11-25

### Added
- Streamable HTTP transport (`--transport http`, 127.0.0.1 only, stateless, JSON
  responses) next to stdio, with `GET /healthz`. stdio is unchanged and stays the default.
- OAuth resource server over HTTP: Protected Resource Metadata, a JWT verifier (RS256,
  JWKS by `kid`, `iss`, `aud` must contain the resource URI, `exp`/`nbf` with 10 s leeway,
  `sub`), `mcp:tools` required for every call, and a per-tool 403 `insufficient_scope`
  naming `mcp:tools mcp:logbook` for the logbook tools. `--issuer` and `--resource-url`
  are required; the issuer is checked against OIDC discovery at start-up.
- Two HTTP-only tools for the signed-in pilot's own data, `my_saved_airports` and
  `my_recent_flights`. They exchange the caller's token (RFC 8693) for a token with
  `aud=logbook-api`, `scope=logbook:read`, never forward the caller's token, cache the
  exchanged token per user (keyed by issuer, subject, audience and scope; memory only,
  LRU 1,024) and re-exchange once on a downstream 401.
- `make demo-obo`: Keycloak 26.8.2 (digest-pinned) in Docker Compose with a demo realm,
  a demo logbook API, PKCE sign-in of two demo users and eight PASS/FAIL checks. All users
  and secrets are throwaway `DEMO-ONLY-` values. The stack is always torn down.
- Logs name users only by a per-process HMAC pseudonym; redaction also masks
  `subject_token` and any three-part JWT. A leak test runs the full on-behalf-of flow at
  DEBUG with redaction off.
- `pyjwt[crypto]` is a direct dependency.

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
