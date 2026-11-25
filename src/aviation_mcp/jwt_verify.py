"""Bearer JWT validation for the HTTP transport (the SDK ships no JWT verifier).

`JwtVerifier` implements the SDK's `TokenVerifier` protocol: it returns an `AccessToken`
for a valid token and `None` for anything else, which the SDK turns into a 401. Checks:
RS256 only, signing key chosen by `kid` from the issuer's JWKS (`use=sig` keys only),
`iss`, `aud` contains our resource URI, `exp`/`nbf`/`iat` with 10 s leeway, `sub` present.

The JWKS is fetched with async httpx (PyJWT's PyJWKClient blocks the event loop) and
refetched at most once per `refetch_interval_s` when a token names an unknown `kid`.
Nothing here logs a token or any part of one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm
from mcp.server.auth.provider import AccessToken

ALGORITHMS = ["RS256"]
LEEWAY_S = 10
log = logging.getLogger("aviation_mcp.jwt")


class JwksClient:
    """The issuer's signing keys by `kid`, fetched lazily and cached in memory."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        jwks_url: str,
        *,
        clock: Callable[[], float] = time.monotonic,
        refetch_interval_s: float = 60.0,
    ) -> None:
        self._http = http
        self._url = jwks_url
        self._clock = clock
        self._interval = refetch_interval_s
        self._keys: dict[str, Any] = {}
        self._fetched_at: float | None = None
        self._lock = asyncio.Lock()

    async def key(self, kid: str) -> Any | None:
        if kid in self._keys:
            return self._keys[kid]
        async with self._lock:
            if kid not in self._keys and self._may_fetch():
                await self._fetch()
        return self._keys.get(kid)

    def _may_fetch(self) -> bool:
        return self._fetched_at is None or self._clock() - self._fetched_at >= self._interval

    async def _fetch(self) -> None:
        self._fetched_at = self._clock()
        try:
            resp = await self._http.get(self._url)
            resp.raise_for_status()
            keys = resp.json()["keys"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            log.warning("JWKS fetch failed", extra={"jwks_host": httpx.URL(self._url).host})
            return
        parsed: dict[str, Any] = {}
        for jwk in keys:
            if not isinstance(jwk, dict) or jwk.get("kty") != "RSA" or "kid" not in jwk:
                continue
            if jwk.get("use", "sig") != "sig" or jwk.get("alg", "RS256") not in ALGORITHMS:
                continue  # e.g. Keycloak's RSA-OAEP encryption key
            try:
                parsed[jwk["kid"]] = RSAAlgorithm.from_jwk(jwk)
            except (jwt.InvalidKeyError, ValueError, KeyError):
                continue
        self._keys = parsed


class JwtVerifier:
    """SDK `TokenVerifier`: a validated access token for `audience`, or None."""

    def __init__(
        self, jwks: JwksClient, *, issuer: str, audience: str, leeway_s: int = LEEWAY_S
    ) -> None:
        self._jwks = jwks
        self.issuer = issuer
        self.audience = audience
        self._leeway = leeway_s

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = await self.decode(token)
        except jwt.PyJWTError as exc:
            log.info("token rejected", extra={"reason": type(exc).__name__})
            return None
        scope = claims.get("scope")
        return AccessToken(
            token=token,
            client_id=str(claims.get("azp") or ""),
            scopes=scope.split() if isinstance(scope, str) else [],
            expires_at=int(claims["exp"]),
            resource=self.audience,  # checked above: aud contains it
            subject=str(claims["sub"]),
            claims={k: claims[k] for k in ("iss", "aud", "azp", "jti") if k in claims},
        )

    async def decode(self, token: str) -> dict[str, Any]:
        """Validated claims, or a jwt.PyJWTError naming what failed (never the token)."""
        header = jwt.get_unverified_header(token)
        if header.get("alg") not in ALGORITHMS:
            raise jwt.InvalidAlgorithmError("algorithm not allowed")
        kid = header.get("kid")
        if not isinstance(kid, str):
            raise jwt.InvalidTokenError("no kid")
        key = await self._jwks.key(kid)
        if key is None:
            raise jwt.InvalidKeyError("unknown kid")
        claims = jwt.decode(
            token,
            key,
            algorithms=ALGORITHMS,
            issuer=self.issuer,
            audience=self.audience,
            leeway=self._leeway,
            options={"require": ["iss", "aud", "exp", "sub"]},
        )
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise jwt.MissingRequiredClaimError("sub")
        return claims
