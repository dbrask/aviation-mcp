# aviation-mcp

A small, read-only [MCP](https://modelcontextprotocol.io) server built on the official
Python SDK (`mcp==2.3.0`, `MCPServer`). It gives a model two things:

- **Airport weather:** METAR and TAF from [aviationweather.gov](https://aviationweather.gov/data/api).
  No auth needed.
- **Flight data:** live aircraft and past arrivals/departures from the
  [OpenSky Network](https://opensky-network.org) REST API, using OAuth2 client credentials.

It is a personal, non-commercial learning project. It is not a product and is not deployed
anywhere. The code was written by a coding agent (Claude) from the design in
[`docs/DESIGN.md`](docs/DESIGN.md) and then reviewed by a model from another vendor (GPT).
The review findings and their fixes are in [`docs/DECISIONS.md`](docs/DECISIONS.md), and
the eval prompts and slice plan in [`docs/PLAN.md`](docs/PLAN.md).

## Tools

| Tool | What it answers | Parameters | Upstream |
|---|---|---|---|
| `get_metar` | Current observed weather at 1–5 airports | `stations` (1–5 ICAO codes), `hours_back` 0–12 (default 0 = latest only) | aviationweather.gov |
| `get_taf` | Forecast ("will it…", "tomorrow") for 1–3 airports | `stations` (1–3 ICAO codes) | aviationweather.gov |
| `aircraft_near_airport` | Live aircraft near an airport, nearest first | `airport` (ICAO), `radius_km` 5–150 (40), `limit` 1–50 (20) | stationinfo + OpenSky `/states/all` |
| `get_aircraft` | Current position of specific aircraft | `icao24` (1–5 hex addresses, e.g. `4ca7b5`) | OpenSky `/states/all` |
| `airport_flights` | Arrivals or departures on a **past** UTC day | `airport`, `direction` (`arrivals`/`departures`), `date_utc` (yesterday back to 7 days; default yesterday), `hour_from`/`hour_to` (UTC), `limit` 1–50 (25) | OpenSky `/flights/arrival`, `/flights/departure` |

All five tools:

- are read-only (`readOnlyHint: true`);
- return `structuredContent` with an output schema, plus the same JSON as text;
- put units in field names (`temp_c`, `wind_kt`, `altitude_m`, `speed_ms`, …);
- cap every list and say so (`count_total`, `returned`, `truncated`);
- return bad input or upstream failures as tool errors (`isError: true`), with a message that
  tells the model how to fix the call. For example, `'ARN' is not a 4-letter ICAO airport
  code … use ESSA`, or "today is not available yet, use date_utc=… or earlier".

If no OpenSky credentials are configured, the server still starts. The three flight tools
then return an error saying OpenSky is not configured: the model should not retry, can use
`get_metar`/`get_taf` instead, and should tell the user the owner must add credentials.

## Run it

Requires [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 if needed.

```sh
uv sync                     # create .venv from uv.lock
make test                   # offline test suite, network blocked
make smoke                  # start the server over stdio, list the 5 tools and their input schemas
make live-metar             # one real get_metar(ESGG) call through the running server
uv run aviation-mcp         # run the stdio server (an MCP client normally starts it)
```

The server speaks MCP over **stdio**. Logs go to **stderr** as JSON lines, because stdout
carries the protocol.

### OpenSky credentials (optional)

Create an API client on your OpenSky account page, then either export
`OPENSKY_CLIENT_ID` / `OPENSKY_CLIENT_SECRET`, or write them to a file that only you can read:

```sh
mkdir -p ~/.config/aviation-mcp
install -m 600 /dev/null ~/.config/aviation-mcp/opensky.env
$EDITOR ~/.config/aviation-mcp/opensky.env
#   OPENSKY_CLIENT_ID=...
#   OPENSKY_CLIENT_SECRET=...
```

`--credentials-file PATH` overrides the default path. The server refuses to start if the
file is readable by group or others. Environment variables take precedence over the file.

## Register it in Hermes

Add a stdio server under `mcp_servers` in the Hermes config. Hermes resolves `${VAR}`
placeholders in `args` when it connects, so the config holds only the path to the
credentials file, never the secret:

```yaml
mcp_servers:
  aviation:
    command: "uv"
    args: ["--directory", "${HOME}/repo/aviation-mcp", "run", "aviation-mcp",
           "--credentials-file", "${HOME}/.config/aviation-mcp/opensky.env"]
    tools:
      include: [get_metar, get_taf]   # an always-on profile: weather only
```

For an interactive profile, leave out `tools.include` to expose all five tools. Keep OpenSky
off any always-on assistant: its terms allow non-profit research and education, but
require a licence for operational or automated use.

## How it works

```
src/aviation_mcp/
  server.py   the five tools (the model-facing API), validation, error mapping, entry point
  results.py  pure mapping from upstream JSON to the result models (+ geometry)
  models.py   pydantic result models -> output schemas
  awc.py      aviationweather.gov client: paced to 1 request/s (60/min, no burst, retries included), backoff on 429/502/504, 204 = no data, cache
  opensky.py  OpenSky client: bearer token, one retry on 401, credit floor, 429 handling, cache
  auth.py     TokenManager: OAuth2 client credentials, in memory, refresh 60 s early, one refresh under concurrency
  config.py   credentials from env or a 0600 file; secret held as SecretStr
  logs.py     JSON-lines logger to stderr with redaction; httpx hooks log method/host/path/status/latency only
  limits.py   TTL cache and token bucket (injectable clock, so tests run on fake time)
```

- **Limits:** METAR is cached 5 min, TAF 15 min, stations 24 h, OpenSky states 10 s and
  flights 6 h. Each tool call has a 20 s deadline. httpx timeouts are 5 s connect and
  10 s read.
- **OpenSky credits:** `X-Rate-Limit-Remaining` is passed on as `credits_remaining`.
  Below 200 credits the server pauses that bucket (states or flights) for an hour. On a
  429 it sleeps and retries once if the wait is 10 s or less; otherwise it returns the
  wait as an error and remembers it: until OpenSky's wait is over, calls to that bucket
  fail at once without a request (the other bucket keeps working).
- **Upstream format:** a 200 answer that does not have the documented shape is a tool
  error ("unexpected format … not an empty result"), never cached, so it is not mistaken
  for "no weather" or "no flights". `get_aircraft` keeps only the requested addresses,
  once each, whatever the upstream returns.
- **Date line and poles:** `aircraft_near_airport` uses one OpenSky box. If the area around
  the airport would cross the 180° meridian or reach a pole, it refuses with an error that
  names the largest `radius_km` that works, instead of clipping the area and reporting a
  partial result as complete. Splitting into two queries is not implemented.
- **Secrets:** nothing logs headers, bodies or the token request. A redaction filter masks
  `Authorization`, bearer tokens, `access_token` and `client_secret` as a second line of
  defence. `tests/test_no_secret_in_logs.py` runs a full token flow (fetch, 401, refresh,
  rejected credentials) at DEBUG and fails if the secret or a token appears in any log line
  or tool result.

## Tests

`make test` runs pytest with the network blocked by
[pytest-socket](https://github.com/miketheman/pytest-socket)
(`--disable-socket --allow-unix-socket` in `pyproject.toml`).
`tests/test_network_blocked.py` checks that the block holds. All HTTP is replayed by
[respx](https://lundberg.github.io/respx/):

- AWC fixtures are recorded real responses (public data, no auth).
- OpenSky fixtures are **synthetic**, written to the documented schema with fake `icao24`
  values (see `tests/fixtures/opensky/README.md`).

The tools run through the SDK's in-memory `Client(server)`. One test starts the real entry
point as a stdio subprocess.

`evals/tool_selection.yaml` holds the ten tool-selection prompts from the plan. For now, a
test only checks that each expected call is valid against the current schemas. Scoring the
prompts against a live model is a later step.

## Not implemented

- **Inbound auth.** There is no OAuth 2.1 resource-server role, no Protected Resource
  Metadata and no audience checks. Over stdio, the MCP spec says to take credentials from
  the environment instead. See DESIGN §5.
- **On-behalf-of / token exchange (RFC 8693).** The server is machine-to-machine: every
  user shares one OpenSky identity and one credit budget. There are no per-user scopes or
  token caches. DESIGN §5 has this as design reasoning only.
- **HTTP transport.** Streamable HTTP, `/healthz`, the Dockerfile and CI are slice 2.
  Slice 1 is stdio only.
- Not included either: MCP resources and prompts, OpenSky tracks, callsign search, owner
  or registration lookup, SIGMET/PIREP, OpenTelemetry export, and pyright in the local
  checks.

## Data and terms

The server sends ICAO codes to aviationweather.gov. To OpenSky it sends bounding boxes,
`icao24` addresses, airport codes and a token request with the API client id. It handles
no personal data about users. OpenSky data is used under its
[terms of use](https://opensky-network.org/about/terms-of-use) as a non-profit learning
project. Real OpenSky responses are not stored in this repo.

OpenSky asks that work using its data cites: Matthias Schäfer, Martin Strohmeier, Vincent
Lenders, Ivan Martinovic and Matthias Wilhelm. "Bringing Up OpenSky: A Large-scale ADS-B
Sensor Network for Research". In Proceedings of the 13th IEEE/ACM International Symposium
on Information Processing in Sensor Networks (IPSN), pages 83-94, April 2014.

## Licence

MIT, see [`LICENSE`](LICENSE). The licence covers this code only, not data from
aviationweather.gov or OpenSky.
