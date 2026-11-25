"""JwtVerifier: signature, algorithm, key id, issuer, audience, expiry, subject, scopes."""

from __future__ import annotations

import time

import httpx
import jwt
import pytest
import respx

from aviation_mcp.jwt_verify import JwksClient, JwtVerifier
from tests.oauth_helpers import ISSUER, JWKS_URL, RESOURCE, SUB_A, TestIdp


@pytest.fixture
def idp() -> TestIdp:
    return TestIdp()


@pytest.fixture
def jwks_route(idp):
    with respx.mock(assert_all_called=False) as mock:
        yield mock.get(JWKS_URL).mock(return_value=httpx.Response(200, json=idp.jwks()))


@pytest.fixture
async def verifier(http, clock):
    return JwtVerifier(JwksClient(http, JWKS_URL, clock=clock), issuer=ISSUER, audience=RESOURCE)


async def test_valid_token_becomes_access_token_for_this_resource(idp, jwks_route, verifier):
    token = idp.mint()
    info = await verifier.verify_token(token)
    assert info is not None
    assert info.token == token
    assert info.subject == SUB_A
    assert info.client_id == "aviation-demo-cli"
    assert info.scopes == ["mcp:tools", "mcp:logbook"]
    assert info.resource == RESOURCE  # what the SDK's validate_token_resource compares
    assert info.claims["iss"] == ISSUER
    assert abs(info.expires_at - (int(time.time()) + 300)) <= 2


async def test_string_audience_is_accepted(idp, jwks_route, verifier):
    assert await verifier.verify_token(idp.mint(aud=RESOURCE)) is not None


async def test_bad_signature_is_rejected(idp, jwks_route, verifier):
    forged = idp.mint(key=idp.other_key)  # right kid, wrong key
    assert await verifier.verify_token(forged) is None


async def test_tampered_payload_is_rejected(idp, jwks_route, verifier):
    head, _payload, sig = idp.mint().split(".")
    other_payload = idp.mint(sub="someone-else").split(".")[1]
    assert await verifier.verify_token(f"{head}.{other_payload}.{sig}") is None


async def test_alg_none_is_rejected(idp, jwks_route, verifier):
    token = jwt.encode(idp.claims(), None, algorithm="none", headers={"kid": idp.kid})
    assert await verifier.verify_token(token) is None


async def test_hs256_signed_with_public_key_is_rejected(idp, jwks_route, verifier):
    # Classic key-confusion attack: HMAC keyed with the RSA public key's bytes.
    secret = b"x" * 64
    token = jwt.encode(idp.claims(), secret, algorithm="HS256", headers={"kid": idp.kid})
    assert await verifier.verify_token(token) is None


async def test_expired_token_is_rejected(idp, jwks_route, verifier):
    now = int(time.time())
    assert await verifier.verify_token(idp.mint(iat=now - 400, exp=now - 60)) is None


async def test_expiry_has_ten_seconds_leeway(idp, jwks_route, verifier):
    now = int(time.time())
    assert await verifier.verify_token(idp.mint(iat=now - 300, exp=now - 5)) is not None


async def test_not_yet_valid_token_is_rejected(idp, jwks_route, verifier):
    now = int(time.time())
    assert await verifier.verify_token(idp.mint(nbf=now + 120)) is None


async def test_wrong_issuer_is_rejected(idp, jwks_route, verifier):
    token = idp.mint(iss="http://localhost:8180/realms/other")
    assert await verifier.verify_token(token) is None


async def test_audience_without_this_resource_is_rejected(idp, jwks_route, verifier):
    assert await verifier.verify_token(idp.mint(aud="other-app")) is None
    assert await verifier.verify_token(idp.mint(aud=["logbook-api"])) is None


async def test_missing_audience_is_rejected(idp, jwks_route, verifier):
    assert await verifier.verify_token(idp.mint(aud=None)) is None


async def test_missing_subject_is_rejected(idp, jwks_route, verifier):
    assert await verifier.verify_token(idp.mint(sub=None)) is None


async def test_missing_expiry_is_rejected(idp, jwks_route, verifier):
    assert await verifier.verify_token(idp.mint(exp=None)) is None


async def test_token_without_scope_claim_has_no_scopes(idp, jwks_route, verifier):
    info = await verifier.verify_token(idp.mint(scope=None))
    assert info is not None and info.scopes == []


async def test_garbage_is_rejected(jwks_route, verifier):
    for junk in ("", "abc", "a.b.c", "Bearer x"):
        assert await verifier.verify_token(junk) is None


async def test_encryption_key_never_verifies(idp, jwks_route, verifier):
    token = idp.mint(key=idp.other_key, kid="enc-key-1")
    assert await verifier.verify_token(token) is None


async def test_jwks_is_fetched_once_and_cached(idp, jwks_route, verifier):
    for _ in range(3):
        assert await verifier.verify_token(idp.mint()) is not None
    assert jwks_route.call_count == 1


async def test_unknown_kid_triggers_one_rate_limited_refetch(idp, jwks_route, verifier, clock):
    assert await verifier.verify_token(idp.mint()) is not None
    unknown = idp.mint(kid="rotated-key")
    assert await verifier.verify_token(unknown) is None  # just fetched: no refetch yet
    assert jwks_route.call_count == 1
    clock.advance(61)
    assert await verifier.verify_token(unknown) is None
    assert await verifier.verify_token(unknown) is None  # within the refetch interval
    assert jwks_route.call_count == 2
    clock.advance(61)
    assert await verifier.verify_token(unknown) is None
    assert jwks_route.call_count == 3


async def test_key_rotation_is_picked_up_by_refetch(idp, jwks_route, verifier, clock):
    assert await verifier.verify_token(idp.mint()) is not None
    clock.advance(61)
    idp.key, idp.kid = idp.other_key, "sig-key-2"  # the IdP rotates its signing key
    jwks_route.mock(return_value=httpx.Response(200, json=idp.jwks()))
    assert await verifier.verify_token(idp.mint()) is not None


async def test_jwks_unreachable_rejects_without_raising(idp, verifier):
    with respx.mock:
        respx.get(JWKS_URL).mock(side_effect=httpx.ConnectError("down"))
        assert await verifier.verify_token(idp.mint()) is None


async def test_rejection_logs_reason_but_not_token(idp, jwks_route, verifier, caplog):
    caplog.set_level("DEBUG")
    token = idp.mint(aud="other-app")
    assert await verifier.verify_token(token) is None
    assert "token rejected" in caplog.text
    assert token not in caplog.text
    assert token.split(".")[1] not in caplog.text
