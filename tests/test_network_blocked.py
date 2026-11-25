"""Guard: the suite must not be able to reach the network."""

import socket

import httpx
import pytest
from pytest_socket import SocketBlockedError

# These tests trip the block on purpose; anywhere else the warning means a real leak.
pytestmark = pytest.mark.filterwarnings("ignore:A test tried to use socket:UserWarning")


def test_raw_socket_is_blocked():
    with pytest.raises(SocketBlockedError):
        socket.create_connection(("1.1.1.1", 443), timeout=2)


async def test_unmocked_httpx_call_is_blocked():
    async with httpx.AsyncClient() as http:
        with pytest.raises(SocketBlockedError):
            await http.get("https://aviationweather.gov/api/data/metar?ids=ESGG&format=json")
