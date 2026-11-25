"""HTTP mode start-up: required flags, OIDC discovery, and the exchange client secret."""

from __future__ import annotations

import os

import httpx
import pytest
import respx
from starlette.applications import Starlette

from aviation_mcp.http_app import build_from_args, discover
from aviation_mcp.server import parse_args
from tests.oauth_helpers import DISCOVERY_URL, ISSUER, JWKS_URL, RESOURCE, TOKEN_URL

HTTP_ARGS = ["--transport", "http", "--issuer", ISSUER, "--resource-url", RESOURCE]
DISCOVERY = {"issuer": ISSUER, "token_endpoint": TOKEN_URL, "jwks_uri": JWKS_URL}
SECRET = "DEMO-ONLY-cli-test-secret"


def args_for(tmp_path, *extra: str):
    none = str(tmp_path / "none.env")
    return parse_args(
        [*HTTP_ARGS, "--credentials-file", none, "--client-credentials-file", none, *extra]
    )


def test_default_transport_is_stdio():
    args = parse_args([])
    assert args.transport == "stdio"
    assert args.port == 8000


@pytest.mark.parametrize("missing", ["--issuer", "--resource-url"])
async def test_http_refuses_to_start_without_issuer_or_resource(tmp_path, capsys, missing):
    argv = list(HTTP_ARGS)
    i = argv.index(missing)
    del argv[i : i + 2]
    args = parse_args([*argv, "--credentials-file", str(tmp_path / "none.env")])
    with pytest.raises(SystemExit) as exc:
        await build_from_args(args, env={})
    assert exc.value.code == 2
    assert missing in capsys.readouterr().err


@respx.mock
async def test_discovery_reads_token_and_jwks_endpoints(http):
    respx.get(DISCOVERY_URL).mock(return_value=httpx.Response(200, json=DISCOVERY))
    assert await discover(http, ISSUER) == (TOKEN_URL, JWKS_URL)


@respx.mock
async def test_discovery_with_another_issuer_stops_startup(http, capsys):
    other = dict(DISCOVERY, issuer="http://evil.example/realms/x")
    respx.get(DISCOVERY_URL).mock(return_value=httpx.Response(200, json=other))
    with pytest.raises(SystemExit) as exc:
        await discover(http, ISSUER)
    assert exc.value.code == 2
    assert "issuer" in capsys.readouterr().err


@respx.mock
async def test_discovery_unreachable_stops_startup(http, capsys):
    respx.get(DISCOVERY_URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(SystemExit):
        await discover(http, ISSUER)
    assert ISSUER in capsys.readouterr().err


@respx.mock
async def test_http_app_without_logbook_url_has_five_tools(tmp_path):
    respx.get(DISCOVERY_URL).mock(return_value=httpx.Response(200, json=DISCOVERY))
    app, http, server = await build_from_args(args_for(tmp_path), env={})
    try:
        assert isinstance(app, Starlette)
        assert len(await server.list_tools()) == 5
    finally:
        await http.aclose()


async def test_logbook_url_without_client_secret_stops_startup(tmp_path, capsys):
    with respx.mock:
        respx.get(DISCOVERY_URL).mock(return_value=httpx.Response(200, json=DISCOVERY))
        with pytest.raises(SystemExit) as exc:
            await build_from_args(args_for(tmp_path, "--logbook-url", "http://localhost:8001"),
                                  env={})
    assert exc.value.code == 2
    assert "AVIATION_MCP_CLIENT_SECRET" in capsys.readouterr().err


@respx.mock
async def test_logbook_url_with_secret_in_env_adds_the_logbook_tools(tmp_path):
    respx.get(DISCOVERY_URL).mock(return_value=httpx.Response(200, json=DISCOVERY))
    env = {"AVIATION_MCP_CLIENT_SECRET": SECRET}
    _app, http, server = await build_from_args(
        args_for(tmp_path, "--logbook-url", "http://localhost:8001"), env=env
    )
    try:
        names = {t.name for t in await server.list_tools()}
        assert {"my_saved_airports", "my_recent_flights"} <= names and len(names) == 7
    finally:
        await http.aclose()


@respx.mock
async def test_client_secret_from_0600_file(tmp_path):
    respx.get(DISCOVERY_URL).mock(return_value=httpx.Response(200, json=DISCOVERY))
    f = tmp_path / "oauth-client.env"
    f.write_text(f"AVIATION_MCP_CLIENT_ID=aviation-mcp\nAVIATION_MCP_CLIENT_SECRET={SECRET}\n")
    os.chmod(f, 0o600)
    args = parse_args([*HTTP_ARGS, "--credentials-file", str(tmp_path / "none.env"),
                       "--client-credentials-file", str(f), "--logbook-url", "http://x:1"])
    _app, http, server = await build_from_args(args, env={})
    await http.aclose()
    assert len(await server.list_tools()) == 7


async def test_world_readable_client_secret_file_stops_startup(tmp_path, capsys):
    f = tmp_path / "oauth-client.env"
    f.write_text(f"AVIATION_MCP_CLIENT_SECRET={SECRET}\n")
    os.chmod(f, 0o644)
    args = parse_args([*HTTP_ARGS, "--credentials-file", str(tmp_path / "none.env"),
                       "--client-credentials-file", str(f), "--logbook-url", "http://x:1"])
    with pytest.raises(SystemExit):
        await build_from_args(args, env={})
    err = capsys.readouterr().err
    assert "chmod 600" in err and SECRET not in err
