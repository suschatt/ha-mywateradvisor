"""Shared fixtures."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Load integrations from custom_components/ with a real (in-memory) recorder.

    recorder_mock must be requested before hass is created, hence it comes first.
    """
    return


# Real hourly readings (gallons) from a Travis County WCID 17 meter,
# cross-checked against the portal's daily totals: Oct 21 = 120.8, Oct 22 = 373.5,
# Oct 23 = 2.1 (only 00:00 reported when captured; later hours padded with 0).
OCT21 = [0.0, 0.09999999997671694, 7.0, 0.0, 0.09999999997671694, 2.0, 10.100000000034925,
         35.59999999997672, 2.1000000000349246, 6.699999999953434, 5.0, 2.2000000000116415,
         3.7000000000116415, 0.09999999997671694, 2.6000000000349246, 21.5, 0.09999999997671694,
         2.5, 0.0, 2.7999999999883585, 9.5, 5.0, 0.0, 2.1000000000349246]
OCT22 = [0.0, 0.0, 4.2000000000116415, 0.0, 3.199999999953434, 0.1000000000349246, 11.5,
         65.70000000001164, 44.09999999997672, 44.29999999998836, 17.400000000023283,
         23.899999999965075, 15.200000000011642, 38.70000000001164, 30.70000000001164,
         17.29999999998836, 7.0, 3.900000000023283, 11.099999999976717, 6.400000000023283, 13.5,
         15.299999999988358, 0.09999999997671694, -0.09999999997671694]
OCT23_PARTIAL = [2.099999999976717] + [0.0] * 23


class FakePortal:
    """In-memory stand-in for the portal's hourly endpoint.

    Holds {local date: [24 hourly values]}; unknown days and hours read as 0,
    like the real portal's padding.
    """

    def __init__(self, days: dict[date, list[float]]) -> None:
        self.days = dict(days)
        self.calls: list[tuple[date, date, int | None]] = []

    def rows_for(self, day: date) -> list[dict[str, Any]]:
        values = self.days.get(day, [0.0] * 24)
        return [
            {"dateTime": f"{day.isoformat()}T{hour:02d}:00:00", "cons": value}
            for hour, value in enumerate(values)
        ]

    async def get_hourly_rows(self, first: date, last: date, meter_count=None):
        self.calls.append((first, last, meter_count))
        rows: list[dict[str, Any]] = []
        day = first
        while day <= last:
            rows += self.rows_for(day) + self.rows_for(day + timedelta(days=1))
            day += timedelta(days=2)
        return rows
