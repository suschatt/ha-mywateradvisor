"""Minimal async client for the My Water Advisor 2.0 customer portal API.

This module has no Home Assistant imports so it can be tested on its own.
The API is undocumented; endpoints and payloads mirror what the official web
app sends.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
import logging
from typing import Any

import aiohttp

from .const import APP_ID, BASE_URL, REQUEST_DELAY_SECONDS

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=30)


class MwaError(Exception):
    """Base error."""


class MwaAuthError(MwaError):
    """Credentials were rejected."""


class MwaApiError(MwaError):
    """The portal could not be reached or returned an unexpected response."""


@dataclass(frozen=True)
class Meter:
    """A water meter on the account."""

    meter_count: int
    serial: str
    address: str


class MwaClient:
    """Talks to the portal API with a per-session token."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        device_id: str,
        base_url: str = BASE_URL,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._email = email
        self._password = password
        self._device_id = device_id
        self._base_url = base_url
        self._token: str | None = None

    async def login(self) -> None:
        """Log in and keep the session token."""
        body = {
            "email": self._email,
            "type": 1,
            "pw": self._password,
            "deviceId": self._device_id,
            "osType": 3,
            "app": APP_ID,
        }
        headers = {"Accept": "application/json", "x-app-id": APP_ID}
        try:
            async with self._session.post(
                f"{self._base_url}/consumer/login",
                json=body,
                headers=headers,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status in (400, 401, 403):
                    raise MwaAuthError(f"Login rejected (HTTP {resp.status})")
                if resp.status != 200:
                    raise MwaApiError(f"Login failed (HTTP {resp.status})")
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise MwaApiError(f"Cannot reach the portal: {err}") from err

        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            raise MwaAuthError("Login response did not contain a token")
        self._token = token

    async def _get(self, path: str) -> Any:
        """GET a JSON resource, logging in again once if the token expired."""
        for attempt in (1, 2):
            if self._token is None:
                await self.login()
            try:
                async with self._session.get(
                    f"{self._base_url}{path}",
                    headers={
                        "Accept": "application/json",
                        "x-access-token": self._token or "",
                    },
                    timeout=_TIMEOUT,
                ) as resp:
                    if resp.status == 401 and attempt == 1:
                        _LOGGER.debug("Token expired, logging in again")
                        self._token = None
                        continue
                    if resp.status == 401:
                        raise MwaAuthError("Session rejected after re-login")
                    if resp.status != 200:
                        raise MwaApiError(f"GET {path} failed (HTTP {resp.status})")
                    return await resp.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError) as err:
                raise MwaApiError(f"Cannot reach the portal: {err}") from err
        raise MwaApiError("Unreachable")  # pragma: no cover

    async def get_meters(self) -> list[Meter]:
        """Return the meters on the account."""
        data = await self._get("/consumer/meters")
        if not isinstance(data, list):
            raise MwaApiError("Unexpected meters response")
        return [
            Meter(
                meter_count=int(m["meterCount"]),
                serial=str(m.get("meterSn") or m["meterCount"]),
                address=str(m.get("fullAddress") or ""),
            )
            for m in data
        ]

    async def get_hourly_rows(
        self, first: date, last: date, meter_count: int | None = None
    ) -> list[dict[str, Any]]:
        """Return raw hourly rows for the local days first..last (inclusive).

        One request with from=D and to=D+1 returns both whole days (48 rows),
        so the range is walked two days at a time.
        """
        prefix = f"{meter_count}/" if meter_count is not None else ""
        rows: list[dict[str, Any]] = []
        day = first
        while day <= last:
            nxt = day + timedelta(days=1)
            data = await self._get(f"/consumption/hourly/{prefix}{day}/{nxt}")
            if not isinstance(data, list):
                raise MwaApiError("Unexpected hourly response")
            rows.extend(data)
            day += timedelta(days=2)
            if day <= last:
                await asyncio.sleep(REQUEST_DELAY_SECONDS)
        return rows


def rows_to_hours(rows: list[dict[str, Any]], tz: tzinfo) -> dict[datetime, float]:
    """Convert portal rows to {utc hour start: volume}.

    Portal labels are local wall-clock hours without an offset. On the
    fall-back day a repeated label goes to the second (standard time) hour;
    on the spring-forward day a nonexistent label is merged into the hour it
    collides with so no volume is lost.
    """
    hours: dict[datetime, float] = {}
    for row in rows:
        label = row.get("dateTime")
        if not label:
            continue
        naive = datetime.fromisoformat(label).replace(tzinfo=None)
        value = float(row.get("cons") or 0.0)
        start = naive.replace(tzinfo=tz).astimezone(UTC)
        if start in hours:
            alt = naive.replace(tzinfo=tz, fold=1).astimezone(UTC)
            if alt not in hours:
                start = alt
            else:
                hours[start] += value
                continue
        hours[start] = value
    return hours


def trim_unreported(hours: dict[datetime, float]) -> dict[datetime, float]:
    """Drop trailing zero hours.

    The portal pads hours that have not been reported yet with 0, so zeros
    after the last nonzero reading mean "unknown", not "no usage".
    """
    nonzero = [start for start, value in hours.items() if value != 0]
    if not nonzero:
        return {}
    cutoff = max(nonzero)
    return {start: value for start, value in hours.items() if start <= cutoff}
