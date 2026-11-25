"""demo/obo_demo.py's scripted sign-in, offline: what it sends to the authorization server.

A transport spy stands in for Keycloak. The MCP authorization spec requires the
`resource` parameter on both the authorization request and the token request.
"""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from demo import obo_demo


@pytest.fixture
def spy(monkeypatch):
    sent: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.path.endswith("/protocol/openid-connect/auth"):
            return httpx.Response(
                200, text='<form id="kc-form-login" action="http://localhost:8180/login"></form>'
            )
        if request.url.path == "/login":
            state = sent[0].url.params["state"]
            return httpx.Response(
                302, headers={"location": f"{obo_demo.CLI_REDIRECT}?code=c-1&state={state}"}
            )
        if request.url.path.endswith("/protocol/openid-connect/token"):
            return httpx.Response(200, json={"access_token": "DEMO-ONLY-spy-access-token"})
        raise AssertionError(f"unexpected request {request.url}")

    def client(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(obo_demo.httpx, "AsyncClient", client)
    return sent


async def test_resource_is_sent_on_the_authorization_and_the_token_request(spy):
    token = await obo_demo.sign_in(obo_demo.CLI_CLIENT, obo_demo.CLI_REDIRECT, "pilot-a",
                                   "mcp:tools mcp:logbook")
    assert token == "DEMO-ONLY-spy-access-token"
    auth, _login, tok = spy
    assert auth.url.params["resource"] == obo_demo.RESOURCE == "http://localhost:8000/mcp"
    assert auth.url.params["code_challenge_method"] == "S256"
    form = {k: v[0] for k, v in parse_qs(tok.content.decode()).items()}
    assert form["resource"] == obo_demo.RESOURCE
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "c-1" and form["code_verifier"]
    assert form["redirect_uri"] == obo_demo.CLI_REDIRECT
