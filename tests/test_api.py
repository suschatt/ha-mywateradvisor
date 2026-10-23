"""Tests for the portal parsing helpers (no Home Assistant needed)."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from custom_components.mywateradvisor.api import rows_to_hours, trim_unreported

from .conftest import OCT21, OCT22, OCT23_PARTIAL

CHICAGO = ZoneInfo("America/Chicago")


def _rows(day: str, values: list[float]) -> list[dict]:
    return [{"dateTime": f"{day}T{h:02d}:00:00", "cons": v} for h, v in enumerate(values)]


def test_real_data_matches_daily_totals() -> None:
    rows = _rows("2026-10-21", OCT21) + _rows("2026-10-22", OCT22) + _rows("2026-10-23", OCT23_PARTIAL)
    hours = trim_unreported(rows_to_hours(rows, CHICAGO))

    # Local midnight Oct 21 (CDT, UTC-5) is 05:00 UTC.
    assert min(hours) == datetime(2026, 10, 21, 5, tzinfo=UTC)
    # Trailing padding on Oct 23 is dropped; the 00:00 reading is kept.
    assert max(hours) == datetime(2026, 10, 23, 5, tzinfo=UTC)
    assert len(hours) == 49

    per_day: dict[str, float] = {}
    for start, value in hours.items():
        key = start.astimezone(CHICAGO).date().isoformat()
        per_day[key] = per_day.get(key, 0.0) + value
    assert round(per_day["2026-10-21"], 1) == 120.8
    assert round(per_day["2026-10-22"], 1) == 373.5
    assert round(per_day["2026-10-23"], 1) == 2.1


def test_all_zero_means_nothing_reported() -> None:
    assert trim_unreported(rows_to_hours(_rows("2026-10-23", [0.0] * 24), CHICAGO)) == {}


def test_fall_back_day_keeps_25_hours() -> None:
    # 2026-11-01: 01:00 happens twice; if the portal repeats the label, both
    # hours must land in separate UTC hours.
    rows = _rows("2026-11-01", [1.0, 1.0]) + [{"dateTime": "2026-11-01T01:00:00", "cons": 1.0}]
    rows += [{"dateTime": f"2026-11-01T{h:02d}:00:00", "cons": 1.0} for h in range(2, 24)]
    hours = rows_to_hours(rows, CHICAGO)
    assert len(hours) == 25
    assert sum(hours.values()) == 25.0


def test_spring_forward_day_keeps_all_volume() -> None:
    # 2027-03-14: 02:00 does not exist locally. If the portal still sends it,
    # its volume must not be lost.
    rows = _rows("2027-03-14", [float(h) for h in range(24)])
    hours = rows_to_hours(rows, CHICAGO)
    assert len(hours) == 23
    assert sum(hours.values()) == sum(range(24))
