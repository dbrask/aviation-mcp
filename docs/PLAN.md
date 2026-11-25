# aviation-mcp: eval seed, Hermes wiring, slice plan

Companion to `DESIGN.md` (tool schemas and the six areas). Numbered citations refer to the Sources list below.

## Tool-selection check (eval seed)

| # | Prompt | Expected call |
|---|---|---|
| 1 | What's the weather at Arlanda right now? | `get_metar(stations=["ESSA"])` |
| 2 | Will it snow at Kiruna tomorrow morning? | `get_taf(stations=["ESNQ"])` |
| 3 | Compare current conditions at Heathrow and Schiphol | `get_metar(stations=["EGLL","EHAM"])` |
| 4 | Has Landvetter's wind been gusting over the last few hours? | `get_metar(stations=["ESGG"], hours_back=6)` |
| 5 | What planes are flying near Bromma? | `aircraft_near_airport(airport="ESSB")` |
| 6 | Any aircraft within 100 km of Frankfurt? | `aircraft_near_airport(airport="EDDF", radius_km=100)` |
| 7 | Where is aircraft 4ca7b5 now? | `get_aircraft(icao24=["4ca7b5"])` |
| 8 | Which flights arrived at Arlanda yesterday evening? | `airport_flights(airport="ESSA", direction="arrivals", hour_from=17, hour_to=24)` |
| 9 | How many departures did Copenhagen have last Monday? | `airport_flights(airport="EKCH", direction="departures", date_utc=<that Monday>)` |
| 10 | What departed from Arlanda in the last hour? | no call, or `airport_flights` answered with its "today not available" error. The model should say OpenSky only has yesterday and earlier. |

## Hermes and a voice assistant

Register the server in Hermes as a stdio server under `mcp_servers`.[12] Hermes resolves `${VAR}` placeholders in `args` at connect time.[12] Secrets therefore stay in the credentials file, and the config holds only its path:

```yaml
mcp_servers:
  aviation:
    command: "uv"
    args: ["--directory", "${HOME}/repo/aviation-mcp", "run", "aviation-mcp",
           "--credentials-file", "${HOME}/.config/aviation-mcp/opensky.env"]
    tools:
      include: [get_metar, get_taf]   # an always-on profile: weather only (see §0)
```

A voice assistant on that profile can then answer "what's the weather at Arlanda?" by calling `get_metar(["ESSA"])`. The decoded, unit-named fields let it answer in speech ("wind 220 at 12 knots, visibility over 6 miles, 3 degrees") instead of reading out the raw METAR. An interactive profile can include all five tools.

## Slice plan

- **Slice 1** (about one evening, 3–4 h): package skeleton, stdio `MCPServer`, the five tools, AWC and OpenSky clients, TokenManager, caching, redacting logger, offline test suite with fixtures, the eval YAML, README and CHANGELOG `0.1.0`. Done means `make test` is green with the network blocked, and Hermes lists the tools.
- **Slice 2** (optional, 2–3 h): Streamable HTTP plus `/healthz`, Dockerfile, CI workflow, `0.2.0`.
- **Not planned:** inbound OAuth and OBO (§5 stays design reasoning) and any hosted deployment.

## Sources

[12] https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp — Hermes docs: MCP
