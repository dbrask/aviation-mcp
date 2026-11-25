"""`make demo-obo`: on-behalf-of token exchange end to end against Keycloak. DEMO ONLY.

Expects Keycloak from demo/compose.yaml on http://localhost:8180 (the Makefile starts it
and always tears it down). This script starts the logbook API and the MCP server over
HTTP as local subprocesses, signs the demo users in with authorization code + PKCE by
posting Keycloak's login form, runs the checks, prints PASS/FAIL per check and exits
non-zero if any check failed. Tokens are never printed: only `aud`, `jti`, `scope`, and a
12-hex-digit SHA-256 fingerprint.

All users, passwords and the client secret are throwaway demo values from
demo/keycloak/aviation-demo-realm.json.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import httpx2
import jwt
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

ISSUER = "http://localhost:8180/realms/aviation-demo"
RESOURCE = "http://localhost:8000/mcp"
LOGBOOK = "http://localhost:8001"
PROTOCOL = "2026-07-28"
CLI_CLIENT, CLI_REDIRECT = "aviation-demo-cli", "http://127.0.0.1:8765/callback"
OTHER_CLIENT, OTHER_REDIRECT = "other-app", "http://127.0.0.1:8766/callback"
# DEMO ONLY: throwaway values that also appear in the realm import.
DEMO_EXCHANGE_SECRET = "DEMO-ONLY-aviation-mcp-not-a-secret"
USERS = {"pilot-a": "DEMO-ONLY-pilot-a", "pilot-b": "DEMO-ONLY-pilot-b"}
ROOT = Path(__file__).resolve().parent.parent

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, *evidence: str) -> None:
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    for line in evidence:
        print(f"       {line}")


def fp(token: str) -> str:
    """A token fingerprint that is safe to print."""
    return f"sha256:{hashlib.sha256(token.encode()).hexdigest()[:12]} (len {len(token)})"


def claims(token: str) -> dict[str, Any]:
    return jwt.decode(token, options={"verify_signature": False})


async def wait_for(url: str, what: str, timeout_s: float = 180.0) -> None:
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient(timeout=5.0) as http:
        while True:
            try:
                if (await http.get(url)).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise SystemExit(f"demo: {what} did not come up at {url} within {timeout_s:.0f} s")
            await asyncio.sleep(1.0)


async def sign_in(client_id: str, redirect: str, user: str, scope: str | None) -> str:
    """Authorization code + PKCE S256 through Keycloak's login form; returns the access token.

    A scripted login against one known, fixed realm, not an MCP client: it does no
    protected-resource or authorization-server discovery, does not check that the server
    advertises S256, and does not validate `state` or `iss` on the callback (it would accept
    wrong ones). `resource` is sent on both the authorization and the token request."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect,
        "code_challenge": challenge.rstrip(b"=").decode(),
        "code_challenge_method": "S256",
        "state": secrets.token_urlsafe(16),
        "resource": RESOURCE,  # MCP clients must send it; Keycloak ignores it (see OBO.md)
    }
    if scope:
        params["scope"] = scope
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as http:
        page = await http.get(f"{ISSUER}/protocol/openid-connect/auth", params=params)
        match = re.search(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', page.text)
        if page.status_code != 200 or not match:
            raise SystemExit(f"demo: Keycloak login page for {client_id} not found "
                             f"(HTTP {page.status_code})")
        # Keycloak marks its session cookies Secure even on http://localhost; browsers treat
        # localhost as a secure context, httpx does not. Carry them by hand.
        cookie = "; ".join(f"{c.name}={c.value}" for c in page.cookies.jar)
        done = await http.post(
            html.unescape(match.group(1)),
            data={"username": user, "password": USERS[user], "credentialId": ""},
            headers={"cookie": cookie},
        )
        location = done.headers.get("location", "")
        code = parse_qs(urlparse(location).query).get("code", [None])[0]
        if done.status_code != 302 or not location.startswith(redirect) or not code:
            raise SystemExit(f"demo: sign-in of {user} at {client_id} failed "
                             f"(HTTP {done.status_code})")
        tok = await http.post(
            f"{ISSUER}/protocol/openid-connect/token",
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
                  "client_id": client_id, "code_verifier": verifier,
                  "resource": RESOURCE},  # required on the token request too (OBO.md)
        )
        if tok.status_code != 200:
            raise SystemExit(f"demo: code exchange for {user} failed (HTTP {tok.status_code})")
        return tok.json()["access_token"]


async def sdk_call(token: str, tool: str, args: dict | None = None) -> Any:
    """One tool call through the MCP SDK client over Streamable HTTP."""
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30.0)
    async with http, Client(streamable_http_client(RESOURCE, http_client=http)) as client:
        return await client.call_tool(tool, args or {})


async def raw_call(token: str | None, tool: str) -> httpx.Response:
    """A raw tools/call, to show status codes and WWW-Authenticate for refusals."""
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "mcp-protocol-version": PROTOCOL,
        "mcp-method": "tools/call",
        "mcp-name": tool,
    }
    if token:
        headers["authorization"] = f"Bearer {token}"
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": {}, "_meta": {
            "io.modelcontextprotocol/protocolVersion": PROTOCOL,
            "io.modelcontextprotocol/clientCapabilities": {}}},
    }
    async with httpx.AsyncClient(timeout=30.0) as http:
        return await http.post(RESOURCE, json=body, headers=headers)


def start(args: list[str], log_path: Path, env: dict[str, str]) -> subprocess.Popen:
    log = log_path.open("w")
    return subprocess.Popen([sys.executable, *args], cwd=ROOT, env=env, stdout=log,
                            stderr=subprocess.STDOUT)


async def run_checks(logdir: Path) -> None:
    print("Signing in (authorization code + PKCE S256 via Keycloak's login form) ...")
    token_a = await sign_in(CLI_CLIENT, CLI_REDIRECT, "pilot-a", "mcp:tools mcp:logbook")
    token_b = await sign_in(CLI_CLIENT, CLI_REDIRECT, "pilot-b", "mcp:tools mcp:logbook")
    token_b_tools = await sign_in(CLI_CLIENT, CLI_REDIRECT, "pilot-b", "mcp:tools")
    token_other = await sign_in(OTHER_CLIENT, OTHER_REDIRECT, "pilot-a", None)
    a = claims(token_a)
    print(f"  pilot-a inbound token {fp(token_a)}: aud={a['aud']} scope={a['scope']!r}")
    print()

    # (0) no token
    r = await raw_call(None, "my_saved_airports")
    www = r.headers.get("www-authenticate", "")
    check("0 no token -> 401 invalid_token with resource_metadata",
          r.status_code == 401 and 'error="invalid_token"' in www and "resource_metadata=" in www,
          f"HTTP {r.status_code}  WWW-Authenticate: {www}")

    # (1), (2) each user gets their own data through the SDK client
    for n, (label, token) in enumerate((("pilot-a", token_a), ("pilot-b", token_b)), start=1):
        res = await sdk_call(token, "my_saved_airports")
        flights = await sdk_call(token, "my_recent_flights", {"limit": 2})
        airports = (res.structured_content or {}).get("airports", [])
        want = {"pilot-a": ["ESSA", "ESGG"], "pilot-b": ["EKCH"]}[label]
        got = [x["icao"] for x in airports]
        check(f"{n} {label} gets {label}'s data via the MCP HTTP endpoint",
              not res.is_error and not flights.is_error and got == want,
              f"my_saved_airports -> {json.dumps(airports)}",
              f"my_recent_flights(limit=2) -> "
              f"{json.dumps(flights.structured_content, separators=(',', ':'))}")

    # (3) missing scope
    r = await raw_call(token_b_tools, "my_saved_airports")
    www = r.headers.get("www-authenticate", "")
    check("3 pilot-b with only mcp:tools -> 403 insufficient_scope naming the scope",
          r.status_code == 403 and 'error="insufficient_scope"' in www
          and 'scope="mcp:tools mcp:logbook"' in www,
          f"token scope={claims(token_b_tools)['scope']!r}",
          f"HTTP {r.status_code}  WWW-Authenticate: {www}")

    # (4) wrong audience
    r = await raw_call(token_other, "get_metar")
    check("4 token for another app (wrong audience) -> 401 invalid_token",
          r.status_code == 401 and 'error="invalid_token"' in r.headers.get("www-authenticate", ""),
          f"token aud={claims(token_other).get('aud')!r} azp={claims(token_other)['azp']!r}",
          f"HTTP {r.status_code}  WWW-Authenticate: {r.headers.get('www-authenticate', '')}")

    # (5) what the logbook API received: run one more pilot-a call, then read its record
    await sdk_call(token_a, "my_saved_airports")
    async with httpx.AsyncClient(timeout=10.0) as http:
        seen = (await http.get(f"{LOGBOOK}/_demo/seen")).json()["seen"]
    last = seen[-1]
    inbound_fp = hashlib.sha256(token_a.encode()).hexdigest()[:12]
    check("5 logbook API got an exchanged token (aud=logbook-api), never the inbound one",
          last["aud"] == "logbook-api" and last["azp"] == "aviation-mcp"
          and last["scope"] == "logbook:read" and last["jti"] != a.get("jti")
          and last["token_sha256_12"] != inbound_fp
          and inbound_fp not in {s["token_sha256_12"] for s in seen},
          f"inbound  (client -> MCP): aud={a['aud']} azp={a['azp']} jti={a.get('jti')} "
          f"sha256={inbound_fp}",
          f"exchanged (MCP -> API):   aud={last['aud']!r} azp={last['azp']} jti={last['jti']} "
          f"sha256={last['token_sha256_12']} scope={last['scope']!r}",
          f"token strings differ: {last['token_sha256_12'] != inbound_fp}; inbound hash among "
          f"{len(seen)} API requests: {inbound_fp in {s['token_sha256_12'] for s in seen}}",
          f"distinct exchanged tokens seen by the API: "
          f"{len({s['token_sha256_12'] for s in seen})}, subjects: "
          f"{len({s['subject'] for s in seen})}")

    # (6) the inbound token sent straight to the logbook API
    async with httpx.AsyncClient(timeout=10.0) as http:
        r = await http.get(f"{LOGBOOK}/me/airports", headers={"authorization": f"Bearer {token_a}"})
    check("6 pilot-a's inbound token sent straight to the logbook API -> 401",
          r.status_code == 401, f"HTTP {r.status_code}  WWW-Authenticate: "
          f"{r.headers.get('www-authenticate', '')}")

    # (7) no token in the server logs
    logs = "".join(p.read_text() for p in logdir.glob("*.log"))
    leaked = [t for t in (token_a, token_b, token_b_tools, token_other, DEMO_EXCHANGE_SECRET)
              if t in logs or t.split(".")[-1] in logs]
    check("7 no inbound token, signature or client secret in the MCP or API logs",
          not leaked and len(logs) > 0, f"{len(logs.splitlines())} log lines searched")


async def main() -> int:
    print("Waiting for Keycloak (realm aviation-demo) ...")
    await wait_for(f"{ISSUER}/.well-known/openid-configuration", "Keycloak")
    logdir = Path(tempfile.mkdtemp(prefix="aviation-mcp-demo-"))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENSKY_", "AVIATION_MCP_"))}
    env["AVIATION_MCP_CLIENT_SECRET"] = DEMO_EXCHANGE_SECRET  # DEMO ONLY
    procs = [
        start(["-m", "demo.logbook_api", "--issuer", ISSUER], logdir / "logbook-api.log", env),
        start(["-m", "aviation_mcp", "--transport", "http", "--issuer", ISSUER,
               "--resource-url", RESOURCE, "--logbook-url", LOGBOOK,
               "--credentials-file", str(logdir / "no-opensky.env"),
               "--client-credentials-file", str(logdir / "no-file.env")],
              logdir / "aviation-mcp.log", env),
    ]
    async with AsyncExitStack() as stack:
        stack.callback(lambda: [p.terminate() for p in procs] and [p.wait(10) for p in procs])
        await wait_for(f"{LOGBOOK}/healthz", "logbook API", 30)
        await wait_for("http://localhost:8000/healthz", "aviation-mcp HTTP server", 30)
        await run_checks(logdir)
    failed = [name for name, ok in results if not ok]
    print()
    print(f"{len(results) - len(failed)}/{len(results)} checks passed. Logs: {logdir}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
