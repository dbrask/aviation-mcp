"""Slice 2 review fixes R1-R4, through the real HTTP app (httpx.ASGITransport, no sockets).

R1: an `Mcp-Name` sent in the `=?base64?...?=` form gets the same per-tool 403 as a plain one.
R2: my_saved_airports returns at most 50 rows and says how many there were.
R3: a 200 from the logbook API without the endpoint's list is an error, not "no records".
R4: a rejected argument value is never echoed into the model-visible error (a caller can
    paste a bearer token into any string argument), with normal log redaction on.
"""

from __future__ import annotations

import base64
import io
import json

import httpx
import pytest

from aviation_mcp import logs
from aviation_mcp import server as server_module
from aviation_mcp.logs import configure_logging
from tests import test_http_app as http_app_tests
from tests.oauth_helpers import LOGBOOK_URL
from tests.test_http_app import AIRPORTS, PRM_URL, call_tool, result_of, rpc, text_of

# The real-app fixtures of test_http_app, reused under the same names.
idp, fake, upstream, asgi = (
    http_app_tests.idp, http_app_tests.fake, http_app_tests.upstream, http_app_tests.asgi
)


def b64_name(name: str) -> str:
    return f"=?base64?{base64.b64encode(name.encode()).decode()}?="


# --- R1: encoded Mcp-Name ------------------------------------------------------------------


async def test_encoded_mcp_name_without_logbook_scope_gets_the_403_challenge(asgi, idp, fake):
    resp = await call_tool(asgi, "my_saved_airports", token=idp.mint(scope="mcp:tools"),
                           name=b64_name("my_saved_airports"))
    assert resp.status_code == 403
    www = resp.headers["www-authenticate"]
    assert 'error="insufficient_scope"' in www
    assert 'scope="mcp:tools mcp:logbook"' in www
    assert f'resource_metadata="{PRM_URL}"' in www
    assert fake.exchanges == []


async def test_encoded_mcp_name_with_both_scopes_succeeds(asgi, idp, fake):
    resp = await call_tool(asgi, "my_saved_airports", token=idp.mint(),
                           name=b64_name("my_saved_airports"))
    result = result_of(resp)
    assert not result["isError"]
    assert result["structuredContent"]["airports"] == [
        {"icao": "ESSA", "label": "home base"}, {"icao": "ESGG", "label": "favourite"}
    ]
    assert len(fake.exchanges) == 1


async def test_encoded_mcp_name_for_recent_flights_is_gated_too(asgi, idp, fake):
    resp = await call_tool(asgi, "my_recent_flights", token=idp.mint(scope="mcp:tools"),
                           name=b64_name("my_recent_flights"))
    assert resp.status_code == 403
    assert 'scope="mcp:tools mcp:logbook"' in resp.headers["www-authenticate"]
    assert fake.exchanges == []


@pytest.mark.parametrize(
    "header",
    [
        "=?base64?not base64!?=",           # bad base64
        "=?base64?bXlfc2F2ZWRfYWlycG9ydHN=?=",  # non-canonical (trailing bits)
        "=?base64?/w==?=",                  # not UTF-8
    ],
)
async def test_malformed_encoded_mcp_name_is_still_rejected_by_the_sdk(asgi, idp, fake, header):
    resp = await call_tool(asgi, "my_saved_airports", token=idp.mint(scope="mcp:tools"),
                           name=header)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32020
    assert fake.exchanges == []


async def test_encoded_mcp_name_that_disagrees_with_body_is_rejected(asgi, idp, fake):
    resp = await call_tool(asgi, "my_saved_airports", token=idp.mint(),
                           name=b64_name("get_metar"))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32020
    assert fake.exchanges == []


# --- R2: saved airports are capped -----------------------------------------------------------


def _airports(n: int) -> list[dict]:
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return [
        {"icao": "E" + letters[i // 676 % 26] + letters[i // 26 % 26] + letters[i % 26],
         "label": f"saved airport number {i:04d} with a long user label".ljust(64, ".")}
        for i in range(n)
    ]


@pytest.fixture
def many_airports():
    def put(n: int) -> str:
        AIRPORTS["many-sub"] = _airports(n)
        return "many-sub"

    yield put
    AIRPORTS.pop("many-sub", None)


async def test_saved_airports_cap_is_fifty():
    assert server_module.SAVED_AIRPORTS_MAX == 50


async def test_exactly_fifty_saved_airports_are_all_returned(asgi, idp, many_airports):
    sub = many_airports(50)
    sc = result_of(await call_tool(asgi, "my_saved_airports", token=idp.mint(sub=sub)))[
        "structuredContent"]
    assert (sc["count_total"], sc["returned"], sc["truncated"]) == (50, 50, False)
    assert sc["airports"] == AIRPORTS[sub]


@pytest.mark.parametrize("n", [51, 2000])
async def test_more_than_fifty_saved_airports_are_truncated_and_bounded(asgi, idp, many_airports, n):
    sub = many_airports(n)
    resp = await call_tool(asgi, "my_saved_airports", token=idp.mint(sub=sub))
    result = result_of(resp)
    sc = result["structuredContent"]
    assert (sc["count_total"], sc["returned"], sc["truncated"]) == (n, 50, True)
    assert sc["airports"] == AIRPORTS[sub][:50]  # upstream order kept
    assert json.loads(text_of(result)) == sc
    # 50 rows with 64-character labels, as structuredContent and again as text.
    assert len(resp.content) < 16_000


async def test_saved_airports_output_schema_has_the_totals(asgi, idp):
    tools = result_of(await rpc(asgi, "tools/list", token=idp.mint()))["tools"]
    schema = next(t for t in tools if t["name"] == "my_saved_airports")["outputSchema"]
    assert {"airports", "count_total", "returned", "truncated"} <= set(schema["required"])
    assert schema["properties"]["airports"]["maxItems"] == 50


# --- R3: malformed logbook envelopes -----------------------------------------------------------

SENTINEL = "UPSTREAM-BODY-SENTINEL-4b1d"
BAD_VALUES = {
    "missing": None,  # the field is left out
    "null": "null",
    "false": "false",
    "zero": "0",
    "string": json.dumps(SENTINEL),
    "object": json.dumps({"icao": "ESSA", "note": SENTINEL}),
}


def _envelope(field: str, case: str) -> bytes:
    body = {"note": SENTINEL}
    raw = json.dumps(body)
    if BAD_VALUES[case] is None:
        return raw.encode()
    return (raw[:-1] + f', "{field}": {BAD_VALUES[case]}}}').encode()


@pytest.mark.parametrize("case", list(BAD_VALUES))
@pytest.mark.parametrize(
    "tool,field", [("my_saved_airports", "airports"), ("my_recent_flights", "flights")]
)
async def test_logbook_200_without_the_list_is_a_tool_error_not_empty_data(
    asgi, idp, upstream, tool, field, case
):
    body = _envelope(field, case)
    upstream.get(url__startswith=LOGBOOK_URL).mock(
        return_value=httpx.Response(200, content=body, headers={"content-type": "application/json"})
    )
    result = result_of(await call_tool(asgi, tool, token=idp.mint()))
    assert result["isError"], f"{tool} with {case} {field} must not succeed"
    text = text_of(result)
    assert "unexpected" in text and "not an empty" in text
    assert SENTINEL not in text and "structuredContent" not in result


@pytest.mark.parametrize(
    "tool,field", [("my_saved_airports", "airports"), ("my_recent_flights", "flights")]
)
async def test_logbook_empty_list_is_a_real_empty_result(asgi, idp, upstream, tool, field):
    upstream.get(url__startswith=LOGBOOK_URL).mock(
        return_value=httpx.Response(200, json={field: []})
    )
    result = result_of(await call_tool(asgi, tool, token=idp.mint()))
    assert not result["isError"]
    sc = result["structuredContent"]
    assert sc[field] == [] and sc["returned"] == 0 and sc["count_total"] == 0


async def test_invalid_rows_are_still_dropped_one_by_one(asgi, idp, upstream):
    upstream.get(url__startswith=LOGBOOK_URL).mock(
        return_value=httpx.Response(200, json={"airports": [
            {"icao": "ESSA", "label": "ok"}, {"icao": "nope", "label": SENTINEL}, "row", None,
        ]})
    )
    result = result_of(await call_tool(asgi, "my_saved_airports", token=idp.mint()))
    assert not result["isError"]
    assert result["structuredContent"]["airports"] == [{"icao": "ESSA", "label": "ok"}]
    assert SENTINEL not in text_of(result)


# --- R4: rejected values are not echoed ----------------------------------------------------------


@pytest.fixture
def normal_logs():
    """Logging as the server runs it: redaction on (default patterns), at DEBUG."""
    buf = io.StringIO()
    configure_logging(stream=buf, level="DEBUG")
    yield buf
    configure_logging(stream=io.StringIO(), level="WARNING")


def _pieces(token: str) -> list[str]:
    """Every 12-character slice of the token at 6-character steps, so a truncated or partial
    echo (pydantic shortens long `input_value`s) is caught too."""
    return [token[i:i + 12] for i in range(0, len(token) - 12, 6)]


@pytest.mark.parametrize(
    "tool,args",
    [
        ("my_recent_flights", lambda t: {"since_utc": t}),
        ("my_recent_flights", lambda t: {"limit": t}),
        ("get_metar", lambda t: {"stations": [t]}),
        ("get_taf", lambda t: {"stations": [t]}),
        ("get_aircraft", lambda t: {"icao24": [t]}),
        ("aircraft_near_airport", lambda t: {"airport": t}),
        ("airport_flights", lambda t: {"airport": "ESSA", "direction": "arrivals", "date_utc": t}),
        ("airport_flights", lambda t: {"airport": "ESSA", "direction": t}),
    ],
)
async def test_a_bearer_token_passed_as_an_argument_is_not_echoed(
    asgi, idp, fake, normal_logs, tool, args
):
    token = idp.mint()
    resp = await call_tool(asgi, tool, args(token), token=token)
    result = result_of(resp)
    assert result["isError"]
    assert "input_value" not in resp.text
    leaked = [p for p in _pieces(token) if p in resp.text]
    assert not leaked, f"{tool} echoed parts of the token: {leaked[:3]}"
    assert token not in normal_logs.getvalue()
    assert fake.exchanges == []  # rejected before any exchange


async def test_since_utc_error_still_says_how_to_fix_the_call(asgi, idp):
    token = idp.mint()
    text = text_of(result_of(await call_tool(asgi, "my_recent_flights", {"since_utc": token},
                                             token=token)))
    assert "since_utc" in text and "YYYY-MM-DD" in text and "2026-11-24" in text


async def test_logbook_labels_are_returned_as_they_are(asgi, idp, upstream):
    # R4 is about echoed arguments, not user data: labels keep their exact text.
    label = "eyJ-looking label, the club's field"
    upstream.get(url__startswith=LOGBOOK_URL).mock(
        return_value=httpx.Response(200, json={"airports": [{"icao": "ESSA", "label": label}]})
    )
    result = result_of(await call_tool(asgi, "my_saved_airports", token=idp.mint()))
    assert result["structuredContent"]["airports"] == [{"icao": "ESSA", "label": label}]


def test_redaction_is_on_in_the_r4_fixture():
    assert logs._PATTERNS  # the default patterns, not the leak test's switched-off set
