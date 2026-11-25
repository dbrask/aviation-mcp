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
5. **Slice 2 (HTTP, Docker, CI):** not built yet.
6. **Python and SDK:** Python 3.12 (`.python-version`, `requires-python >=3.12`). The SDK
   is pinned exactly to `mcp==2.3.0` and locked in `uv.lock`.
7. **Build and review:** the code was written by a coding agent (Claude) from `DESIGN.md`.
   A model from another vendor (GPT) reviewed it and required five changes (R1 to R5 below),
   then approved the fixes.

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
