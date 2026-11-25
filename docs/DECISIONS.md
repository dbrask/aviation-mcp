# Decisions

## Project decisions

1. **OpenSky account and API client:** whoever runs the server creates them. The credentials
   go in `~/.config/aviation-mcp/opensky.env` (mode 0600) or the environment, and never in
   the repo. The build was tested against synthetic fixtures only. No authenticated OpenSky
   call has been made.
2. **Published on GitHub under the MIT licence.** The OpenSky fixtures stay synthetic, the
   README carries the OpenSky citation and says this is a non-commercial learning project.
3. **OpenSky terms:** used as a personal, non-commercial learning project. OpenSky stays off
   any always-on assistant. The Lufthansa Open API is the fallback if that changes.
4. **Always-on assistants** get the weather tools only (`get_metar`, `get_taf`). The README's
   Hermes snippet shows this with `tools.include`.
5. **Slice 2 (HTTP, inbound OAuth, token exchange):** built in `0.2.0` from `OBO.md`. The
   Dockerfile and CI moved to slice 3 and are not built.
6. **Python and SDK:** Python 3.12 (`.python-version`, `requires-python >=3.12`). The SDK
   is pinned exactly to `mcp==2.3.0` and locked in `uv.lock`.
7. **Build and review:** the code was written by a coding agent (Claude) from `DESIGN.md`.
   A model from another vendor (GPT) reviewed it and required five changes (R1 to R5 below),
   then approved the fixes. Slice 2 was designed by Claude Opus (`OBO.md`) and built by
   Claude Opus (`claude-opus-5-5`) with tests written first; its cross-vendor review is
   pending.

## Where the build differs from DESIGN.md (smallest fixes)

| # | Design said | Build does | Why |
|---|---|---|---|
| 1 | SDK result fields `inputSchema`, `structuredContent`, `isError` | v2 Python objects use snake_case: `input_schema`, `structured_content`, `is_error`. On the wire they are still camelCase. | API naming in mcp 2.3.0 |
| 2 | METAR visibility `'10+'` = over 10 SM | AWC JSON actually sends `"6+"` (observed live 2026-11-25). The docstring says `'6+' = 6 or more`, and `visibility_sm` is passed through as a string. | Real data |
| 3 | Aircraft fields have no position | `latitude`, `longitude` and `last_contact_utc` are added. `get_aircraft` answers "where is it now", which needs a position. | Design gap |
| 4 | `get_aircraft` returns `StatesResult` | `StatesResult` gains `not_found` (requested addresses with no state). `airport` and `distance_km` are null for this tool. | The model needs to know which addresses failed |
| 5 | "Every list is capped" (no METAR number given) | METAR: at most 30 reports in total, split evenly per station, newest first. `truncated: true` when the cap cut reports. | Keeps `hours_back=12` × 5 stations bounded |
| 6 | Below the credit floor, "stops calling a bucket by itself" | The bucket is paused for 1 hour, then one call refreshes `X-Rate-Limit-Remaining`. The error states the remaining credits and the floor. | Duration was not specified |
| 7 | Every error says how to fix the call | Type and bound errors are the SDK's pydantic messages (e.g. `Input should be less than or equal to 150`), returned as `isError`. ICAO, icao24 and date formats use custom validators, so the message names the fix (e.g. "use ESSA, not the IATA code ARN"). The JSON schema still has the regex `pattern`. | Pydantic's default regex message does not explain the fix |
| 8 | "not configured" error text not specified | Exact text is `NOT_CONFIGURED` in `src/aviation_mcp/server.py`. It says: do not retry, use `get_metar`/`get_taf`, and tell the user the owner must add credentials. | Acceptance item 4 |
| 9 | Token errors | 400, 401 or 403 from the token endpoint gives "OpenSky rejected the API client credentials". 5xx or a network error gives "token service failed, try again later". Response bodies are never shown. | Avoids echoing upstream detail |
| 10 | `/states` 404 | Treated as an empty result, the same as `/flights/*` 404. | Defensive |
| 11 | OpenTelemetry middleware "at no cost until an exporter is configured" | Not configured or verified in slice 1. | Out of slice-1 scope |
| 12 | `httpx` default logging | The `httpx` and `httpcore` loggers are set to WARNING, because httpx logs full URLs at INFO. The server logs method, host, path, status and latency itself. | Logging rule in §4 |

## Fixes from the cross-vendor review (R1–R5)

| # | Problem | Fix |
|---|---|---|
| R1 | The AWC bucket started full (60 tokens) and refilled 1/s, so 110 requests could go out in 50 s, above AWC's 100/min. | AWC uses the same `TokenBucket` with `burst=1`: one request per second, retries and concurrent misses included. Tests measure every dispatch timestamp and check every 60 s window. The generic limiter keeps its default burst. |
| R2 | An unexpected 200 body became cached "no data" (6 h for flights). `get_aircraft` returned every upstream row. | AWC needs a list of rows with `icaoId`; OpenSky states need `{"time": int, "states": null or rows starting with icao24}`; flights need rows with `icao24`. Anything else is an error that does not echo the body and is not cached. AWC 204, flights 404, `[]` and `states: null` still mean no data. `get_aircraft` keeps only requested addresses, once each, in request order. |
| R3 | A long 429 was not remembered; the next call spent another request. | A valid `X-Rate-Limit-Retry-After-Seconds` (finite, >= 0) on a final 429 sets a per-bucket cooldown on the monotonic clock. Uncached calls fail at once until it ends. `X-Rate-Limit-Remaining` on a 429 is recorded; with no valid Retry-After, the credit floor pause applies. The one short-wait retry (<= 10 s) is unchanged. |
| R4 | The box was clamped at ±180°, so a search near the date line silently missed aircraft on the other side. | `aircraft_near_airport` refuses areas that cross the 180° meridian or reach a pole, before any OpenSky request, and names the largest `radius_km` that works. No two-box query. The box is now exact on the haversine sphere (it used 111.32 km/°, slightly smaller than the radius measured by `haversine_km`). |
| R5 | Eval case 2 asked about Kiruna but expected ESKN (Stockholm/Skavsta). | ESNQ (Kiruna) in `evals/tool_selection.yaml` and `docs/PLAN.md`. A test pins each airport name in the prompts to its ICAO code and checks the PLAN table agrees. |

## Found while testing

- **Redaction bug, fixed:** the first version redacted only the finished JSON line. Inside
  it, escaped quotes (`\"access_token\": \"...\"`) hid the `key: value` pattern, so a
  token logged as JSON text passed through. Values are now redacted before JSON encoding,
  and the finished line is redacted again. `tests/test_no_secret_in_logs.py` covers it.
- **Leak test checked by mutation:** adding a `log.debug("got token %s ...")` line to
  `TokenManager` makes `test_full_token_flow_leaves_no_secret_in_logs_or_results` fail.
  The change was reverted.

## Slice 2: where the build differs from OBO.md (smallest fixes)

| # | OBO.md said | Build does | Why |
|---|---|---|---|
| S1 | "The client signs in with PKCE … posting Keycloak's login form with httpx (checked in the spike)" | Keycloak 26.8.2 marks its login-session cookies `Secure` even on `http://localhost`. httpx (like Python's cookiejar) does not send `Secure` cookies over plain HTTP, so the login POST got HTTP 400. `demo/obo_demo.py` copies the cookies from the login page into a `Cookie` header by hand. | The spike used curl, which treats localhost as a secure context |
| S2 | "No client sends `Mcp-Name`: tool error naming the scope" | A request that sends `Mcp-Method` without `Mcp-Name` is refused by the SDK (400, `-32020`). The tool-level scope check is reached by clients that send neither header (older protocol versions); a test covers that case. | SDK 2.3.0 behaviour |
| S3 | `ToolScopeGate` "inside the SDK's auth middleware" | It is appended to `user_middleware`: inside `AuthenticationMiddleware` (the user is known) but outside the route's `RequireAuthMiddleware`. So a logbook call whose token lacks both scopes gets the per-tool 403 naming both, not the SDK's 403 for `mcp:tools`. A test pins the order. | Where Starlette puts app middleware |
| S4 | Unknown `kid`: "one rate-limited refetch" | At most one JWKS fetch per 60 s; an unknown `kid` within 60 s of the last fetch is rejected without a fetch. A JWKS that cannot be fetched gives 401, never a 500. | Rate limit made concrete |
| S5 | Exchanged token expiry `min(X.exp, A.exp) − 30 s` | X's expiry is taken from the token response's `expires_in` (default 60 s if absent); X is treated as opaque and not decoded. | The server is only a client of X |
| S6 | Tool annotations `readOnlyHint: true` | The two `my_*` tools also set `openWorldHint: false` (one known API, the user's own data). | They do not reach the open world |
| S7 | HTTP flags `--issuer`, `--resource-url`, `--logbook-url` | Also `--port` (default 8000) and `--client-credentials-file` (default `~/.config/aviation-mcp/oauth-client.env`, keys `AVIATION_MCP_CLIENT_ID`/`AVIATION_MCP_CLIENT_SECRET`, id defaults to `aviation-mcp`). There is no `--host`: HTTP binds 127.0.0.1 only. `--logbook-url` without a secret stops start-up. | Needed to run it; host fixed by design §1 |
| S8 | `DESIGN.md` §2: `--transport streamable-http` | `--transport http`. | Shorter; DESIGN.md updated |
| S9 | Logbook rows | Rows that fail the result schema (ICAO pattern, label length, numbers) are dropped instead of failing the call or reaching the model. | Labels are user data |
| S10 | Realm from the spike | `aviation-demo-cli` and `other-app` have direct grants off, as the design table says (the spike realm had them on). The realm's display name and attributes say "DEMO ONLY". | Matches OBO.md §6 |
| S11 | `make demo-obo` checks (0)–(6) | Adds (7): the server and API logs from the run are searched for the inbound tokens, their signatures and the client secret. Refusal checks (0, 3, 4) use raw HTTP POSTs to show the status and `WWW-Authenticate`; the data checks (1, 2, 5) go through the SDK client. | Evidence for acceptance item 3 on a real run |
| S12 | Per-key exchange locks | The lock for a key is dropped when its entry leaves the LRU, not when it expires. At worst a dropped lock lets two concurrent calls for one user make two exchanges; it never mixes users. | Simplicity |

## Slice 2: found while testing

- **Leak test checked by mutation:** `tests/test_no_token_in_logs_http.py` runs with the
  redaction safety net off. Adding `log.debug("got %s", token)` to `TokenExchanger`, or a
  log line with the raw `sub` to `LogbookClient`, makes it fail ("exchanged 0 leaked into
  logs", "sub A leaked into logs"). Both changes were reverted.
- **pytest-asyncio and the ASGI lifespan:** entering the app's lifespan in an async fixture
  and leaving it in teardown fails, because anyio cancel scopes must exit in the task that
  entered them. The tests run the lifespan in one dedicated task (`tests/test_http_app.py`,
  `running`).
- **`make live-metar` once timed out** (`ConnectTimeout` to aviationweather.gov) while
  curl reached it; the rerun passed. Network flake, no code change.
