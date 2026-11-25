"""Acceptance: no token, client secret or Authorization header can reach a log line.

Layer 1: nothing logs headers, bodies or the token exchange. Proven by running a full
token flow (fetch, 401, refresh, retry, then rejected credentials) through the real
server at DEBUG with NO secret registered for redaction, and searching every log line.
Layer 2: if some code did log them, the redacting formatter masks them anyway.
"""

from __future__ import annotations

import io
import json
import logging

import httpx
import pytest
import respx
from mcp import Client
from pydantic import SecretStr

from aviation_mcp import logs
from aviation_mcp.auth import TOKEN_URL, TokenManager
from aviation_mcp.awc import AwcClient
from aviation_mcp.logs import configure_logging, http_event_hooks
from aviation_mcp.opensky import OPENSKY_BASE, OpenSkyClient
from aviation_mcp.server import build_server
from tests.conftest import TODAY, load_fixture

SECRET = "client-secret-ZX81-must-never-appear"
TOKEN_1 = "eyJhbGciOiJSUzI1NiJ9.first-token-payload.sig1"
TOKEN_2 = "eyJhbGciOiJSUzI1NiJ9.second-token-payload.sig2"
FORBIDDEN = [SECRET, TOKEN_1, TOKEN_2, "first-token-payload", "second-token-payload", "Bearer "]


async def _no_sleep(_s: float) -> None:
    return None


@pytest.fixture
def captured(monkeypatch):
    # Layer 1 must hold on its own: make sure no secret is registered for redaction.
    monkeypatch.setattr(logs, "_secrets", set())
    buf = io.StringIO()
    configure_logging(stream=buf, level="DEBUG")
    yield buf
    configure_logging(stream=io.StringIO(), level="WARNING")


def assert_clean(text: str, *, header_name_allowed: bool = False) -> None:
    for needle in FORBIDDEN:
        assert needle not in text, f"{needle!r} leaked into:\n{text}"
    if not header_name_allowed:  # the normal flow never logs headers at all
        assert "authorization" not in text.lower()


@respx.mock
async def test_full_token_flow_leaves_no_secret_in_logs_or_results(captured):
    respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": TOKEN_1, "expires_in": 1800}),
            httpx.Response(200, json={"access_token": TOKEN_2, "expires_in": 1800}),
            httpx.Response(401, json={"error": "invalid_client", "secret_echo": SECRET}),
        ]
    )
    respx.get(f"{OPENSKY_BASE}/states/all").mock(
        side_effect=[
            httpx.Response(401, json={"detail": f"expired token {TOKEN_1}"}),
            httpx.Response(200, json=load_fixture("opensky/states_icao24.json")),
            httpx.Response(401),
        ]
    )
    async with httpx.AsyncClient(event_hooks=http_event_hooks()) as http:
        tokens = TokenManager(http, "leak-test-client", SecretStr(SECRET))
        server = build_server(
            awc=AwcClient(http, sleep=_no_sleep),
            opensky=OpenSkyClient(http, tokens, sleep=_no_sleep),
            today=lambda: TODAY,
        )
        async with Client(server) as client:
            ok = await client.call_tool("get_aircraft", {"icao24": ["f0f001"]})
            tokens.invalidate(TOKEN_2)  # force a third token request, which is rejected
            bad = await client.call_tool("get_aircraft", {"icao24": ["f0f002"]})

    assert not ok.is_error and bad.is_error
    results_text = json.dumps([c.model_dump() for c in ok.content + bad.content])
    assert_clean(results_text)

    out = captured.getvalue()
    assert_clean(out)

    # The logs did capture the calls (an empty log would pass the check above trivially).
    records = [json.loads(line) for line in out.splitlines()]
    upstream = [r for r in records if r.get("msg") == "upstream call"]
    assert {(r["method"], r["host"], r["path"]) for r in upstream} >= {
        ("POST", "auth.opensky-network.org", "/auth/realms/opensky-network/protocol/openid-connect/token"),
        ("GET", "opensky-network.org", "/api/states/all"),
    }
    assert [r["upstream_status"] for r in upstream if r["method"] == "POST"] == [200, 200, 401]
    tool_lines = [r for r in records if r.get("msg") == "tool call"]
    assert [r["outcome"] for r in tool_lines] == ["ok", "upstream_error"]


def test_safety_net_masks_secrets_that_code_logs_by_mistake(captured):
    logs.register_secret(SECRET)
    log = logging.getLogger("aviation_mcp.careless")
    log.debug("request headers: %s", {"Authorization": f"Bearer {TOKEN_1}"})
    log.debug("token response: %s", json.dumps({"access_token": TOKEN_2, "expires_in": 1800}))
    log.debug("form: grant_type=client_credentials&client_id=x&client_secret=%s", SECRET)
    log.debug("secret alone: %s", SECRET)
    out = captured.getvalue()
    assert out.count(logs.REDACTED) >= 4
    assert_clean(out, header_name_allowed=True)
