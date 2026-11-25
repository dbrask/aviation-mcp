"""demo/logbook_api.py: the downstream resource server of the demo, offline."""

from __future__ import annotations

import hashlib

import httpx
import pytest
import respx

from aviation_mcp.jwt_verify import JwksClient, JwtVerifier
from demo.logbook_api import create_logbook_app
from tests.oauth_helpers import ISSUER, JWKS_URL, SUB_A, SUB_B, TestIdp

DATA = {
    SUB_A: {"airports": [{"icao": "ESSA", "label": "home base"}], "flights": [{"n": 1}]},
    SUB_B: {"airports": [{"icao": "EKCH", "label": "home base"}], "flights": []},
}


@pytest.fixture
def idp() -> TestIdp:
    return TestIdp()


@pytest.fixture
async def api(idp, http):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(JWKS_URL).mock(return_value=httpx.Response(200, json=idp.jwks()))
        verifier = JwtVerifier(JwksClient(http, JWKS_URL), issuer=ISSUER, audience="logbook-api")
        app = create_logbook_app(verifier, DATA)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8001") as c:
            yield c


def exchanged(idp, sub=SUB_A, **kw):
    claims = {"aud": "logbook-api", "azp": "aviation-mcp", "scope": "logbook:read", "sub": sub}
    claims.update(kw)
    return idp.mint(**claims)


async def get(api, path, token=None):
    headers = {"authorization": f"Bearer {token}"} if token else {}
    return await api.get(path, headers=headers)


async def test_each_user_gets_only_their_own_rows(api, idp):
    a = await get(api, "/me/airports", exchanged(idp, SUB_A))
    b = await get(api, "/me/airports", exchanged(idp, SUB_B))
    assert a.json() == {"airports": DATA[SUB_A]["airports"]}
    assert b.json() == {"airports": DATA[SUB_B]["airports"]}
    assert (await get(api, "/me/flights", exchanged(idp, SUB_A))).json() == {"flights": [{"n": 1}]}


async def test_unknown_user_gets_empty_lists(api, idp):
    resp = await get(api, "/me/airports", exchanged(idp, "someone-new"))
    assert resp.json() == {"airports": []}


async def test_inbound_mcp_token_is_refused_401(api, idp):
    resp = await get(api, "/me/airports", idp.mint())  # aud = MCP URI + aviation-mcp
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"].startswith('Bearer error="invalid_token"')


async def test_no_token_is_refused_401(api):
    assert (await get(api, "/me/airports")).status_code == 401


async def test_missing_logbook_read_scope_is_403(api, idp):
    resp = await get(api, "/me/airports", exchanged(idp, scope="profile"))
    assert resp.status_code == 403
    assert 'scope="logbook:read"' in resp.headers["www-authenticate"]


async def test_token_minted_for_another_client_is_403(api, idp):
    resp = await get(api, "/me/airports", exchanged(idp, azp="other-app"))
    assert resp.status_code == 403


async def test_seen_records_claims_and_hash_but_never_the_token(api, idp):
    token = exchanged(idp, SUB_A, jti="jti-123")
    await get(api, "/me/airports", token)
    seen = (await api.get("/_demo/seen")).json()["seen"]
    assert len(seen) == 1
    rec = seen[0]
    assert rec["jti"] == "jti-123"
    assert rec["aud"] == "logbook-api" and rec["azp"] == "aviation-mcp"
    assert rec["scope"] == "logbook:read"
    assert rec["token_sha256_12"] == hashlib.sha256(token.encode()).hexdigest()[:12]
    assert rec["subject"].startswith("p_") and SUB_A not in str(rec)
    assert token not in str(seen) and token.split(".")[2] not in str(seen)


def test_realm_import_holds_only_marked_throwaway_secrets():
    import json
    from pathlib import Path

    root = Path(__file__).parent.parent / "demo"
    realm = json.loads((root / "keycloak" / "aviation-demo-realm.json").read_text())
    assert "DEMO ONLY" in realm["displayName"]
    secrets = [c["secret"] for c in realm["clients"] if "secret" in c]
    secrets += [cred["value"] for u in realm["users"] for cred in u["credentials"]]
    assert secrets and all(s.startswith("DEMO-ONLY-") for s in secrets)
    assert all(u["email"].endswith("@example.invalid") for u in realm["users"])
    compose = (root / "compose.yaml").read_text()
    assert "KC_BOOTSTRAP_ADMIN_PASSWORD: DEMO-ONLY-" in compose
    assert '"127.0.0.1:8180:8080"' in compose
