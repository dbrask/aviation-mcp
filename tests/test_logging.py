"""Redacting JSON-lines logger. Logs go to stderr because stdout carries MCP."""

from __future__ import annotations

import io
import json
import logging

import pytest

from aviation_mcp.logs import REDACTED, JsonFormatter, configure_logging, redact, register_secret


@pytest.fixture
def captured():
    buf = io.StringIO()
    configure_logging(stream=buf, level="DEBUG")
    yield buf
    configure_logging(stream=io.StringIO(), level="WARNING")


def lines(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def test_log_lines_are_json_with_structured_fields(captured):
    logging.getLogger("aviation_mcp.test").info(
        "tool call", extra={"tool": "get_metar", "latency_ms": 12, "cache_hit": False}
    )
    [rec] = lines(captured)
    assert rec["msg"] == "tool call"
    assert rec["level"] == "INFO"
    assert rec["tool"] == "get_metar"
    assert rec["latency_ms"] == 12
    assert rec["cache_hit"] is False


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Bearer eyJhbGciOi.abc.def",
        "headers={'authorization': 'Bearer eyJhbGciOi.abc.def'}",
        '{"access_token": "eyJhbGciOi.abc.def", "expires_in": 1800}',
        "access_token=eyJhbGciOi.abc.def&token_type=Bearer",
        "grant_type=client_credentials&client_id=x&client_secret=eyJhbGciOi.abc.def",
        '{"client_secret": "eyJhbGciOi.abc.def"}',
        "sending Bearer eyJhbGciOi.abc.def upstream",
    ],
)
def test_redact_masks_credential_patterns(text):
    out = redact(text)
    assert "eyJhbGciOi.abc.def" not in out
    assert REDACTED in out


def test_redact_leaves_ordinary_text_alone():
    text = "GET aviationweather.gov /api/data/metar 200 in 84 ms"
    assert redact(text) == text


def test_registered_secret_is_masked_anywhere(captured):
    register_secret("plain-looking-value-123")
    logging.getLogger("aviation_mcp.test").warning("config dump: %s", {"x": "plain-looking-value-123"})
    assert "plain-looking-value-123" not in captured.getvalue()
    assert REDACTED in captured.getvalue()


def test_secret_in_exception_traceback_is_masked(captured):
    log = logging.getLogger("aviation_mcp.test")
    try:
        raise RuntimeError("upstream said: Authorization: Bearer eyJleak.xyz")
    except RuntimeError:
        log.exception("boom")
    out = captured.getvalue()
    assert "eyJleak.xyz" not in out
    assert "boom" in out


def test_secret_in_extra_field_is_masked(captured):
    logging.getLogger("aviation_mcp.test").info("req", extra={"headers": "Bearer eyJleak.xyz"})
    assert "eyJleak.xyz" not in captured.getvalue()


def test_root_has_only_the_redacting_handler(captured):
    # pytest adds its own LogCaptureHandlers during a test; ignore those.
    ours = [h for h in logging.getLogger().handlers if "LogCapture" not in type(h).__name__]
    assert len(ours) == 1
    assert isinstance(ours[0].formatter, JsonFormatter)
    # httpx's own INFO line prints the full URL; we log requests ourselves instead.
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
