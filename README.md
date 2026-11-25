# aviation-mcp

A small, read-only [MCP](https://modelcontextprotocol.io) server built on the official
Python SDK (`mcp==2.3.0`, `MCPServer`). It gives a model two things:

- **Airport weather:** METAR and TAF from [aviationweather.gov](https://aviationweather.gov/data/api).
  No auth needed.
- **Flight data:** live aircraft and past arrivals/departures from the
  [OpenSky Network](https://opensky-network.org) REST API, using OAuth2 client credentials.

It runs over **stdio** (the default) or over **Streamable HTTP** as an OAuth resource
server. Over HTTP it can also read the signed-in pilot's own saved airports and logbook
from a separate API, on behalf of that user, through OAuth 2.0 Token Exchange (RFC 8693).
`make demo-obo` shows this end to end against Keycloak in Docker.

It is a personal, non-commercial learning project. It is not a product and is not deployed
anywhere. The code was written by a coding agent (Claude) from the designs in
[`docs/DESIGN.md`](docs/DESIGN.md) (slice 1) and [`docs/OBO.md`](docs/OBO.md) (slice 2).
Slice 1 was then reviewed by a model from another vendor (GPT); slice 2 gets the same kind
of review.
The review findings and their fixes are in [`docs/DECISIONS.md`](docs/DECISIONS.md), and
the eval prompts and slice plan in [`docs/PLAN.md`](docs/PLAN.md).

## Tools

| Tool | What it answers | Parameters | Upstream | Scopes (HTTP) |
|---|---|---|---|---|
| `get_metar` | Current observed weather at 1–5 airports | `stations` (1–5 ICAO codes), `hours_back` 0–12 (default 0 = latest only) | aviationweather.gov | `mcp:tools` |
| `get_taf` | Forecast ("will it…", "tomorrow") for 1–3 airports | `stations` (1–3 ICAO codes) | aviationweather.gov | `mcp:tools` |
| `aircraft_near_airport` | Live aircraft near an airport, nearest first | `airport` (ICAO), `radius_km` 5–150 (40), `limit` 1–50 (20) | stationinfo + OpenSky `/states/all` | `mcp:tools` |
| `get_aircraft` | Current position of specific aircraft | `icao24` (1–5 hex addresses, e.g. `4ca7b5`) | OpenSky `/states/all` | `mcp:tools` |
| `airport_flights` | Arrivals or departures on a **past** UTC day | `airport`, `direction` (`arrivals`/`departures`), `date_utc` (yesterday back to 7 days; default yesterday), `hour_from`/`hour_to` (UTC), `limit` 1–50 (25) | OpenSky `/flights/arrival`, `/flights/departure` | `mcp:tools` |
| `my_saved_airports` | The signed-in pilot's saved airports, at most 50 (HTTP only) | none | logbook API `GET /me/airports` with an exchanged token | `mcp:tools mcp:logbook` |
| `my_recent_flights` | The signed-in pilot's own logbook, newest first (HTTP only) | `limit` 1–50 (10), `since_utc` (YYYY-MM-DD) | logbook API `GET /me/flights` with an exchanged token | `mcp:tools mcp:logbook` |

Over stdio there is no sign-in: only the first five tools exist and the scopes column does
not apply. Over HTTP every call needs a bearer token with `mcp:tools`. The two `my_*` tools
exist only over HTTP with `--logbook-url`, and also need `mcp:logbook`. `tools/list` is not
filtered by scope. Calling a `my_*` tool without `mcp:logbook` gets HTTP 403
`insufficient_scope` with `scope="mcp:tools mcp:logbook"`, so the client can ask the user
for more access. This holds whether the client sends `Mcp-Name` plain or in its
`=?base64?…?=` form. The `my_*` results carry no token or user id; labels are the user's own
text and are returned as they are.

All tools:

- are read-only (`readOnlyHint: true`);
- return `structuredContent` with an output schema, plus the same JSON as text;
- put units in field names (`temp_c`, `wind_kt`, `altitude_m`, `speed_ms`, …);
- cap every list and say so (`count_total`, `returned`, `truncated`); `my_saved_airports`
  has a fixed cap of 50 and no paging;
- return bad input or upstream failures as tool errors (`isError: true`), with a message that
  tells the model how to fix the call. For example, `'ARN' is not a 4-letter ICAO airport
  code … use ESSA`, or "today is not available yet, use date_utc=… or earlier". A rejected
  value is repeated only if it is short and plain (up to 8 letters, digits or `-`, like
  `'ARN'`); anything else, and pydantic's `input_value`, is left out, so text a caller
  pastes into an argument (a bearer token, say) does not come back into the model's
  context.

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
make demo-obo               # on-behalf-of demo against Keycloak in Docker (see below)
uv run aviation-mcp         # run the stdio server (an MCP client normally starts it)
```

By default the server speaks MCP over **stdio**. Logs go to **stderr** as JSON lines,
because stdout carries the protocol.

### HTTP mode (OAuth resource server)

```sh
uv run aviation-mcp --transport http \
    --issuer http://localhost:8180/realms/aviation-demo \
    --resource-url http://localhost:8000/mcp \
    --logbook-url http://localhost:8001          # optional: enables the two my_* tools
```

- Listens on `127.0.0.1` only (`--port`, default 8000), Streamable HTTP at the path of
  `--resource-url`, stateless, JSON responses. `GET /healthz` needs no token.
- `--issuer` and `--resource-url` are required: there is no unauthenticated HTTP mode. At
  start-up the server reads the issuer's OIDC discovery document and refuses to start if
  its `issuer` differs from `--issuer`.
- Every request needs `Authorization: Bearer <JWT>`. The server checks the RS256 signature
  against the issuer's JWKS, `iss`, that `aud` contains `--resource-url`, `exp`/`nbf`
  (10 s leeway) and `sub`. Any failure is HTTP 401 with a `WWW-Authenticate` header that
  points to the Protected Resource Metadata at `/.well-known/oauth-protected-resource/mcp`.
- With `--logbook-url`, the server is also a confidential OAuth client of the issuer, for
  token exchange. Its secret is read like the OpenSky one: `AVIATION_MCP_CLIENT_SECRET`
  (and optionally `AVIATION_MCP_CLIENT_ID`, default `aviation-mcp`) in the environment, or
  `~/.config/aviation-mcp/oauth-client.env` with mode 0600 (`--client-credentials-file`).
- The caller's token is never forwarded. For a `my_*` call the server exchanges it at the
  issuer for a token with `aud=logbook-api` and `scope=logbook:read`, caches that per user
  (memory only, until 30 s before it or the caller's token expires) and calls the API with
  it.

### On-behalf-of demo: `make demo-obo`

Needs Docker. It starts Keycloak 26.8.2 (digest-pinned) from `demo/compose.yaml` on
`127.0.0.1:8180` with the realm in `demo/keycloak/aviation-demo-realm.json`, then runs
`demo/obo_demo.py`. That script starts the demo logbook API (`demo/logbook_api.py`, port
8001) and the server in HTTP mode (port 8000), signs two demo users in with authorization
code + PKCE, and prints PASS/FAIL with evidence for each check:

0. no token: 401 `invalid_token` with `resource_metadata`;
1. pilot A gets A's airports and flights through the MCP SDK client;
2. pilot B gets B's;
3. B signed in with only `mcp:tools`: 403 `insufficient_scope`, `scope="mcp:tools mcp:logbook"`;
4. a token issued to another app (wrong audience): 401;
5. the logbook API received a token with `aud=logbook-api`, `azp=aviation-mcp`,
   `scope=logbook:read`, whose `jti` and SHA-256 differ from A's inbound token (tokens are
   shown only as 12-digit hash prefixes, never whole);
6. A's inbound token sent straight to the logbook API: 401;
7. no inbound token or client secret in the server and API logs.

It exits non-zero if a check fails and always tears the Compose stack down (`docker
compose down --volumes`), also on failure or Ctrl-C.

> **Demo-only values.** The realm import, `demo/compose.yaml` and `demo/obo_demo.py`
> contain throwaway users (`pilot-a`, `pilot-b`), their passwords, the Keycloak admin
> password and the `aviation-mcp` client secret. All of them start with `DEMO-ONLY-`. They
> protect nothing, exist only on loopback while the demo runs, and must never be reused.

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
  server.py   the tools (the model-facing API), validation, error mapping, entry point
  http_app.py Streamable HTTP app: SDK bearer auth + ToolScopeGate (per-tool 403), /healthz, OIDC discovery
  jwt_verify.py  JwtVerifier (the SDK's TokenVerifier): RS256, JWKS by kid, iss/aud/exp/sub
  exchange.py TokenExchanger: RFC 8693 exchange, per-user cache keyed (iss, sub, audience, scope)
  logbook.py  logbook API client: exchanged token only, one re-exchange on 401
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
  for "no weather" or "no flights". The same holds for the logbook API: a 200 without an
  `airports`/`flights` list is an error, never "no records"; a real `[]` is an empty
  result, and single rows that fail validation are dropped. `get_aircraft` keeps only the
  requested addresses, once each, whatever the upstream returns.
- **Date line and poles:** `aircraft_near_airport` uses one OpenSky box. If the area around
  the airport would cross the 180° meridian or reach a pole, it refuses with an error that
  names the largest `radius_km` that works, instead of clipping the area and reporting a
  partial result as complete. Splitting into two queries is not implemented.
- **Secrets:** nothing logs headers, bodies or the token request. A redaction filter masks
  `Authorization`, bearer tokens, `access_token`, `subject_token`, `client_secret` and any
  three-part JWT as a second line of defence. `tests/test_no_secret_in_logs.py` runs a full
  OpenSky token flow at DEBUG. `tests/test_no_token_in_logs_http.py` runs a full
  on-behalf-of flow over HTTP (two users, cache hits, downstream 401, refused exchange,
  wrong audience, missing scope) at DEBUG with redaction switched off. Both fail if a
  token, the client secret or (over HTTP) a raw user id appears in any log line or result.
  `tests/test_http_hardening.py` passes a bearer token as an argument to every tool, with
  normal redaction on, and fails if any 12-character piece of it comes back in the error.
- **Users in logs:** tool-call lines name the user only as `p_` + HMAC-SHA256(per-process
  random key, iss|sub)[:12], which cannot be joined to the identity provider's user id or
  across restarts.

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
point as a stdio subprocess. The HTTP tests drive the real Starlette app through
`httpx.ASGITransport` with RSA keys generated per run; the JWKS, the token endpoint and the
logbook API are served by respx. `make test` needs neither Docker nor a network.

`evals/tool_selection.yaml` holds the ten tool-selection prompts from the plan. For now, a
test only checks that each expected call is valid against the current schemas. Scoring the
prompts against a live model is a later step.

## Not implemented

What `make demo-obo` does **not** show:

- **Production identity.** Everything runs on localhost over plain HTTP. The MCP spec
  requires HTTPS for authorization-server endpoints; the demo uses no TLS anywhere.
- **Real users or secrets.** Users, passwords and the client secret are throwaway demo
  values. Only Keycloak is configured; Auth0, Entra ID and others are untested.
- **Client registration and consent.** Clients are pre-registered in the realm import.
  There is no dynamic client registration, no Client ID Metadata Document and no consent
  screen. The demo signs users in by posting Keycloak's login form from a script, not from
  a real MCP client or browser. That script sends PKCE S256 and `resource` on both the
  authorization and the token request, but it is not a conformant MCP OAuth client: it
  does no protected-resource or authorization-server discovery, does not check that the
  server advertises S256, and does not validate `state` or `iss` on the callback (it would
  accept wrong ones).
- **Token lifetime management.** There are no refresh tokens (the client signs in again)
  and no early revocation: a cached exchanged token lives at most until the caller's token
  expires (5 minutes in the demo realm).
- **Delegation.** The exchanged token impersonates the user (same `sub`, no `act` claim).
  The API sees `azp=aviation-mcp`, but no delegation chain.
- **Per-user identity at OpenSky.** The five public tools stay machine to machine: every
  user shares one OpenSky identity and one credit budget.
- Also not included: DPoP or mTLS-bound tokens, any deployment beyond localhost, the
  Dockerfile and CI (slice 3), MCP resources and prompts, OpenSky tracks, callsign search,
  owner or registration lookup, SIGMET/PIREP, OpenTelemetry export, and pyright in the
  local checks.

## Data and terms

The server sends ICAO codes to aviationweather.gov. To OpenSky it sends bounding boxes,
`icao24` addresses, airport codes and a token request with the API client id. Over stdio it handles no personal data about users.
Over HTTP it handles the user id (`sub`) and the user's own logbook rows in memory while a
call runs, and logs only a per-process pseudonym. OpenSky data is used under its
[terms of use](https://opensky-network.org/about/terms-of-use) as a non-profit learning
project. Real OpenSky responses are not stored in this repo.

OpenSky asks that work using its data cites: Matthias Schäfer, Martin Strohmeier, Vincent
Lenders, Ivan Martinovic and Matthias Wilhelm. "Bringing Up OpenSky: A Large-scale ADS-B
Sensor Network for Research". In Proceedings of the 13th IEEE/ACM International Symposium
on Information Processing in Sensor Networks (IPSN), pages 83-94, April 2014.

## Licence

MIT, see [`LICENSE`](LICENSE). The licence covers this code only, not data from
aviationweather.gov or OpenSky.
