"""Minimal async client for the My Water Advisor 2.0 customer portal API.

This module has no Home Assistant imports so it can be tested on its own.
The API is undocumented; endpoints and payloads mirror what the official web
app sends.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
import logging
import random
import time
from typing import Any

import aiohttp

from .const import (
    APP_ID,
    BASE_URL,
    LOGIN_BACKOFF_FIRST,
    LOGIN_BACKOFF_MAX,
    LOGIN_BUDGET_PER_DAY,
    REQUEST_DELAY_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=30)


class MwaError(Exception):
    """Base error."""


class MwaAuthError(MwaError):
    """Credentials were rejected."""


class MwaApiError(MwaError):
    """The portal could not be reached or returned an unexpected response."""


class MwaLoginRefusedError(MwaApiError):
    """Login answered 400/403/429.

    The portal has been seen answering 400 while the account was temporarily
    locked, with correct credentials, so these are not treated as a wrong
    password. `retry_after` is in seconds, when the portal sent one.
    """

    def __init__(self, message: str, status: int, retry_after: float | None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class MwaLoginBlockedError(MwaApiError):
    """Our own login guard is holding off; no request was sent."""

    def __init__(self, wait: float) -> None:
        super().__init__(f"Holding off login for {wait / 60:.0f} more minutes")
        self.wait = wait


class LoginGuard:
    """Login backoff and daily budget, persisted by the caller.

    Every login attempt counts against a rolling 24 h budget. After a failed
    attempt, logins wait 15 min, 30 min, 1 h ... up to 6 h (or longer if the
    portal sent Retry-After). A successful login clears the backoff.
    `on_change` is called with the state dict after every change so it can be
    saved across restarts.
    """

    def __init__(
        self,
        data: dict[str, Any] | None = None,
        on_change: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Initialize from saved state."""
        data = data or {}
        self.next_login_at: float = float(data.get("next_login_at", 0))
        self.fail_count: int = int(data.get("fail_count", 0))
        self.attempts: list[float] = [float(t) for t in data.get("attempts", [])]
        self._on_change = on_change
        self._clock = clock

    def as_dict(self) -> dict[str, Any]:
        """Return the state to persist."""
        return {
            "next_login_at": self.next_login_at,
            "fail_count": self.fail_count,
            "attempts": self.attempts,
        }

    def wait_seconds(self) -> float:
        """Seconds until a login may be sent (0 = now)."""
        now = self._clock()
        self.attempts = [t for t in self.attempts if t > now - 86400]
        wait = self.next_login_at - now
        if len(self.attempts) >= LOGIN_BUDGET_PER_DAY:
            wait = max(wait, self.attempts[0] + 86400 - now)
        return max(wait, 0.0)

    def record_attempt(self) -> None:
        """Count a login request against the budget."""
        self.attempts.append(self._clock())
        self._changed()

    def record_success(self) -> None:
        """Clear the backoff after a successful login."""
        self.fail_count = 0
        self.next_login_at = 0
        self._changed()

    def record_failure(self, retry_after: float | None = None) -> None:
        """Back off exponentially after a failed login."""
        self.fail_count += 1
        delay = min(
            LOGIN_BACKOFF_FIRST.total_seconds() * 2 ** (self.fail_count - 1),
            LOGIN_BACKOFF_MAX.total_seconds(),
        )
        if retry_after:
            delay = max(delay, retry_after)
        self.next_login_at = self._clock() + delay * (1 + random.random() * 0.1)
        self._changed()

    def _changed(self) -> None:
        if self._on_change:
            self._on_change(self.as_dict())


def _retry_after(resp: aiohttp.ClientResponse) -> float | None:
    """Return Retry-After in seconds if the portal sent a numeric one."""
    try:
        return float(resp.headers.get("Retry-After", ""))
    except ValueError:
        return None


async def _error_text(resp: aiohttp.ClientResponse) -> str:
    """First 200 characters of an error response body, for the log.

    Only called for error responses: a successful login body holds the token.
    """
    try:
        return (await resp.text())[:200].replace("\n", " ")
    except (aiohttp.ClientError, UnicodeDecodeError):
        return "<unreadable body>"


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
        guard: LoginGuard | None = None,
    ) -> None:
        """Initialize the client.

        Without a guard (config flow, a human clicking Submit) every login is
        sent. With one, background logins honour its backoff and budget.
        """
        self._session = session
        self._email = email
        self._password = password
        self._device_id = device_id
        self._base_url = base_url
        self.guard = guard
        self._token: str | None = None

    @property
    def has_token(self) -> bool:
        """Whether a session token is held (no login needed for the next GET)."""
        return self._token is not None

    async def login(self) -> None:
        """Log in and keep the session token."""
        if self.guard:
            wait = self.guard.wait_seconds()
            if wait > 0:
                raise MwaLoginBlockedError(wait)
            self.guard.record_attempt()
        try:
            await self._login()
        except MwaError as err:
            if self.guard:
                retry = err.retry_after if isinstance(err, MwaLoginRefusedError) else None
                self.guard.record_failure(retry)
            raise
        if self.guard:
            self.guard.record_success()

    async def _login(self) -> None:
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
                if resp.status != 200:
                    text = await _error_text(resp)
                    _LOGGER.warning(
                        "Portal refused login: HTTP %s, Retry-After %s, body: %s",
                        resp.status,
                        resp.headers.get("Retry-After"),
                        text,
                    )
                    if resp.status == 401:
                        raise MwaAuthError("Login rejected (HTTP 401)")
                    if resp.status in (400, 403, 429):
                        raise MwaLoginRefusedError(
                            f"Login refused (HTTP {resp.status}): {text}",
                            resp.status,
                            _retry_after(resp),
                        )
                    raise MwaApiError(f"Login failed (HTTP {resp.status})")
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise MwaApiError(f"Cannot reach the portal: {err}") from err
        except ValueError as err:  # body wasn't JSON (e.g. a maintenance page)
            raise MwaApiError("Unexpected login response from the portal") from err

        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            # Seen as maintenance-style JSON, not proof of a wrong password.
            raise MwaApiError("Login response did not contain a token")
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
                        text = await _error_text(resp)
                        _LOGGER.warning(
                            "GET %s failed: HTTP %s, Retry-After %s, body: %s",
                            path,
                            resp.status,
                            resp.headers.get("Retry-After"),
                            text,
                        )
                        raise MwaApiError(f"GET {path} failed (HTTP {resp.status})")
                    return await resp.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError) as err:
                raise MwaApiError(f"Cannot reach the portal: {err}") from err
            except ValueError as err:  # body wasn't JSON (e.g. a maintenance page)
                raise MwaApiError(f"GET {path} returned a non-JSON response") from err
        raise MwaApiError("Unreachable")  # pragma: no cover

    async def get_meters(self) -> list[Meter]:
        """Return the meters on the account."""
        data = await self._get("/consumer/meters")
        if not isinstance(data, list):
            raise MwaApiError("Unexpected meters response")
        try:
            return [
                Meter(
                    meter_count=int(m["meterCount"]),
                    serial=str(m.get("meterSn") or m["meterCount"]),
                    address=str(m.get("fullAddress") or ""),
                )
                for m in data
            ]
        except (KeyError, TypeError, ValueError, AttributeError) as err:
            raise MwaApiError(f"Unexpected meters data: {err!r}") from err

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
        try:
            label = row.get("dateTime")
            if not label:
                continue
            naive = datetime.fromisoformat(label).replace(tzinfo=None)
            value = float(row.get("cons") or 0.0)
        except (AttributeError, TypeError, ValueError) as err:
            raise MwaApiError(f"Unexpected hourly data: {row!r}") from err
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


def plan_chunks(first: date, last: date, size: int) -> list[tuple[date, date]]:
    """Split the local days first..last into chunks of at most `size` days.

    Chunks are aligned to end on `last`, so the newest chunk always holds the
    most recent `size` days, where not-yet-reported hours can occur. Older
    chunks are complete history.
    """
    chunks: list[tuple[date, date]] = []
    end = last
    while end >= first:
        start = max(first, end - timedelta(days=size - 1))
        chunks.append((start, end))
        end = start - timedelta(days=1)
    chunks.reverse()
    return chunks


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
