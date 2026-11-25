"""JSON-lines logging to stderr with a redaction safety net.

The first line of defence is that nothing logs headers, bodies or the token request
(see `http_event_hooks`). This module is the second: every finished log line, including
tracebacks and extra fields, passes through `redact()` before it is written.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from datetime import UTC, datetime
from typing import IO, Any

import httpx

REDACTED = "[REDACTED]"

_PATTERNS = [
    # Authorization: Bearer xxx  /  'authorization': 'Bearer xxx'
    re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?)[^'\",}\s][^'\",}]*"),
    # access_token / client_secret / refresh_token in JSON, form or kwargs style
    re.compile(
        r"(?i)((?:access_token|refresh_token|id_token|client_secret)['\"]?\s*[:=]\s*['\"]?)"
        r"[^'\"&,}\s]+"
    ),
    # a bare bearer token anywhere
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/]+=*"),
]

_secrets: set[str] = set()

# Standard LogRecord attributes; everything else on a record came in via `extra=`.
_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}


def register_secret(value: str) -> None:
    """Mask this exact value wherever it appears in a log line (e.g. the client secret)."""
    if value and len(value) >= 4:
        _secrets.add(value)


def redact(text: str) -> str:
    for secret in _secrets:
        if secret in text:
            text = text.replace(secret, REDACTED)
    for pat in _PATTERNS:
        text = pat.sub(lambda m: m.group(1) + REDACTED, text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        # Redact each value before JSON escaping (escaped quotes would hide key=value
        # patterns), then the finished line once more.
        entry = {k: redact(v) if isinstance(v, str) else v for k, v in entry.items()}
        return redact(json.dumps(entry, default=lambda o: redact(str(o)), ensure_ascii=False))


def configure_logging(stream: IO[str] | None = None, level: str = "INFO") -> None:
    """Install one redacting JSON handler on the root logger (replacing any others).

    Call this before constructing the MCPServer: the SDK's own logging setup uses
    logging.basicConfig, which does nothing once the root logger has a handler.
    """
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)
    # httpx logs the full URL at INFO and httpcore logs connection internals at DEBUG.
    # We log requests ourselves via http_event_hooks.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


_http_log = logging.getLogger("aviation_mcp.http")


async def _on_request(request: httpx.Request) -> None:
    request.extensions["aviation_mcp_t0"] = time.perf_counter()


async def _on_response(response: httpx.Response) -> None:
    req = response.request
    t0 = req.extensions.get("aviation_mcp_t0")
    latency_ms = round((time.perf_counter() - t0) * 1000) if t0 else None
    # Method, host, path, status, latency. Never headers, query or bodies.
    _http_log.info(
        "upstream call",
        extra={
            "method": req.method,
            "host": req.url.host,
            "path": req.url.path,
            "upstream_status": response.status_code,
            "latency_ms": latency_ms,
        },
    )


def http_event_hooks() -> dict[str, list]:
    return {"request": [_on_request], "response": [_on_response]}
