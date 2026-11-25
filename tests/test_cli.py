"""Entry point: argument parsing, credentials, and a real stdio subprocess start."""

from __future__ import annotations

import os
import sys

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from aviation_mcp.server import create_app, parse_args


def test_defaults():
    args = parse_args([])
    assert str(args.credentials_file).endswith(".config/aviation-mcp/opensky.env")
    assert args.log_level == "INFO"


async def test_no_credentials_builds_server_without_opensky(tmp_path, caplog):
    app = create_app(parse_args(["--credentials-file", str(tmp_path / "none.env")]), env={})
    assert app.opensky is None
    assert "OpenSky not configured" in caplog.text
    await app.aclose()


async def test_credentials_from_env_enable_opensky(tmp_path):
    env = {"OPENSKY_CLIENT_ID": "id", "OPENSKY_CLIENT_SECRET": "secret-value-1"}
    app = create_app(parse_args(["--credentials-file", str(tmp_path / "none.env")]), env=env)
    assert app.opensky is not None
    await app.aclose()


def test_http_client_timeouts():
    app = create_app(parse_args(["--credentials-file", "/nonexistent"]), env={})
    t = app.http.timeout
    assert (t.connect, t.read) == (5.0, 10.0)


def test_world_readable_credentials_file_stops_startup(tmp_path, capsys):
    f = tmp_path / "opensky.env"
    f.write_text("OPENSKY_CLIENT_ID=a\nOPENSKY_CLIENT_SECRET=supersecret\n")
    os.chmod(f, 0o644)
    with pytest.raises(SystemExit) as exc:
        create_app(parse_args(["--credentials-file", str(f)]), env={})
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "chmod 600" in err and "supersecret" not in err


async def test_server_starts_over_stdio_without_credentials(tmp_path):
    """Spawn the real entry point as a subprocess (pipes, not sockets) and list tools."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENSKY_")}
    env["HOME"] = str(tmp_path)  # no ~/.config/aviation-mcp/opensky.env either
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "aviation_mcp"], env=env, cwd=str(tmp_path)
    )
    async with Client(params) as client:
        tools = (await client.list_tools()).tools
        result = await client.call_tool("airport_flights", {"airport": "ESSA", "direction": "arrivals"})
    assert len(tools) == 5
    assert result.is_error
    assert "OpenSky is not configured" in result.content[0].text
