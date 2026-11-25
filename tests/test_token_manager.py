"""TokenManager: OAuth2 client-credentials token for OpenSky, held in memory only."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx
from pydantic import SecretStr

from aviation_mcp.auth import TOKEN_URL, OpenSkyAuthError, TokenManager

FAKE_ID = "test-client"
FAKE_SECRET = "s3cr3t-DO-NOT-LOG-7f9a"


def token_response(token: str, expires_in: int = 1800) -> httpx.Response:
    return httpx.Response(
        200, json={"access_token": token, "expires_in": expires_in, "token_type": "Bearer"}
    )


def make_manager(http: httpx.AsyncClient, clock) -> TokenManager:
    return TokenManager(http, FAKE_ID, SecretStr(FAKE_SECRET), clock=clock)


@respx.mock
async def test_fetches_token_with_client_credentials_grant(clock):
    route = respx.post(TOKEN_URL).mock(return_value=token_response("tok-1"))
    async with httpx.AsyncClient() as http:
        tm = make_manager(http, clock)
        assert await tm.get_token() == "tok-1"

    form = dict(httpx.QueryParams(route.calls.last.request.content.decode()))
    assert form == {
        "grant_type": "client_credentials",
        "client_id": FAKE_ID,
        "client_secret": FAKE_SECRET,
    }


@respx.mock
async def test_reuses_cached_token_until_refresh_margin(clock):
    route = respx.post(TOKEN_URL).mock(
        side_effect=[token_response("tok-1"), token_response("tok-2")]
    )
    async with httpx.AsyncClient() as http:
        tm = make_manager(http, clock)
        assert await tm.get_token() == "tok-1"
        clock.advance(1800 - 61)  # 61 s before expiry: still outside the 60 s margin
        assert await tm.get_token() == "tok-1"
        assert route.call_count == 1
        clock.advance(2)  # 59 s before expiry: inside the margin, refresh early
        assert await tm.get_token() == "tok-2"
        assert route.call_count == 2


@respx.mock
async def test_concurrent_callers_trigger_one_refresh(clock):
    async def slow_token(request):
        await asyncio.sleep(0.01)
        return token_response("tok-1")

    route = respx.post(TOKEN_URL).mock(side_effect=slow_token)
    async with httpx.AsyncClient() as http:
        tm = make_manager(http, clock)
        tokens = await asyncio.gather(*(tm.get_token() for _ in range(10)))
    assert tokens == ["tok-1"] * 10
    assert route.call_count == 1


@respx.mock
async def test_invalidate_forces_a_new_token(clock):
    route = respx.post(TOKEN_URL).mock(
        side_effect=[token_response("tok-1"), token_response("tok-2")]
    )
    async with httpx.AsyncClient() as http:
        tm = make_manager(http, clock)
        assert await tm.get_token() == "tok-1"
        tm.invalidate("tok-1")
        assert await tm.get_token() == "tok-2"
    assert route.call_count == 2


@respx.mock
async def test_invalidate_of_a_stale_token_keeps_the_newer_one(clock):
    """Two calls both got a 401 with tok-1; the first already refreshed to tok-2.
    The second must not throw tok-2 away."""
    route = respx.post(TOKEN_URL).mock(
        side_effect=[token_response("tok-1"), token_response("tok-2")]
    )
    async with httpx.AsyncClient() as http:
        tm = make_manager(http, clock)
        await tm.get_token()
        tm.invalidate("tok-1")
        assert await tm.get_token() == "tok-2"
        tm.invalidate("tok-1")
        assert await tm.get_token() == "tok-2"
    assert route.call_count == 2


@pytest.mark.parametrize("status", [400, 401, 403])
@respx.mock
async def test_rejected_credentials_raise_auth_error_without_secret(clock, status):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(status, json={"error": "invalid_client", "echo": FAKE_SECRET})
    )
    async with httpx.AsyncClient() as http:
        tm = make_manager(http, clock)
        with pytest.raises(OpenSkyAuthError) as exc:
            await tm.get_token()
    assert str(exc.value) == "OpenSky rejected the API client credentials"
    assert FAKE_SECRET not in repr(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


@respx.mock
async def test_token_endpoint_outage_is_a_retryable_auth_error(clock):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as http:
        tm = make_manager(http, clock)
        with pytest.raises(OpenSkyAuthError) as exc:
            await tm.get_token()
    assert "token service" in str(exc.value)
    assert "503" in str(exc.value)


def test_repr_never_shows_secret_or_token(clock):
    tm = make_manager(httpx.AsyncClient(), clock)
    tm._token = "tok-live-123"  # simulate a cached token
    text = repr(tm) + str(tm) + repr(vars(tm))
    assert FAKE_SECRET not in text
    assert "tok-live-123" not in repr(tm) + str(tm)
