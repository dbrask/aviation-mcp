# aviation-mcp: design

Status: slice 1 is implemented as `0.1.0`. Where the build differs from this design is listed in `DECISIONS.md`. The eval prompts, Hermes wiring and slice plan are in `PLAN.md`.

A small MCP server, written with the official Python SDK, that gives a model airport weather (METAR/TAF from aviationweather.gov, no auth) and flight data (OpenSky Network, OAuth2 client credentials). It is read-only and makes no writes anywhere. API facts carry a numbered source. Anything without one is a design decision.

## 0. Verdict on the upstream APIs

**OpenSky is usable for this, within limits.** It accepts only the OAuth2 client credentials flow: create an API client on the account page, then exchange `client_id`/`client_secret` for a Bearer token. Tokens expire after 30 minutes, and a 401 means the token has expired.[1] The terms allow use "solely for the purpose of non-profit research and non-profit education". A written licence is needed for any for-profit entity and for "operational use of the REST API in any live product, service, or automated system", even an internal one.[2] A personal learning project fits. An always-on service, a client deployment or a commercial demo does not. So in this design, OpenSky tools run only on demand in chat and stay off the always-on voice path, and slice 2 binds to localhost. OpenSky test fixtures are synthetic, because the terms forbid passing the data on and require anonymised aircraft identifiers in anything disclosed.[2] If this is not acceptable, the fallback is the Lufthansa Open API, which also uses `grant_type=client_credentials`.[11] Its terms were not reviewed.

**aviationweather.gov** needs no auth. It asks for a custom User-Agent, at most 100 requests per minute, and returns at most 400 entries for most endpoints. It answers 204 for a valid request with no data and rejects undocumented parameters.[3] `/api/data/metar` takes `ids` (comma-separated), `format=json` and `hours`. `/api/data/taf` takes `ids` and `format`. METAR units are °C, knots, statute miles (`10+` means over 10) and hPa. `/api/data/stationinfo` returns `lat`/`lon` for an ICAO id.[4]

## 1. Tool and schema design (5 tools)

Five principles shape the tools:
- Parameters are things a model already knows (ICAO codes, dates), not things it would have to compute (bounding boxes, Unix times).
- Units go in the field names.
- Every list is capped, and every result says when it was cut.
- Every error tells the model how to fix the call.
- Every tool returns `structuredContent` plus the same JSON as text, as the spec recommends.[7]

Input errors and upstream failures come back as tool execution errors (`isError: true`) with an actionable message, not as protocol errors, so the model can correct itself.[7]

```python
ICAO = Annotated[str, Field(pattern=r"^[A-Za-z]{4}$")]          # upper-cased by the server
ICAO24 = Annotated[str, Field(pattern=r"^[0-9a-fA-F]{6}$")]     # lower-cased by the server

def get_metar(stations: Annotated[list[ICAO], Field(min_length=1, max_length=5)],
              hours_back: Annotated[int, Field(ge=0, le=12)] = 0) -> MetarResult:
    """Current observed weather (METAR) at 1-5 airports, by 4-letter ICAO code
    (ESSA = Stockholm Arlanda, not the IATA code ARN). hours_back=0 returns only the
    latest report per airport. Units: temp_c, wind_kt, gust_kt, visibility_sm
    (statute miles, '10+' = over 10), altimeter_hpa. raw is the original METAR text."""
# -> {"reports": [{"station","observed_utc","raw","flight_category","temp_c","dewpoint_c",
#     "wind_dir_deg"|"VRB","wind_kt","gust_kt","visibility_sm","altimeter_hpa",
#     "clouds":[{"cover","base_ft"}]}], "missing": ["XXXX"], "truncated": false, "source": "aviationweather.gov"}

def get_taf(stations: Annotated[list[ICAO], Field(min_length=1, max_length=3)]) -> TafResult:
    """Terminal aerodrome forecast (TAF) for 1-3 airports by ICAO code. Use for
    'will it...', 'tomorrow', 'later today'. Returns the raw TAF plus validity window (UTC)."""
# -> {"forecasts": [{"station","issued_utc","valid_from_utc","valid_to_utc","raw"}], "missing": [...]}

def aircraft_near_airport(airport: ICAO,
                          radius_km: Annotated[int, Field(ge=5, le=150)] = 40,
                          limit: Annotated[int, Field(ge=1, le=50)] = 20) -> StatesResult:
    """Live aircraft currently near an airport (OpenSky, ADS-B). Sorted by distance.
    Units: altitude_m (barometric), speed_ms (ground speed), vertical_rate_ms, track_deg."""
# -> {"time_utc", "airport", "count_total", "returned", "truncated": bool,
#     "aircraft": [{"icao24","callsign","distance_km","altitude_m","speed_ms","track_deg",
#                   "vertical_rate_ms","on_ground"}], "credits_remaining": int|null}

def get_aircraft(icao24: Annotated[list[ICAO24], Field(min_length=1, max_length=5)]) -> StatesResult:
    """Current position of specific aircraft by 24-bit transponder address (hex, e.g. 4ca7b5),
    typically taken from an aircraft_near_airport result. Not a callsign or a registration."""

def airport_flights(airport: ICAO,
                    direction: Literal["arrivals", "departures"],
                    date_utc: date | None = None,             # default: yesterday; today-7 <= d <= yesterday
                    hour_from: Annotated[int, Field(ge=0, le=23)] = 0,
                    hour_to: Annotated[int, Field(ge=1, le=24)] = 24,
                    limit: Annotated[int, Field(ge=1, le=50)] = 25) -> FlightsResult:
    """Arrivals or departures at an airport on a PAST UTC day (OpenSky). Data for today is
    not available yet: OpenSky processes flights overnight. Times are UTC."""
# -> {"airport","direction","date_utc","count_total","returned","truncated",
#     "flights":[{"icao24","callsign","first_seen_utc","last_seen_utc","from_airport","to_airport"}]}
```

Why each tool is shaped this way:
- `get_metar`/`get_taf` are split because "now" and "later" are different questions, and the model picks better from two clear descriptions than from one tool with a `kind` flag. The list caps (5 and 3) keep a result under about 2 KB.
- `aircraft_near_airport` takes an airport and a radius, not a raw bounding box. Models do coordinate arithmetic badly. The server resolves the airport with stationinfo[4] and builds the box itself. A 150 km radius is about 2.7° × 5.4° at 60°N, roughly 15 sq°, and stays under 25 sq° up to about 70°N, so a call costs 1 credit (the 1-credit tier ends at 25 sq°).[1]
- `get_aircraft` is the follow-up tool: it chains on `icao24` from the previous result. OpenSky filters by `icao24` and not by callsign,[1] so the description says so plainly.
- `airport_flights` merges arrivals and departures behind an enum and refuses "today". OpenSky fills these endpoints by a nightly batch, so "only arrivals from the previous day or earlier are available".[1] One call stays within one UTC day.

Left out, and why:
- Tracks: OpenSky calls the endpoint "purely experimental" and "can be out of order at any time".[1]
- `/flights/all` (global, and bounded to two hours[1]): too much data for a context window.
- Callsign search: there is no upstream filter for it.
- Owner or registration lookup: aircraft identifiers can point to private owners (see §4).
- SIGMET/PIREP: worth a sixth tool later, but out of scope here.
- MCP resources and prompts: tools are enough to show the core pattern.

## 2. MCP SDK

- Pin `mcp==2.3.0` (current release, Oct 2026, Python ≥3.10)[5] in `pyproject.toml`, with a `uv.lock`.
- v2 renamed `FastMCP` to `MCPServer` (`from mcp.server import MCPServer`). Input schemas still come from type hints.[6] Pinning matters here: v1 code breaks on its first import under v2.[6]
- v2 speaks the 2026-07-28 protocol revision.[6]
- Transport for slice 1 is stdio. Logs must go to stderr, because stdout carries the protocol.
- Streamable HTTP is slice 2 (`--transport streamable-http`, endpoint `/mcp`).[5]
- Tests drive the server in memory: v2's `Client` accepts the server object directly, with no transport.[6]

## 3. API integration

- **Tokens.** A `TokenManager` holds the token in memory only. It refreshes 60 s before `expires_in` runs out (OpenSky's own example uses 30 s[1]) and uses an `asyncio.Lock`, so concurrent tool calls trigger one refresh, not N. On a 401 it drops the token, refreshes and retries once. A second 401 returns `isError: "OpenSky rejected the API client credentials"`, with no detail about the secret.
- **Credits.** OpenSky counts credits in three independent buckets (`/states`, `/flights`, `/tracks`). A standard user gets 4,000 per bucket per day.[1] A `/states/all` call costs 1–4 credits depending on box area. `/flights/*` costs 4 credits for live or under 24 h and 30 credits for 1–2 day partitions.[1] The server reads `X-Rate-Limit-Remaining` into `credits_remaining`, and stops calling a bucket by itself when it drops below a floor (default 200). On a 429 it reads `X-Rate-Limit-Retry-After-Seconds`.[1] If the wait is 10 s or less it sleeps and retries once. Otherwise it returns an error that states the wait, and remembers the wait for that bucket so later calls fail at once without spending a request. It does not block a chat for hours.
- **aviationweather.gov.** Requests are paced at one per second (burst 1, retries included), so the server stays at 60 requests per minute in any window, below the published 100.[3] 502, 504 and 429 responses get exponential backoff with jitter (0.5 s, then 1 s, at most 2 retries).[3] A 204 becomes `missing: [...]`.[3]
- **Caching.** Results are cached with a TTL: METAR 5 min, TAF 15 min, states 10 s, flights 6 h, stationinfo 24 h. Repeated questions cost no credits.
- **Timeouts.** httpx connect 5 s, read 10 s, and a 20 s deadline for each tool call.
- **Bounds.** The radius limit bounds the box. Lists are sorted, cut to `limit`, and carry `count_total` and `truncated`. Paging is not exposed. With these caps, one page covers a voice or chat answer, and a cursor would only push the model into loops.

## 4. Security and data

- **Secrets.** Credentials never enter the repo. The server reads `OPENSKY_CLIENT_ID`/`OPENSKY_CLIENT_SECRET` from the environment, or from `--credentials-file`, which defaults to `~/.config/aviation-mcp/opensky.env`. The server refuses that file if group or others can read it. The secret is held as `pydantic.SecretStr`, whose repr is `**********`. `.gitignore` excludes `*.env` and `credentials*`.
- **Logging.** Output is JSON lines to stderr. The httpx hooks log only method, host, path, status and latency, never headers, bodies, or anything sent to the token endpoint. A redaction filter masks `Authorization`, `access_token` and `client_secret` as a second safety net. A test runs a full token flow with a known fake secret and fails if it appears anywhere in the captured logs.
- **Scope.** All tools are read-only. The server makes GET requests to two fixed API hosts, plus one POST to the OpenSky token endpoint. No parameter is a URL, so the model cannot make the server fetch arbitrary addresses. Inputs are validated by regex or enum, and bounds are checked before any network call. The MCP spec requires servers to validate inputs and rate-limit.[7] Free-text METAR remarks come back as data fields, never as instructions.
- **Data flow, in plain words.** The server sends ICAO codes and bounding boxes to aviationweather.gov, a US government service in the US,[unverified] and to OpenSky, a Swiss non-profit association bound by Swiss law.[2] It also sends a token request with the account holder's API client id. Aircraft positions are public ADS-B broadcasts. The server handles no personal data about users, so there is no GDPR processing to document for the service itself. Three real points remain:
  - OpenSky stores the account holder's data under Swiss privacy law.[2]
  - Tool results go on to whichever LLM provider Hermes uses. That, not this server, is where data residency matters.
  - A private aircraft's `icao24` can identify its owner. OpenSky's terms ask users to respect that privacy,[2] which is why the server offers no owner lookup.

## 5. Agentic concepts: M2M now, OBO as design reasoning (not implemented)

This server is **machine-to-machine**. It calls OpenSky as itself, using client credentials, and every user of the server shares that one identity and one credit budget. Over stdio, the MCP spec says to take credentials from the environment and not to use its HTTP authorization flow.[8]

*Design reasoning only:* suppose a tool acted for a signed-in user, for example a crew member's roster or an airline's own flight plans. Then four things change:
1. **Inbound auth.** Over Streamable HTTP, the MCP server becomes an OAuth 2.1 resource server. It publishes Protected Resource Metadata (RFC 9728), and clients find the authorization server through it, using PKCE and the `resource` parameter (RFC 8707).[8][9] The server must accept only tokens whose audience is the server itself.[9]
2. **No passthrough, use exchange.** The server "MUST NOT pass through the token it received from the MCP client". Upstream calls use a separate token.[9] The standard way to get one is OAuth 2.0 Token Exchange (RFC 8693): the server sends the user's token as `subject_token` and receives a token for the upstream API with a narrower scope. That is the on-behalf-of pattern.[10] Microsoft Entra's "OBO flow" is a vendor version of the same idea.[unverified]
3. **Per-user scopes.** `tools/list` may differ by the authorization presented, so a user without `flights:read` never sees that tool.[7] A missing scope at call time returns 403 `insufficient_scope` with the required scope in `WWW-Authenticate`, and the client can then ask the user for more access (step-up).[8] Client-credentials clients may step up or simply abort.[8]
4. **New obligations.** Token caches are keyed by user and scope, never shared. Audit logs record a pseudonymous subject, so personal data now exists: GDPR purpose, retention and residency become real work. The accountable party for each call becomes the user, not the service.

## 6. Deployment and operations

- **Versioning.** SemVer, with a `CHANGELOG.md` in Keep a Changelog format. Tool names and schemas are the public API for models, so renaming a tool or tightening a parameter counts as a breaking change. `server_info.version` comes from the package version.
- **Tests (offline).** pytest with `pytest-socket` blocks the network. respx replays fixtures: AWC fixtures are recorded real responses (public, no auth), and OpenSky fixtures are synthetic but follow the documented schema, with fake `icao24` values (see §0). Cases: token manager on a fake clock (refresh margin, one refresh under concurrency, 401 then one retry), 429 with short and long Retry-After, 204 giving `missing`, truncation, validation errors giving `isError`, the log-leak test, and all five tools through the in-memory `Client`.[6]
- **Tool-selection eval.** The ten prompts in `PLAN.md` become `evals/tool_selection.yaml` in slice 1, and are later run against a live model.
- **Container (slice 2).** Multi-stage `python:3.12-slim` build with `uv sync --frozen`. The process runs as a non-root user on a read-only root filesystem. `docker run -i` works for stdio. HTTP binds 127.0.0.1:8000 `/mcp`, with a `/healthz` route that checks liveness and never calls upstream. Docker `HEALTHCHECK` hits that route.
- **Logs and metrics.** Structured logs carry `tool`, `latency_ms`, `upstream_status`, `cache_hit` and `credits_remaining`. The SDK's built-in OpenTelemetry middleware gives each request a span, at no cost until an exporter is configured.[6]
- **CI.** `.github/workflows/ci.yml` runs ruff, pyright, the offline pytest suite and `docker build`. The same steps exist locally as `make ci`. There is no remote, so locally is where CI runs for now.

## Sources

[1] https://openskynetwork.github.io/opensky-api/rest.html — OpenSky REST API docs
[2] https://opensky-network.org/about/terms-of-use — OpenSky Terms of Use
[3] https://aviationweather.gov/data/api — AviationWeather.gov Data API
[4] https://aviationweather.gov/data/schema/openapi.yaml — AviationWeather.gov OpenAPI spec
[5] https://pypi.org/project/mcp — mcp on PyPI (2.3.0)
[6] https://py.sdk.modelcontextprotocol.io/whats-new — MCP Python SDK: What's new in v2
[7] https://modelcontextprotocol.io/specification/2026-07-28/server/tools — MCP spec 2026-07-28: Tools
[8] https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization — MCP spec 2026-07-28: Authorization
[9] https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/security-considerations — MCP spec: Authorization security considerations
[10] https://www.rfc-editor.org/rfc/rfc8693 — RFC 8693 OAuth 2.0 Token Exchange
[11] https://developer.lufthansa.com/docs/read/api_basics/Getting_Started — Lufthansa Open API: Getting an access token
