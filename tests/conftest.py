"""Shared test helpers. The whole suite runs with the network blocked (pytest-socket,
see pyproject.toml addopts); every HTTP call is answered by respx from fixtures."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from aviation_mcp.auth import TokenManager
from aviation_mcp.awc import AwcClient
from aviation_mcp.opensky import OpenSkyClient
from aviation_mcp.server import build_server

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(rel: str):
    return json.loads((FIXTURES / rel).read_text())


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


TODAY = date(2026, 11, 25)
FAKE_CLIENT_ID = "test-client"
FAKE_CLIENT_SECRET = "s3cr3t-DO-NOT-LOG-7f9a"


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def awc(http, clock) -> AwcClient:
    async def fake_sleep(seconds: float) -> None:
        clock.advance(seconds)  # the AWC pacer waits on fake time

    return AwcClient(http, clock=clock, sleep=fake_sleep)


@pytest.fixture
def opensky(http, clock) -> OpenSkyClient:
    tokens = TokenManager(http, FAKE_CLIENT_ID, SecretStr(FAKE_CLIENT_SECRET), clock=clock)
    return OpenSkyClient(http, tokens, clock=clock, sleep=_no_sleep)


@pytest.fixture
def server(awc, opensky):
    return build_server(awc=awc, opensky=opensky, today=lambda: TODAY)


@pytest.fixture
def unconfigured_server(awc):
    return build_server(awc=awc, opensky=None, today=lambda: TODAY)
