"""Demo logbook API: the downstream resource server for `make demo-obo`. DEMO ONLY.

It accepts only exchanged tokens: audience `logbook-api`, scope `logbook:read`, minted for
the client `aviation-mcp` (`azp`). It returns the rows of the token's `sub` and nothing
else. For the demo's evidence it records, per request, the token's `jti`, `aud`, `azp`,
`scope`, a per-process pseudonym of the subject and the first 12 hex digits of the token's
SHA-256, never the token itself, on `GET /_demo/seen`. Binds to 127.0.0.1 only.

    uv run python -m demo.logbook_api --issuer http://localhost:8180/realms/aviation-demo
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import jwt
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from aviation_mcp.jwt_verify import JwksClient, JwtVerifier
from aviation_mcp.logs import configure_logging, subject_pseudonym

AUDIENCE = "logbook-api"
REQUIRED_SCOPE = "logbook:read"
REQUIRED_AZP = "aviation-mcp"
DATA_FILE = Path(__file__).with_name("logbook_data.json")


def _error(status: int, error: str, extra: str = "") -> JSONResponse:
    return JSONResponse(
        {"error": error},
        status_code=status,
        headers={"www-authenticate": f'Bearer error="{error}"{extra}'},
    )


def create_logbook_app(verifier: JwtVerifier, data: dict[str, dict[str, Any]]) -> Starlette:
    seen: list[dict[str, Any]] = []

    async def authorize(request: Request) -> dict[str, Any] | JSONResponse:
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return _error(401, "invalid_token")
        token = header[7:]
        try:
            claims = await verifier.decode(token)
        except jwt.PyJWTError:
            return _error(401, "invalid_token")
        if REQUIRED_SCOPE not in str(claims.get("scope", "")).split():
            return _error(403, "insufficient_scope", f', scope="{REQUIRED_SCOPE}"')
        if claims.get("azp") != REQUIRED_AZP:
            return _error(403, "insufficient_scope", ', error_description="wrong client"')
        seen.append(
            {
                "path": request.url.path,
                "jti": claims.get("jti"),
                "aud": claims.get("aud"),
                "azp": claims.get("azp"),
                "scope": claims.get("scope"),
                "subject": subject_pseudonym(claims["iss"], claims["sub"]),
                "token_sha256_12": hashlib.sha256(token.encode()).hexdigest()[:12],
            }
        )
        return claims

    def rows(kind: str):
        async def endpoint(request: Request) -> JSONResponse:
            claims = await authorize(request)
            if isinstance(claims, JSONResponse):
                return claims
            return JSONResponse({kind: data.get(claims["sub"], {}).get(kind, [])})

        return endpoint

    async def demo_seen(_request: Request) -> JSONResponse:
        return JSONResponse({"seen": seen})

    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return Starlette(
        routes=[
            Route("/me/airports", rows("airports")),
            Route("/me/flights", rows("flights")),
            Route("/_demo/seen", demo_seen),
            Route("/healthz", healthz),
        ]
    )


async def _serve(issuer: str, port: int) -> None:
    import uvicorn

    async with httpx.AsyncClient(timeout=10.0) as http:
        doc = (await http.get(f"{issuer}/.well-known/openid-configuration")).json()
        if doc["issuer"] != issuer:
            raise SystemExit(f"discovery names issuer {doc['issuer']!r}, expected {issuer!r}")
        verifier = JwtVerifier(JwksClient(http, doc["jwks_uri"]), issuer=issuer, audience=AUDIENCE)
        app = create_logbook_app(verifier, json.loads(DATA_FILE.read_text())["users"])
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None)
        await uvicorn.Server(config).serve()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    configure_logging(level="INFO")
    asyncio.run(_serve(args.issuer, args.port))


if __name__ == "__main__":
    main()
