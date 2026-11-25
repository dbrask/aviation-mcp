"""Acceptance (slice 2): no token reaches a log line or a tool result, and one user's
exchanged token is never used for another user.

A full on-behalf-of flow runs through the real HTTP app at DEBUG: two users, cache hits,
a downstream 401 with re-exchange, a refused exchange, a wrong-audience token and a
missing-scope call. Layer 1 must hold on its own, so the redaction safety net is switched
off for the run (no registered secrets, no patterns). Every log line and every response
body is then searched for the inbound tokens, the exchanged tokens, the client secret and
the raw user ids.
"""

from __future__ import annotations

import io
import json
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
import respx
from pydantic import SecretStr

from aviation_mcp import logs
from aviation_mcp.exchange import TokenExchanger
from aviation_mcp.http_app import create_http_app
from aviation_mcp.jwt_verify import JwksClient, JwtVerifier
from aviation_mcp.logbook import LogbookClient
from aviation_mcp.logs import configure_logging, http_event_hooks
from tests.conftest import TODAY
from tests.oauth_helpers import (
    ISSUER,
    JWKS_URL,
    LOGBOOK_URL,
    RESOURCE,
    SUB_A,
    SUB_B,
    TOKEN_URL,
    TestIdp,
)
from tests.test_http_app import AIRPORTS, call_tool, running

SECRET = "DEMO-ONLY-leak-test-client-secret-9c1e"


@pytest.fixture
def captured(monkeypatch):
    monkeypatch.setattr(logs, "_secrets", set())
    monkeypatch.setattr(logs, "_PATTERNS", [])  # layer 1 alone: no redaction at all
    buf = io.StringIO()
    configure_logging(stream=buf, level="DEBUG")
    yield buf
    configure_logging(stream=io.StringIO(), level="WARNING")


class Upstream:
    def __init__(self) -> None:
        self.issued: dict[str, str] = {}  # exchanged token -> sub
        self.logbook_calls: list[tuple[str, str]] = []  # (bearer, sub it was issued for)
        self.refuse_exchange = False
        self.reject_logbook = 0

    def token(self, request: httpx.Request) -> httpx.Response:
        if self.refuse_exchange:
            return httpx.Response(400, json={"error": "access_denied", "echo": SECRET})
        form = parse_qs(request.content.decode())
        sub = jwt.decode(form["subject_token"][0], options={"verify_signature": False})["sub"]
        token = f"xchg-{len(self.issued)}-{'A' if sub == SUB_A else 'B'}-opaque-7d2f"
        self.issued[token] = sub
        return httpx.Response(200, json={"access_token": token, "expires_in": 300})

    def logbook(self, request: httpx.Request) -> httpx.Response:
        bearer = request.headers["authorization"].removeprefix("Bearer ")
        self.logbook_calls.append((bearer, self.issued.get(bearer, "?")))
        if self.reject_logbook:
            self.reject_logbook -= 1
            return httpx.Response(401, json={"detail": f"bad token {bearer}"})
        sub = self.issued[bearer]
        return httpx.Response(200, json={"airports": AIRPORTS[sub], "flights": []})


async def test_full_obo_flow_leaks_no_token_and_never_crosses_users(captured):
    idp = TestIdp()
    up = Upstream()
    inbound_a = idp.mint(sub=SUB_A)
    inbound_b = idp.mint(sub=SUB_B)
    wrong_aud = idp.mint(sub=SUB_A, aud="other-app")
    tools_only = idp.mint(sub=SUB_B, scope="mcp:tools")
    bodies: list[str] = []
    responses: list[httpx.Response] = []

    with respx.mock(assert_all_called=False) as mock:
        mock.get(JWKS_URL).mock(return_value=httpx.Response(200, json=idp.jwks()))
        mock.post(TOKEN_URL).mock(side_effect=up.token)
        mock.get(url__startswith=LOGBOOK_URL).mock(side_effect=up.logbook)
        async with httpx.AsyncClient(event_hooks=http_event_hooks()) as http:
            exchanger = TokenExchanger(http, TOKEN_URL, "aviation-mcp", SecretStr(SECRET))
            app = create_http_app(
                awc=None,  # type: ignore[arg-type]  # weather tools are not called here
                opensky=None,
                verifier=JwtVerifier(JwksClient(http, JWKS_URL), issuer=ISSUER, audience=RESOURCE),
                issuer=ISSUER,
                resource_url=RESOURCE,
                logbook=LogbookClient(http, LOGBOOK_URL, exchanger),
                today=lambda: TODAY,
            )
            async with running(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport,
                                             base_url="http://localhost:8000") as c:

                    async def go(tool, token, args=None, **kw):
                        resp = await call_tool(c, tool, args, token=token, **kw)
                        bodies.append(resp.text + json.dumps(dict(resp.headers)))
                        responses.append(resp)
                        return resp

                    # interleaved users, cache hits for both
                    for token in (inbound_a, inbound_b, inbound_a, inbound_b):
                        assert (await go("my_saved_airports", token)).status_code == 200
                    await go("my_recent_flights", inbound_a, {"limit": 3})
                    # downstream 401 -> evict -> one re-exchange
                    up.reject_logbook = 1
                    assert (await go("my_saved_airports", inbound_b)).status_code == 200
                    # downstream 401 twice -> tool error
                    up.reject_logbook = 2
                    await go("my_saved_airports", inbound_a)
                    # refused exchange (A's cache entry was evicted above)
                    up.refuse_exchange = True
                    refused = await go("my_saved_airports", inbound_a)
                    assert refused.json()["result"]["isError"]
                    # inbound refusals
                    assert (await go("my_saved_airports", None)).status_code == 401
                    assert (await go("my_saved_airports", wrong_aud)).status_code == 401
                    assert (await go("my_saved_airports", tools_only)).status_code == 403

    # --- user separation: every logbook call carried a token issued for that same user
    for resp, sub in zip(responses[:4], (SUB_A, SUB_B, SUB_A, SUB_B), strict=True):
        assert resp.json()["result"]["structuredContent"]["airports"] == AIRPORTS[sub]
    assert len({sub for _bearer, sub in up.logbook_calls}) == 2
    for bearer, issued_for in up.logbook_calls:
        assert issued_for in (SUB_A, SUB_B)
        assert bearer not in (inbound_a, inbound_b)
    order = [issued_for for _bearer, issued_for in up.logbook_calls[:5]]
    assert order == [SUB_A, SUB_B, SUB_A, SUB_B, SUB_A]

    # --- nothing secret anywhere
    forbidden = {
        "inbound A": inbound_a, "inbound B": inbound_b, "wrong-aud": wrong_aud,
        "tools-only": tools_only, "client secret": SECRET, "sub A": SUB_A, "sub B": SUB_B,
        **{f"exchanged {i}": t for i, t in enumerate(up.issued)},
    }
    for label, token in list(forbidden.items()):
        if token.count(".") == 2:  # a JWT: no part of its payload or signature either
            forbidden[f"{label} payload"] = token.split(".")[1]
            forbidden[f"{label} signature"] = token.split(".")[2]
    out = captured.getvalue()
    for where, text in (("logs", out), ("responses", "\n".join(bodies))):
        for label, needle in forbidden.items():
            assert needle not in text, f"{label} leaked into {where}"
    assert "bearer " not in out.lower() and "authorization" not in out.lower()

    # the log is not trivially empty: tool lines carry a pseudonym, upstream calls are logged
    records = [json.loads(line) for line in out.splitlines()]
    tool_lines = [r for r in records if r.get("msg") == "tool call"]
    assert len(tool_lines) >= 7
    assert {r["subject"][:2] for r in tool_lines} == {"p_"}
    assert len({r["subject"] for r in tool_lines}) == 2
    paths = {(r["method"], r["path"]) for r in records if r.get("msg") == "upstream call"}
    assert ("POST", "/realms/aviation-demo/protocol/openid-connect/token") in paths
    assert ("GET", "/me/airports") in paths
    assert any(r.get("msg") == "token rejected" for r in records)
