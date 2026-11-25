"""Smoke test: start the server over stdio as a subprocess, connect with the SDK's Client,
print the five tool names with their input schemas. With --call-metar ICAO, also make one
live get_metar call through the running server (aviationweather.gov, no credentials).

    uv run python scripts/smoke.py
    uv run python scripts/smoke.py --call-metar ESGG
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from mcp import Client
from mcp.client.stdio import StdioServerParameters


async def main(call_metar: str | None) -> int:
    params = StdioServerParameters(command=sys.executable, args=["-m", "aviation_mcp"])
    async with Client(params) as client:
        info = client.session.server_info if hasattr(client.session, "server_info") else None
        if info is not None:
            print(f"server: {info.name} {info.version}")
        tools = (await client.list_tools()).tools
        print(f"{len(tools)} tools")
        for tool in tools:
            print(f"\n## {tool.name}")
            print(json.dumps(tool.input_schema, indent=2))
        if call_metar:
            print(f"\n## live call: get_metar(stations=[{call_metar!r}])")
            result = await client.call_tool("get_metar", {"stations": [call_metar]})
            print("is_error:", result.is_error)
            if result.structured_content is not None:
                print(json.dumps(result.structured_content, indent=2, ensure_ascii=False))
            else:
                print("\n".join(getattr(c, "text", "") for c in result.content))
            return 1 if result.is_error else 0
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--call-metar", metavar="ICAO")
    sys.exit(asyncio.run(main(parser.parse_args().call_metar)))
