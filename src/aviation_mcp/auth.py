"""OAuth2 client-credentials token for the OpenSky REST API.

The token lives in memory only. It is refreshed `refresh_margin_s` before it expires,
and an asyncio.Lock makes N concurrent tool calls trigger one refresh, not N.
Nothing in this module logs or formats the secret or the token.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import httpx
from pydantic import SecretStr

TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
)
DEFAULT_EXPIRES_IN_S = 1800  # OpenSky tokens expire after 30 minutes


class OpenSkyAuthError(Exception):
    """Token request failed. The message is safe to show to a model: it never
    contains the client secret, the token, or the token endpoint's response body."""


class TokenManager:
    def __init__(
        self,
        http: httpx.AsyncClient,
        client_id: str,
        client_secret: SecretStr,
        *,
        token_url: str = TOKEN_URL,
        clock: Callable[[], float] = time.monotonic,
        refresh_margin_s: float = 60.0,
    ) -> None:
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_url = token_url
        self._clock = clock
        self._margin = refresh_margin_s
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        state = "cached" if self._token else "empty"
        return f"TokenManager(client_id={self._client_id!r}, token=<{state}>)"

    __str__ = __repr__

    def _valid(self) -> bool:
        return self._token is not None and self._clock() < self._expires_at - self._margin

    async def get_token(self) -> str:
        if self._valid():
            return self._token  # type: ignore[return-value]
        async with self._lock:
            # Another caller may have refreshed while we waited for the lock.
            if not self._valid():
                await self._refresh()
            return self._token  # type: ignore[return-value]

    def invalidate(self, token: str) -> None:
        """Drop `token` after the API answered 401. If a newer token has already
        replaced it, keep the newer one."""
        if self._token == token:
            self._token = None
            self._expires_at = 0.0

    async def _refresh(self) -> None:
        try:
            resp = await self._http.post(
                self._token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret.get_secret_value(),
                },
            )
        except httpx.HTTPError:
            raise OpenSkyAuthError(
                "Could not reach the OpenSky token service (network error). Try again later."
            ) from None
        if resp.status_code in (400, 401, 403):
            raise OpenSkyAuthError("OpenSky rejected the API client credentials") from None
        if resp.status_code != 200:
            raise OpenSkyAuthError(
                f"The OpenSky token service failed (HTTP {resp.status_code}). Try again later."
            ) from None
        try:
            body = resp.json()
            token = body["access_token"]
            expires_in = float(body.get("expires_in", DEFAULT_EXPIRES_IN_S))
        except (ValueError, KeyError, TypeError):
            raise OpenSkyAuthError(
                "The OpenSky token service returned an unexpected response."
            ) from None
        self._token = token
        self._expires_at = self._clock() + expires_in
