"""On-behalf-of token exchange (RFC 8693) and the per-user exchanged-token cache.

The caller's inbound token is never forwarded downstream. It is sent only to the
identity provider, as `subject_token`, in exchange for a short-lived token addressed to
the downstream API (`audience`) with a read-only `scope`.

Cache: memory only, keyed `(iss, sub, audience, scope)`, so one user's token is never
served to another. An entry lives until `min(exchanged expiry, inbound expiry) - margin`.
At most `max_entries` entries (least recently used goes first). One asyncio.Lock per key
makes concurrent calls by one user trigger one exchange. The inbound token is never cached.
Nothing here logs or formats a token, the client secret or the IdP's response body.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from mcp.server.auth.provider import AccessToken
from pydantic import SecretStr

GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"

SIGN_IN_AGAIN = (
    "The identity provider refused to issue a logbook token for this user. Ask the user to "
    "sign in again (with the mcp:logbook scope); do not retry with the same token."
)
UNAVAILABLE = "The identity provider is unavailable, so the logbook cannot be read. Try again later."

CacheKey = tuple[str, str, str, str]


class ExchangeError(Exception):
    """Exchange failed. The message is safe for a model: no token, secret or IdP body."""


@dataclass
class _Entry:
    token: str
    valid_until: float


class TokenExchanger:
    def __init__(
        self,
        http: httpx.AsyncClient,
        token_url: str,
        client_id: str,
        client_secret: SecretStr,
        *,
        audience: str = "logbook-api",
        scope: str = "logbook:read",
        clock: Callable[[], float] = time.time,
        margin_s: float = 30.0,
        max_entries: int = 1024,
    ) -> None:
        self._http = http
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self.audience = audience
        self.scope = scope
        self._clock = clock
        self._margin = margin_s
        self._max = max_entries
        self._cache: OrderedDict[CacheKey, _Entry] = OrderedDict()
        self._locks: dict[CacheKey, asyncio.Lock] = {}

    def __repr__(self) -> str:
        return (
            f"TokenExchanger(client_id={self._client_id!r}, audience={self.audience!r}, "
            f"scope={self.scope!r}, cached={len(self._cache)})"
        )

    __str__ = __repr__

    def __len__(self) -> int:
        return len(self._cache)

    def _key(self, inbound: AccessToken) -> CacheKey:
        iss = (inbound.claims or {}).get("iss")
        if not inbound.subject or not iss:
            raise ExchangeError(SIGN_IN_AGAIN)
        return (str(iss), inbound.subject, self.audience, self.scope)

    def _cached(self, key: CacheKey) -> str | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        if self._clock() >= entry.valid_until:
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return entry.token

    async def token_for(self, inbound: AccessToken) -> str:
        """An exchanged token for the user `inbound` belongs to."""
        key = self._key(inbound)
        token = self._cached(key)
        if token is not None:
            return token
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            token = self._cached(key)  # another call may have exchanged while we waited
            if token is not None:
                return token
            token, expires_in = await self._exchange(inbound.token)
            now = self._clock()
            until = now + expires_in
            if inbound.expires_at is not None:
                until = min(until, float(inbound.expires_at))
            until -= self._margin
            if until > now:
                self._cache[key] = _Entry(token, until)
                self._cache.move_to_end(key)
                while len(self._cache) > self._max:
                    old_key, _ = self._cache.popitem(last=False)
                    self._locks.pop(old_key, None)
            return token

    def evict(self, inbound: AccessToken, token: str) -> None:
        """Drop `token` after the downstream API answered 401. A newer token stays."""
        try:
            key = self._key(inbound)
        except ExchangeError:
            return
        entry = self._cache.get(key)
        if entry is not None and entry.token == token:
            del self._cache[key]

    async def _exchange(self, subject_token: str) -> tuple[str, float]:
        # RFC 6749 §2.3.1: form-urlencode the id and secret before Basic encoding.
        auth = httpx.BasicAuth(
            quote(self._client_id, safe=""),
            quote(self._client_secret.get_secret_value(), safe=""),
        )
        try:
            resp = await self._http.post(
                self._token_url,
                auth=auth,
                data={
                    "grant_type": GRANT_TYPE,
                    "subject_token": subject_token,
                    "subject_token_type": ACCESS_TOKEN_TYPE,
                    "requested_token_type": ACCESS_TOKEN_TYPE,
                    "audience": self.audience,
                    "scope": self.scope,
                },
            )
        except httpx.HTTPError:
            raise ExchangeError(UNAVAILABLE) from None
        if 400 <= resp.status_code < 500:
            raise ExchangeError(SIGN_IN_AGAIN) from None
        if resp.status_code != 200:
            raise ExchangeError(UNAVAILABLE) from None
        try:
            body = resp.json()
            token = body["access_token"]
            expires_in = float(body.get("expires_in", 60))
            if not isinstance(token, str) or not token:
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise ExchangeError(UNAVAILABLE) from None
        return token, expires_in
