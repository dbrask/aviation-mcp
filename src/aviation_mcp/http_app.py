"""Streamable HTTP app: aviation-mcp as an OAuth resource server.

The SDK does the endpoint-level work: `BearerAuthBackend` calls our `JwtVerifier` (401 on
any failure), `RequireAuthMiddleware` requires `mcp:tools` (403), and the Protected
Resource Metadata route is served from `AuthSettings`. The SDK has no per-tool scopes, so
`ToolScopeGate` answers a `tools/call` of a logbook tool without `mcp:logbook` with a 403
that names the scope to step up to. It reads the `Mcp-Name` header, decoded the way the
SDK decodes it (plain or `=?base64?...?=`), which the SDK refuses when it disagrees with
the request body. The tool re-checks the scope itself.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable, Mapping
from datetime import date
from typing import NoReturn

import httpx
from mcp.server import MCPServer
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.routes import build_resource_metadata_url
from mcp.server.auth.settings import AuthSettings
from mcp.shared.inbound import decode_header_value
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from aviation_mcp.awc import AwcClient
from aviation_mcp.config import EXCHANGE_SECRET_KEY, CredentialsError, load_exchange_credentials
from aviation_mcp.exchange import TokenExchanger
from aviation_mcp.jwt_verify import JwksClient, JwtVerifier
from aviation_mcp.logbook import LogbookClient
from aviation_mcp.opensky import OpenSkyClient
from aviation_mcp.server import (
    LOGBOOK_SCOPE,
    LOGBOOK_TOOLS,
    MCP_SCOPE,
    _utc_today,
    build_server,
    make_upstreams,
)

LOOPBACK = "127.0.0.1"
log = logging.getLogger("aviation_mcp.http")


class ToolScopeGate:
    """403 insufficient_scope for a `tools/call` whose tool needs scopes the token lacks."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        mcp_path: str,
        tool_scopes: Mapping[str, list[str]],
        resource_metadata_url: str,
    ) -> None:
        self.app = app
        self.mcp_path = mcp_path
        self.tool_scopes = dict(tool_scopes)
        self.resource_metadata_url = resource_metadata_url

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("method") == "POST" and (
            scope.get("path", "").rstrip("/") == self.mcp_path
        ):
            user = scope.get("user")
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            # The SDK's own decoding: a `=?base64?...?=` name is compared by its decoded
            # text. A malformed one decodes to None and the SDK answers it with 400.
            name = decode_header_value(headers.get("mcp-name"))
            needed = self.tool_scopes.get(name) if name is not None else None
            if (
                needed
                and headers.get("mcp-method") == "tools/call"
                and isinstance(user, AuthenticatedUser)  # else the SDK answers 401
                and not set(needed) <= set(user.scopes)
            ):
                await self._forbid(send, needed)
                return
        await self.app(scope, receive, send)

    async def _forbid(self, send: Send, needed: list[str]) -> None:
        scope_str = " ".join(needed)
        www = (
            f'Bearer error="insufficient_scope", scope="{scope_str}", '
            f'resource_metadata="{self.resource_metadata_url}", '
            f'error_description="This tool requires scope: {scope_str}"'
        )
        body = json.dumps(
            {"error": "insufficient_scope", "error_description": f"Required scope: {scope_str}"}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", www.encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def create_http_app(
    *,
    awc: AwcClient,
    opensky: OpenSkyClient | None,
    verifier: JwtVerifier,
    issuer: str,
    resource_url: str,
    logbook: LogbookClient | None,
    today: Callable[[], date] = _utc_today,
    host: str = LOOPBACK,
) -> Starlette:
    auth = AuthSettings(
        issuer_url=AnyHttpUrl(issuer),
        resource_server_url=AnyHttpUrl(resource_url),
        required_scopes=[MCP_SCOPE],
        validate_token_resource=True,
    )
    server = build_server(
        awc=awc, opensky=opensky, today=today, auth=auth, token_verifier=verifier, logbook=logbook
    )

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    mcp_path = (AnyHttpUrl(resource_url).path or "/mcp").rstrip("/") or "/mcp"
    app = server.streamable_http_app(
        streamable_http_path=mcp_path, json_response=True, stateless_http=True, host=host
    )
    tool_scopes = {name: [MCP_SCOPE, LOGBOOK_SCOPE] for name in LOGBOOK_TOOLS}
    # Appended last, so it runs inside the SDK's AuthenticationMiddleware (the user is
    # known) and before the endpoint's RequireAuthMiddleware. A test pins this order.
    app.user_middleware.append(
        Middleware(
            ToolScopeGate,
            mcp_path=mcp_path,
            tool_scopes=tool_scopes,
            resource_metadata_url=str(build_resource_metadata_url(AnyHttpUrl(resource_url))),
        )
    )
    app.state.mcp_server = server
    return app


# --- start-up from the command line --------------------------------------------------------


def _fail(message: str) -> NoReturn:
    print(f"aviation-mcp: {message}", file=sys.stderr)
    raise SystemExit(2)


async def discover(http: httpx.AsyncClient, issuer: str) -> tuple[str, str]:
    """(token_endpoint, jwks_uri) from the issuer's OIDC discovery document, whose
    `issuer` must equal the configured one exactly."""
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        resp = await http.get(url)
        resp.raise_for_status()
        doc = resp.json()
        found, token_url, jwks_url = doc["issuer"], doc["token_endpoint"], doc["jwks_uri"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        _fail(f"could not read OIDC discovery for {issuer} ({url})")
    if found != issuer:
        _fail(f"OIDC discovery names issuer {found!r}, but --issuer is {issuer!r}")
    return token_url, jwks_url


async def build_from_args(
    args: argparse.Namespace, env: Mapping[str, str] | None = None
) -> tuple[Starlette, httpx.AsyncClient, MCPServer]:
    """The HTTP app from command-line arguments. There is no unauthenticated HTTP mode."""
    for flag, value in (("--issuer", args.issuer), ("--resource-url", args.resource_url)):
        if not value:
            _fail(f"--transport http needs {flag}")
    try:
        exchange_creds = load_exchange_credentials(env=env, path=args.client_credentials_file)
    except CredentialsError as exc:
        _fail(str(exc))
    if args.logbook_url and exchange_creds is None:
        _fail(
            "--logbook-url needs the token-exchange client secret: set "
            f"{EXCHANGE_SECRET_KEY} or write it to {args.client_credentials_file} (mode 0600)"
        )
    http, awc, opensky = make_upstreams(args, env)
    try:
        token_url, jwks_url = await discover(http, args.issuer)
    except SystemExit:
        await http.aclose()
        raise
    verifier = JwtVerifier(JwksClient(http, jwks_url), issuer=args.issuer, audience=args.resource_url)
    logbook = None
    if args.logbook_url and exchange_creds is not None:
        exchanger = TokenExchanger(
            http, token_url, exchange_creds.client_id, exchange_creds.client_secret
        )
        logbook = LogbookClient(http, args.logbook_url, exchanger)
    app = create_http_app(
        awc=awc,
        opensky=opensky,
        verifier=verifier,
        issuer=args.issuer,
        resource_url=args.resource_url,
        logbook=logbook,
    )
    return app, http, app.state.mcp_server


async def serve_http(args: argparse.Namespace) -> None:
    import uvicorn

    app, http, _server = await build_from_args(args)
    # log_config=None: uvicorn logs through our redacting JSON handler on the root logger.
    config = uvicorn.Config(app, host=LOOPBACK, port=args.port, log_config=None)
    log.info("serving", extra={"transport": "http", "host": LOOPBACK, "port": args.port,
                               "resource": args.resource_url, "logbook": bool(args.logbook_url)})
    try:
        await uvicorn.Server(config).serve()
    finally:
        await http.aclose()
