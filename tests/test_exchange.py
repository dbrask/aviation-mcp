"""TokenExchanger: RFC 8693 exchange at the IdP and the per-user exchanged-token cache."""

from __future__ import annotations

import asyncio
import base64
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from mcp.server.auth.provider import AccessToken
from pydantic import SecretStr

from aviation_mcp.exchange import ExchangeError, TokenExchanger
from tests.conftest import FakeClock
from tests.oauth_helpers import ISSUER, RESOURCE, SUB_A, SUB_B, TOKEN_URL

SECRET = "DEMO-ONLY-exchange-secret-test"
NOW = 1_800_000_000.0


def inbound(sub: str = SUB_A, *, iss: str = ISSUER, ttl: int = 300, token: str | None = None):
    return AccessToken(
        token=token or f"inbound-{sub}-{iss[-5:]}",
        client_id="aviation-demo-cli",
        scopes=["mcp:tools", "mcp:logbook"],
        expires_at=int(NOW) + ttl,
        resource=RESOURCE,
        subject=sub,
        claims={"iss": iss},
    )


@pytest.fixture
def wall() -> FakeClock:
    return FakeClock(start=NOW)


@pytest.fixture
def exchanger(http, wall) -> TokenExchanger:
    return TokenExchanger(http, TOKEN_URL, "aviation-mcp", SecretStr(SECRET), clock=wall)


def issue_per_subject(expires_in: int = 300):
    """A token endpoint that answers with a token naming the subject token it got."""
    counter = {"n": 0}

    def answer(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        form = parse_qs(request.content.decode())
        subject = form["subject_token"][0]
        return httpx.Response(
            200,
            json={
                "access_token": f"x{counter['n']}-for-{subject}",
                "expires_in": expires_in,
                "token_type": "Bearer",
                "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
            },
        )

    return answer


@respx.mock
async def test_exchange_sends_rfc8693_form_with_basic_client_auth(exchanger):
    route = respx.post(TOKEN_URL).mock(side_effect=issue_per_subject())
    token = await exchanger.token_for(inbound())
    assert token == f"x1-for-inbound-{SUB_A}-{ISSUER[-5:]}"
    request = route.calls.last.request
    form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
    assert form == {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": f"inbound-{SUB_A}-{ISSUER[-5:]}",
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "audience": "logbook-api",
        "scope": "logbook:read",
    }
    basic = base64.b64decode(request.headers["authorization"].removeprefix("Basic ")).decode()
    assert basic == f"aviation-mcp:{SECRET}"


@respx.mock
async def test_second_call_for_same_user_is_served_from_cache(exchanger):
    route = respx.post(TOKEN_URL).mock(side_effect=issue_per_subject())
    first = await exchanger.token_for(inbound())
    second = await exchanger.token_for(inbound(token="a-newer-inbound-token-same-user"))
    assert first == second
    assert route.call_count == 1


@respx.mock
async def test_concurrent_calls_for_one_user_make_one_exchange(exchanger):
    async def slow(request):
        await asyncio.sleep(0.01)
        return issue_per_subject()(request)

    route = respx.post(TOKEN_URL).mock(side_effect=slow)
    tokens = await asyncio.gather(*(exchanger.token_for(inbound()) for _ in range(8)))
    assert len(set(tokens)) == 1
    assert route.call_count == 1


@respx.mock
async def test_user_a_cached_token_is_never_served_to_user_b(exchanger):
    route = respx.post(TOKEN_URL).mock(side_effect=issue_per_subject())
    a = await exchanger.token_for(inbound(SUB_A))
    b = await exchanger.token_for(inbound(SUB_B))
    a_again = await exchanger.token_for(inbound(SUB_A))
    b_again = await exchanger.token_for(inbound(SUB_B))
    assert a != b
    assert SUB_A in a and SUB_A in a_again and SUB_B not in a
    assert SUB_B in b and SUB_B in b_again and SUB_A not in b
    assert route.call_count == 2


@respx.mock
async def test_concurrent_users_each_get_their_own_token(exchanger):
    async def slow(request):
        await asyncio.sleep(0.01)
        return issue_per_subject()(request)

    respx.post(TOKEN_URL).mock(side_effect=slow)
    subs = [SUB_A, SUB_B] * 5
    tokens = await asyncio.gather(*(exchanger.token_for(inbound(s)) for s in subs))
    for sub, token in zip(subs, tokens, strict=True):
        assert token.endswith(f"inbound-{sub}-{ISSUER[-5:]}")


@respx.mock
async def test_same_subject_at_another_issuer_is_another_user(exchanger):
    route = respx.post(TOKEN_URL).mock(side_effect=issue_per_subject())
    a = await exchanger.token_for(inbound(SUB_A))
    other = await exchanger.token_for(inbound(SUB_A, iss="http://localhost:8180/realms/other"))
    assert a != other
    assert route.call_count == 2


@respx.mock
async def test_cached_token_is_replaced_30s_before_it_expires(exchanger, wall):
    route = respx.post(TOKEN_URL).mock(side_effect=issue_per_subject(expires_in=120))
    first = await exchanger.token_for(inbound(ttl=3600))
    wall.advance(89)
    assert await exchanger.token_for(inbound(ttl=3600)) == first
    wall.advance(2)  # 91 s: inside the 30 s margin of the 120 s token
    assert await exchanger.token_for(inbound(ttl=3600)) != first
    assert route.call_count == 2


@respx.mock
async def test_cache_lifetime_is_capped_at_the_inbound_token_expiry(exchanger, wall):
    route = respx.post(TOKEN_URL).mock(side_effect=issue_per_subject(expires_in=3600))
    await exchanger.token_for(inbound(ttl=100))
    wall.advance(71)  # inbound expires at 100 s, so the cache entry ends at 70 s
    await exchanger.token_for(inbound(ttl=100))
    assert route.call_count == 2


@respx.mock
async def test_evict_forces_a_new_exchange(exchanger):
    route = respx.post(TOKEN_URL).mock(side_effect=issue_per_subject())
    first = await exchanger.token_for(inbound())
    exchanger.evict(inbound(), first)
    second = await exchanger.token_for(inbound())
    assert second != first and route.call_count == 2


@respx.mock
async def test_evicting_a_stale_token_keeps_the_newer_one(exchanger):
    route = respx.post(TOKEN_URL).mock(side_effect=issue_per_subject())
    first = await exchanger.token_for(inbound())
    exchanger.evict(inbound(), first)
    second = await exchanger.token_for(inbound())
    exchanger.evict(inbound(), first)  # a late 401 for the old token
    assert await exchanger.token_for(inbound()) == second
    assert route.call_count == 2


@respx.mock
async def test_cache_is_bounded_least_recently_used_first(http, wall):
    route = respx.post(TOKEN_URL).mock(side_effect=issue_per_subject())
    ex = TokenExchanger(http, TOKEN_URL, "aviation-mcp", SecretStr(SECRET), clock=wall,
                        max_entries=2)
    await ex.token_for(inbound("u1"))
    await ex.token_for(inbound("u2"))
    await ex.token_for(inbound("u1"))  # u1 is now the most recent
    await ex.token_for(inbound("u3"))  # evicts u2
    assert len(ex) == 2
    await ex.token_for(inbound("u1"))
    assert route.call_count == 3
    await ex.token_for(inbound("u2"))
    assert route.call_count == 4


@pytest.mark.parametrize("status", [400, 401, 403])
@respx.mock
async def test_refused_exchange_says_sign_in_again_without_idp_body(exchanger, status):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(status, json={"error": "access_denied", "echo": SECRET})
    )
    with pytest.raises(ExchangeError) as exc:
        await exchanger.token_for(inbound())
    msg = str(exc.value)
    assert "sign in again" in msg
    assert SECRET not in msg and "access_denied" not in msg and "inbound-" not in msg


@pytest.mark.parametrize(
    "outcome",
    [httpx.Response(500), httpx.Response(503), httpx.ConnectError("refused"),
     httpx.Response(200, json={"no": "token"}), httpx.Response(200, text="<html>")],
)
@respx.mock
async def test_idp_down_or_garbled_says_unavailable(exchanger, outcome):
    if isinstance(outcome, Exception):
        respx.post(TOKEN_URL).mock(side_effect=outcome)
    else:
        respx.post(TOKEN_URL).mock(return_value=outcome)
    with pytest.raises(ExchangeError) as exc:
        await exchanger.token_for(inbound())
    assert "identity provider" in str(exc.value) and "unavailable" in str(exc.value)


@respx.mock
async def test_failed_exchange_is_not_cached(exchanger):
    route = respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json={"access_token": "x-ok", "expires_in": 300}),
        ]
    )
    with pytest.raises(ExchangeError):
        await exchanger.token_for(inbound())
    assert await exchanger.token_for(inbound()) == "x-ok"
    assert route.call_count == 2


async def test_inbound_token_without_subject_or_issuer_is_refused(exchanger):
    no_sub = inbound().model_copy(update={"subject": None})
    no_iss = inbound().model_copy(update={"claims": {}})
    for token in (no_sub, no_iss):
        with pytest.raises(ExchangeError):
            await exchanger.token_for(token)


@respx.mock
async def test_repr_shows_no_token_or_secret(exchanger):
    respx.post(TOKEN_URL).mock(side_effect=issue_per_subject())
    token = await exchanger.token_for(inbound())
    text = repr(exchanger) + str(exchanger)
    assert token not in text and SECRET not in text and SUB_A not in text
