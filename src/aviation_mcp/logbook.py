"""Client for the logbook API (the demo's downstream resource server).

Every call carries an exchanged token for the calling user, never the inbound token.
A 401 from the API evicts that token and re-exchanges once; a second 401 is an error.
A 200 whose endpoint list (`airports`, `flights`) is missing or not a list is an error,
not an empty result. Single rows that do not match the result models are dropped, not
passed to the model. No error message carries any part of the API's response.
"""

from __future__ import annotations

import httpx
from mcp.server.auth.provider import AccessToken
from pydantic import ValidationError

from aviation_mcp.exchange import TokenExchanger
from aviation_mcp.models import LogbookFlight, SavedAirport


class LogbookError(Exception):
    """Logbook API failed. The message is safe to show a model."""


REFUSED = (
    "The logbook API refused the exchanged token twice. Ask the user to sign in again; "
    "do not retry this call."
)
UNAVAILABLE = "The logbook API is unavailable. Try again later."
MALFORMED = (
    "The logbook API answered in an unexpected format. This is not an empty result: do not "
    "tell the user they have no records. Try again later."
)


class LogbookClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str, exchanger: TokenExchanger) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._exchanger = exchanger

    async def _get(self, inbound: AccessToken, path: str) -> dict:
        for attempt in (1, 2):
            token = await self._exchanger.token_for(inbound)
            try:
                resp = await self._http.get(
                    f"{self._base}{path}", headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.HTTPError:
                raise LogbookError(UNAVAILABLE) from None
            if resp.status_code == 401:
                self._exchanger.evict(inbound, token)
                if attempt == 2:
                    raise LogbookError(REFUSED)
                continue
            if resp.status_code != 200:
                raise LogbookError(UNAVAILABLE)
            try:
                body = resp.json()
            except ValueError:
                raise LogbookError(UNAVAILABLE) from None
            if not isinstance(body, dict):
                raise LogbookError(UNAVAILABLE)
            return body
        raise AssertionError("unreachable")  # pragma: no cover

    async def saved_airports(self, inbound: AccessToken) -> list[SavedAirport]:
        body = await self._get(inbound, "/me/airports")
        return _valid_rows(SavedAirport, _rows(body, "airports"))

    async def flights(self, inbound: AccessToken) -> list[LogbookFlight]:
        body = await self._get(inbound, "/me/flights")
        return _valid_rows(LogbookFlight, _rows(body, "flights"))


def _rows(body: dict, field: str) -> list:
    """The endpoint's list. A missing or non-list field is an error, never "no records"."""
    rows = body.get(field)
    if not isinstance(rows, list):
        raise LogbookError(MALFORMED)
    return rows


def _valid_rows[M](model: type[M], rows: list) -> list[M]:
    out: list[M] = []
    for row in rows:
        try:
            out.append(model.model_validate(row))  # type: ignore[attr-defined]
        except ValidationError:
            continue
    return out
