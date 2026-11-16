"""Login error classification and the login guard (no Home Assistant needed)."""

from __future__ import annotations

import json

import pytest

from custom_components.mywateradvisor.api import (
    LoginGuard,
    MwaApiError,
    MwaAuthError,
    MwaClient,
    MwaLoginBlockedError,
    MwaLoginRefusedError,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class FakeResponse:
    def __init__(self, status: int, body: object, headers: dict | None = None) -> None:
        self.status = status
        self.headers = headers or {}
        self._text = body if isinstance(body, str) else json.dumps(body)

    async def text(self) -> str:
        return self._text

    async def json(self, content_type=None):
        return json.loads(self._text)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Answers every POST with the next queued response."""

    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.posts = 0

    def post(self, *args, **kwargs):
        self.posts += 1
        return self.responses.pop(0)


def _client(session: FakeSession, guard: LoginGuard) -> MwaClient:
    return MwaClient(session, "a@b.c", "pw", "dev", base_url="http://x", guard=guard)


def test_backoff_grows_caps_and_resets() -> None:
    clock = FakeClock()
    guard = LoginGuard(clock=clock)
    waits = []
    for _ in range(7):
        guard.record_failure()
        waits.append(guard.wait_seconds())
    # 15 min, 30 min, 1 h, 2 h, 4 h, then 6 h cap, each with <=10% jitter.
    for wait, base in zip(waits, [900, 1800, 3600, 7200, 14400, 21600, 21600]):
        assert base <= wait <= base * 1.1
    guard.record_success()
    assert guard.wait_seconds() == 0


def test_daily_budget() -> None:
    clock = FakeClock()
    guard = LoginGuard(clock=clock)
    for _ in range(8):
        guard.record_attempt()
        clock.now += 60
    assert guard.wait_seconds() == pytest.approx(86400 - 8 * 60)
    clock.now += 86400
    assert guard.wait_seconds() == 0


def test_state_round_trips() -> None:
    saved = {}
    clock = FakeClock()
    guard = LoginGuard(clock=clock, on_change=saved.update)
    guard.record_attempt()
    guard.record_failure()
    restored = LoginGuard(dict(saved), clock=clock)
    assert restored.fail_count == 1
    assert restored.wait_seconds() == pytest.approx(guard.wait_seconds())


async def test_400_is_refused_not_auth_and_blocks_next_login() -> None:
    clock = FakeClock()
    guard = LoginGuard(clock=clock)
    session = FakeSession(FakeResponse(400, {"error": "Account locked", "code": 1}))
    client = _client(session, guard)
    with pytest.raises(MwaLoginRefusedError) as err:
        await client.login()
    assert err.value.status == 400
    assert "Account locked" in str(err.value)
    assert guard.fail_count == 1
    # A second attempt inside the backoff sends nothing.
    with pytest.raises(MwaLoginBlockedError):
        await client.login()
    assert session.posts == 1


async def test_429_honours_retry_after() -> None:
    clock = FakeClock()
    guard = LoginGuard(clock=clock)
    session = FakeSession(FakeResponse(429, "slow down", {"Retry-After": "7200"}))
    with pytest.raises(MwaLoginRefusedError):
        await _client(session, guard).login()
    assert guard.wait_seconds() >= 7200


async def test_401_is_auth_error() -> None:
    guard = LoginGuard(clock=FakeClock())
    session = FakeSession(FakeResponse(401, {"error": "Invalid credentials"}))
    with pytest.raises(MwaAuthError):
        await _client(session, guard).login()


async def test_200_without_token_is_not_auth_error() -> None:
    guard = LoginGuard(clock=FakeClock())
    session = FakeSession(FakeResponse(200, {"message": "maintenance"}))
    with pytest.raises(MwaApiError) as err:
        await _client(session, guard).login()
    assert not isinstance(err.value, MwaAuthError)
    assert guard.fail_count == 1


async def test_success_clears_backoff() -> None:
    clock = FakeClock()
    guard = LoginGuard({"fail_count": 3, "next_login_at": 0}, clock=clock)
    client = _client(FakeSession(FakeResponse(200, {"token": "t"})), guard)
    await client.login()
    assert client.has_token
    assert guard.fail_count == 0
