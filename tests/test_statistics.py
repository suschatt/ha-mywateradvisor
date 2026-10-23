"""End-to-end tests: setup, statistics import into a real recorder, updates."""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.mywateradvisor.api import Meter, MwaAuthError
from custom_components.mywateradvisor.const import (
    CONF_BACKFILL_DAYS,
    CONF_DEVICE_ID,
    CONF_UNIT,
    DOMAIN,
)
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    list_statistic_ids,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .conftest import OCT21, OCT22, OCT23_PARTIAL, FakePortal

CLIENT = "custom_components.mywateradvisor.MwaClient"
STAT_ID = "mywateradvisor:water_221370217"
METER = Meter(meter_count=591, serial="221370217", address="1 Example St")


@pytest.fixture
async def setup_tz(hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("America/Chicago")


def _entry(backfill: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="someone@example.com",
        data={
            CONF_EMAIL: "someone@example.com",
            CONF_PASSWORD: "secret",
            CONF_DEVICE_ID: "dev",
            CONF_UNIT: "gal",
            CONF_BACKFILL_DAYS: backfill,
        },
    )


async def _stats(hass: HomeAssistant) -> list[dict]:
    await async_wait_recording_done(hass)
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 1, 1, tzinfo=UTC),
        None,
        {STAT_ID},
        "hour",
        None,
        {"sum", "state"},
    )
    return result.get(STAT_ID, [])


async def _setup(hass: HomeAssistant, entry: MockConfigEntry, portal: FakePortal, meters=None):
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.login"), patch(
        f"{CLIENT}.get_meters", return_value=meters or [METER]
    ), patch(f"{CLIENT}.get_hourly_rows", side_effect=portal.get_hourly_rows):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)


async def test_first_import_matches_portal(
    recorder_mock, hass: HomeAssistant, setup_tz, freezer
) -> None:
    freezer.move_to("2026-10-23T19:36:00+00:00")  # 2:36 PM CDT
    portal = FakePortal(
        {date(2026, 10, 21): OCT21, date(2026, 10, 22): OCT22, date(2026, 10, 23): OCT23_PARTIAL}
    )
    entry = _entry(backfill=2)
    await _setup(hass, entry, portal)
    assert entry.state is ConfigEntryState.LOADED

    stats = await _stats(hass)
    assert len(stats) == 49  # Oct 21 00:00 .. Oct 23 00:00 local
    assert stats[0]["start"] == datetime(2026, 10, 21, 5, tzinfo=UTC).timestamp()
    # The two -0.1 readings are clamped, so the total is 0.2 above 496.4.
    assert stats[-1]["sum"] == pytest.approx(496.5, abs=0.01)
    assert all(b["sum"] >= a["sum"] for a, b in zip(stats, stats[1:], strict=False))

    ids = await get_instance(hass).async_add_executor_job(list_statistic_ids, hass, {STAT_ID})
    assert ids[0]["source"] == DOMAIN
    assert ids[0]["statistics_unit_of_measurement"] == "gal"
    assert ids[0]["name"] == "My Water Advisor meter 221370217"


async def test_update_continues_running_total(
    recorder_mock, hass: HomeAssistant, setup_tz, freezer
) -> None:
    # Ten days of 10 gal/hour, then the real Oct 21-23 data.
    days = {date(2026, 10, d): [10.0] * 24 for d in range(11, 21)}
    days |= {date(2026, 10, 21): OCT21, date(2026, 10, 22): OCT22, date(2026, 10, 23): OCT23_PARTIAL}
    portal = FakePortal(days)
    freezer.move_to("2026-10-23T19:36:00+00:00")
    entry = _entry(backfill=12)
    await _setup(hass, entry, portal)

    first = await _stats(hass)
    expected_first = 10 * 240 + 496.5
    assert first[-1]["sum"] == pytest.approx(expected_first, abs=0.01)

    # Next day: the rest of Oct 23 arrives (24 gal) and an Oct 22 hour is
    # corrected from 65.7 to 70.7 (+5).
    portal.days[date(2026, 10, 23)] = [2.1] + [1.0] * 23
    portal.days[date(2026, 10, 22)] = OCT22[:7] + [70.70000000001164] + OCT22[8:]
    freezer.move_to("2026-10-24T19:36:00+00:00")
    coordinator = entry.runtime_data
    with patch(f"{CLIENT}.get_meters", return_value=[METER]), patch(
        f"{CLIENT}.get_hourly_rows", side_effect=portal.get_hourly_rows
    ):
        await coordinator.async_refresh()
    await hass.async_block_till_done()

    # The update only re-fetched from 3 days before the last imported hour.
    assert portal.calls[-1][0] == date(2026, 10, 20)

    stats = await _stats(hass)
    assert len(stats) == 13 * 24  # Oct 11 .. Oct 23, all hours
    assert stats[-1]["sum"] == pytest.approx(expected_first + 23 + 5, abs=0.01)
    # History before the re-fetch window is untouched.
    assert stats[24 * 9 - 1]["sum"] == pytest.approx(10 * 24 * 9, abs=0.01)


async def test_multiple_meters_use_per_meter_endpoint(
    recorder_mock, hass: HomeAssistant, setup_tz, freezer
) -> None:
    freezer.move_to("2026-10-23T19:36:00+00:00")
    portal = FakePortal({date(2026, 10, 22): OCT22})
    meters = [METER, Meter(meter_count=600, serial="999", address="")]
    await _setup(hass, _entry(backfill=2), portal, meters=meters)

    assert {call[2] for call in portal.calls} == {591, 600}
    await async_wait_recording_done(hass)
    ids =await get_instance(hass).async_add_executor_job(list_statistic_ids, hass)
    assert {i["statistic_id"] for i in ids} >= {STAT_ID, "mywateradvisor:water_999"}


async def test_bad_password_starts_reauth(recorder_mock, hass: HomeAssistant) -> None:
    entry = _entry(backfill=2)
    entry.add_to_hass(hass)
    with patch(f"{CLIENT}.login", side_effect=MwaAuthError("rejected")):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_unload(recorder_mock, hass: HomeAssistant, setup_tz, freezer) -> None:
    freezer.move_to("2026-10-23T19:36:00+00:00")
    entry = _entry(backfill=1)
    await _setup(hass, entry, FakePortal({}))
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
