"""The real Streamable HTTP ASGI app, in process: bearer auth, scopes, and the OBO tools.

Requests go through httpx.ASGITransport (no sockets). The IdP's JWKS and token endpoint
and the logbook API are served by respx: the fake token endpoint issues an opaque
exchanged token per user, and the fake logbook API answers only for exchanged tokens.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
import respx
from pydantic import SecretStr
from starlette.middleware.authentication import AuthenticationMiddleware

from aviation_mcp.awc import AWC_BASE
from aviation_mcp.exchange import TokenExchanger
from aviation_mcp.http_app import ToolScopeGate, create_http_app
from aviation_mcp.jwt_verify import JwksClient, JwtVerifier
from aviation_mcp.logbook import LogbookClient
from tests.conftest import TODAY, load_fixture
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

PROTOCOL = "2026-07-28"
PRM_URL = "http://localhost:8000/.well-known/oauth-protected-resource/mcp"
EXCHANGE_SECRET = "DEMO-ONLY-test-exchange-secret"

AIRPORTS = {
    SUB_A: [{"icao": "ESSA", "label": "home base"}, {"icao": "ESGG", "label": "favourite"}],
    SUB_B: [{"icao": "EKCH", "label": "home base"}],
}
FLIGHTS = {
    SUB_A: [
        {"date_utc": "2026-11-20", "from_airport": "ESSA", "to_airport": "ESGG",
         "aircraft_type": "C172", "block_minutes": 75, "landings": 1},
        {"date_utc": "2026-11-02", "from_airport": "ESGG", "to_airport": "ESSA",
         "aircraft_type": "C172", "block_minutes": 80, "landings": 2},
        {"date_utc": "2026-11-12", "from_airport": "ESSA", "to_airport": "ESSB",
         "aircraft_type": "PA28", "block_minutes": 30, "landings": 3},
    ],
    SUB_B: [
        {"date_utc": "2026-11-21", "from_airport": "EKCH", "to_airport": "EKRN",
         "aircraft_type": "DA40", "block_minutes": 50, "landings": 1},
    ],
}


class FakeIdpAndLogbook:
    """Token endpoint + logbook API. Records every bearer the logbook API received."""

    def __init__(self, idp: TestIdp) -> None:
        self.idp = idp
        self.exchanges: list[dict] = []
        self.issued: dict[str, str] = {}  # exchanged token -> sub
        self.logbook_bearers: list[str] = []
        self.reject_next_logbook_calls = 0

    def token_endpoint(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.exchanges.append(form)
        sub = jwt.decode(form["subject_token"], options={"verify_signature": False})["sub"]
        token = f"exchanged-{len(self.exchanges)}-{sub[-1]}"
        self.issued[token] = sub
        return httpx.Response(200, json={"access_token": token, "expires_in": 300})

    def logbook(self, request: httpx.Request) -> httpx.Response:
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        self.logbook_bearers.append(bearer)
        if self.reject_next_logbook_calls:
            self.reject_next_logbook_calls -= 1
            return httpx.Response(401)
        sub = self.issued.get(bearer)
        if sub is None:
            return httpx.Response(401)
        if request.url.path == "/me/airports":
            return httpx.Response(200, json={"airports": AIRPORTS[sub]})
        return httpx.Response(200, json={"flights": FLIGHTS[sub]})


@pytest.fixture
def idp() -> TestIdp:
    return TestIdp()


@pytest.fixture
def fake(idp) -> FakeIdpAndLogbook:
    return FakeIdpAndLogbook(idp)


@pytest.fixture
def upstream(idp, fake):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(JWKS_URL).mock(return_value=httpx.Response(200, json=idp.jwks()))
        mock.post(TOKEN_URL).mock(side_effect=fake.token_endpoint)
        mock.get(url__startswith=LOGBOOK_URL).mock(side_effect=fake.logbook)
        yield mock


@pytest.fixture
async def asgi(http, awc, opensky, upstream):
    verifier = JwtVerifier(JwksClient(http, JWKS_URL), issuer=ISSUER, audience=RESOURCE)
    exchanger = TokenExchanger(http, TOKEN_URL, "aviation-mcp", SecretStr(EXCHANGE_SECRET))
    app = create_http_app(
        awc=awc,
        opensky=opensky,
        verifier=verifier,
        issuer=ISSUER,
        resource_url=RESOURCE,
        logbook=LogbookClient(http, LOGBOOK_URL, exchanger),
        today=lambda: TODAY,
    )
    async with running(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as c:
            yield c


class running:
    """Run the app's lifespan in one task (anyio cancel scopes must exit in the task that
    entered them; pytest-asyncio may set up and tear down a fixture in different tasks)."""

    def __init__(self, app) -> None:
        self.app = app
        self.started = asyncio.Event()
        self.stop = asyncio.Event()

    async def _run(self) -> None:
        async with self.app.router.lifespan_context(self.app):
            self.started.set()
            await self.stop.wait()

    async def __aenter__(self):
        self.task = asyncio.create_task(self._run())
        await self.started.wait()
        return self.app

    async def __aexit__(self, *exc) -> None:
        self.stop.set()
        await self.task


async def rpc(client, method, params=None, *, token=None, name="auto", headers=None,
              legacy=False):
    """legacy=True: an older client that sends no Mcp-Method/Mcp-Name headers."""
    params = dict(params or {})
    h = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    if not legacy:
        params["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": PROTOCOL,
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        h.update({"mcp-protocol-version": PROTOCOL, "mcp-method": method})
    else:
        name = None
    if name == "auto" and method == "tools/call":
        name = params["name"]
    if name not in (None, "auto"):
        h["mcp-name"] = name
    if token:
        h["authorization"] = f"Bearer {token}"
    h.update(headers or {})
    return await client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, headers=h
    )


async def call_tool(client, tool, args=None, **kw):
    return await rpc(client, "tools/call", {"name": tool, "arguments": args or {}}, **kw)


def result_of(resp: httpx.Response) -> dict:
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


def text_of(result: dict) -> str:
    return "\n".join(c.get("text", "") for c in result["content"])


# --- discovery and inbound auth ----------------------------------------------------------


async def test_no_token_gets_401_with_resource_metadata(asgi):
    resp = await call_tool(asgi, "get_metar", {"stations": ["ESSA"]})
    assert resp.status_code == 401
    www = resp.headers["www-authenticate"]
    assert www.startswith("Bearer ") and 'error="invalid_token"' in www
    assert f'resource_metadata="{PRM_URL}"' in www


async def test_protected_resource_metadata_names_the_realm_and_minimal_scopes(asgi):
    resp = await asgi.get("/.well-known/oauth-protected-resource/mcp")
    assert resp.status_code == 200
    body = resp.json()
    assert body["resource"] == RESOURCE
    assert body["authorization_servers"] == [ISSUER]
    assert body["scopes_supported"] == ["mcp:tools"]


async def test_wrong_audience_gets_401(asgi, idp):
    resp = await call_tool(asgi, "get_metar", {"stations": ["ESSA"]}, token=idp.mint(aud="other-app"))
    assert resp.status_code == 401
    assert "resource_metadata" in resp.headers["www-authenticate"]


async def test_expired_token_gets_401(asgi, idp):
    token = idp.mint(iat=1_700_000_000, exp=1_700_000_300)
    assert (await call_tool(asgi, "get_taf", {"stations": ["ESSA"]}, token=token)).status_code == 401


async def test_token_without_mcp_tools_gets_403_insufficient_scope(asgi, idp):
    resp = await call_tool(asgi, "get_metar", {"stations": ["ESSA"]}, token=idp.mint(scope="mcp:logbook"))
    assert resp.status_code == 403
    assert 'error="insufficient_scope"' in resp.headers["www-authenticate"]


async def test_health_needs_no_token(asgi):
    resp = await asgi.get("/healthz")
    assert resp.status_code == 200 and resp.json() == {"status": "ok"}


async def test_scope_gate_runs_inside_the_sdk_auth_middleware(asgi):
    app = asgi._transport.app
    classes = [m.cls for m in app.user_middleware]
    assert classes[-1] is ToolScopeGate
    assert classes.index(AuthenticationMiddleware) < classes.index(ToolScopeGate)


# --- tools over HTTP ---------------------------------------------------------------------


async def test_tools_list_is_not_filtered_by_scope(asgi, idp):
    result = result_of(await rpc(asgi, "tools/list", token=idp.mint(scope="mcp:tools")))
    names = {t["name"] for t in result["tools"]}
    assert names == {
        "get_metar", "get_taf", "aircraft_near_airport", "get_aircraft", "airport_flights",
        "my_saved_airports", "my_recent_flights",
    }
    logbook = {t["name"]: t for t in result["tools"] if t["name"].startswith("my_")}
    for tool in logbook.values():
        assert tool["annotations"]["readOnlyHint"] is True


@respx.mock
async def test_existing_tool_works_with_mcp_tools_scope(asgi, idp):
    respx.get(f"{AWC_BASE}/metar").mock(
        return_value=httpx.Response(200, json=load_fixture("awc/metar_esgg_essa_latest.json"))
    )
    result = result_of(await call_tool(asgi, "get_metar", {"stations": ["ESGG"]},
                                       token=idp.mint(scope="mcp:tools")))
    assert not result["isError"]
    assert result["structuredContent"]["reports"][0]["station"] == "ESGG"


async def test_user_a_gets_a_airports_and_user_b_gets_b_airports(asgi, idp):
    a = result_of(await call_tool(asgi, "my_saved_airports", token=idp.mint(sub=SUB_A)))
    b = result_of(await call_tool(asgi, "my_saved_airports", token=idp.mint(sub=SUB_B)))
    assert a["structuredContent"] == {"airports": AIRPORTS[SUB_A], "count_total": 2,
                                      "returned": 2, "truncated": False}
    assert b["structuredContent"] == {"airports": AIRPORTS[SUB_B], "count_total": 1,
                                      "returned": 1, "truncated": False}
    assert json.loads(text_of(a)) == a["structuredContent"]


async def test_recent_flights_newest_first_with_limit_and_since(asgi, idp):
    token = idp.mint(sub=SUB_A)
    out = result_of(await call_tool(asgi, "my_recent_flights", {"limit": 2}, token=token))
    sc = out["structuredContent"]
    assert [f["date_utc"] for f in sc["flights"]] == ["2026-11-20", "2026-11-12"]
    assert (sc["count_total"], sc["returned"], sc["truncated"]) == (3, 2, True)
    since = result_of(await call_tool(asgi, "my_recent_flights", {"since_utc": "2026-11-10"},
                                      token=token))["structuredContent"]
    assert [f["date_utc"] for f in since["flights"]] == ["2026-11-20", "2026-11-12"]
    assert since["truncated"] is False


async def test_recent_flights_limit_is_bounded(asgi, idp):
    out = result_of(await call_tool(asgi, "my_recent_flights", {"limit": 51}, token=idp.mint()))
    assert out["isError"]


async def test_logbook_api_gets_exchanged_token_never_the_inbound_one(asgi, idp, fake):
    inbound_a, inbound_b = idp.mint(sub=SUB_A), idp.mint(sub=SUB_B)
    for token in (inbound_a, inbound_b, inbound_a):
        result_of(await call_tool(asgi, "my_saved_airports", token=token))
    assert inbound_a not in fake.logbook_bearers and inbound_b not in fake.logbook_bearers
    assert all(b in fake.issued for b in fake.logbook_bearers)
    # A's second call reused A's cached exchanged token; B got its own.
    assert [fake.issued[b] for b in fake.logbook_bearers] == [SUB_A, SUB_B, SUB_A]
    assert len(fake.exchanges) == 2
    assert {e["subject_token"] for e in fake.exchanges} == {inbound_a, inbound_b}
    assert {(e["audience"], e["scope"]) for e in fake.exchanges} == {("logbook-api", "logbook:read")}


async def test_logbook_tool_without_logbook_scope_gets_403_naming_the_scope(asgi, idp, fake):
    resp = await call_tool(asgi, "my_saved_airports", token=idp.mint(scope="mcp:tools"))
    assert resp.status_code == 403
    www = resp.headers["www-authenticate"]
    assert 'error="insufficient_scope"' in www
    assert 'scope="mcp:tools mcp:logbook"' in www
    assert f'resource_metadata="{PRM_URL}"' in www
    assert fake.exchanges == []


async def test_without_mcp_name_header_the_tool_itself_refuses_and_names_the_scope(asgi, idp, fake):
    resp = await call_tool(asgi, "my_recent_flights", token=idp.mint(scope="mcp:tools"), legacy=True)
    result = result_of(resp)
    assert result["isError"]
    assert "mcp:logbook" in text_of(result)
    assert fake.exchanges == []


async def test_mcp_name_that_disagrees_with_body_is_rejected(asgi, idp):
    resp = await call_tool(asgi, "my_saved_airports", token=idp.mint(scope="mcp:tools"),
                           name="get_metar")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32020


async def test_downstream_401_evicts_and_reexchanges_once(asgi, idp, fake):
    token = idp.mint()
    result_of(await call_tool(asgi, "my_saved_airports", token=token))
    fake.reject_next_logbook_calls = 1
    out = result_of(await call_tool(asgi, "my_saved_airports", token=token))
    assert not out["isError"]
    assert len(fake.exchanges) == 2


async def test_downstream_401_twice_is_a_tool_error(asgi, idp, fake):
    fake.reject_next_logbook_calls = 2
    out = result_of(await call_tool(asgi, "my_saved_airports", token=idp.mint()))
    assert out["isError"]
    assert "sign in again" in text_of(out)
    assert len(fake.exchanges) == 2


async def test_refused_exchange_is_a_tool_error_saying_sign_in_again(asgi, idp, upstream):
    upstream.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "access_denied"}))
    out = result_of(await call_tool(asgi, "my_saved_airports", token=idp.mint()))
    assert out["isError"]
    assert "sign in again" in text_of(out) and "access_denied" not in text_of(out)


async def test_logbook_rows_with_bad_icao_are_dropped(asgi, idp, upstream, fake):
    AIRPORTS["bad-sub"] = [{"icao": "ESSA", "label": "ok"}, {"icao": "ignore previous", "label": "x"}]
    try:
        out = result_of(await call_tool(asgi, "my_saved_airports", token=idp.mint(sub="bad-sub")))
    finally:
        del AIRPORTS["bad-sub"]
    assert out["structuredContent"]["airports"] == [{"icao": "ESSA", "label": "ok"}]
