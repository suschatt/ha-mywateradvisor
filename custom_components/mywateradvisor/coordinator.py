"""Fetch hourly usage and import it into Home Assistant long-term statistics."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
import logging
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import Meter, MwaApiError, MwaAuthError, MwaClient, rows_to_hours, trim_unreported
from .const import (
    CONF_BACKFILL_DAYS,
    CONF_UNIT,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_UNIT,
    DOMAIN,
    LOOKBACK_DAYS,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


def statistic_id_for(meter: Meter) -> str:
    """Return the external statistic id for a meter."""
    slug = "".join(c if c.isalnum() else "_" for c in meter.serial.lower()).strip("_")
    return f"{DOMAIN}:water_{slug or meter.meter_count}"


def build_metadata(statistic_id: str, name: str, unit: str) -> StatisticMetaData:
    """Build statistic metadata that works across recorder versions.

    Newer recorders replaced has_mean with mean_type and added unit_class;
    only send the keys this Home Assistant version knows about.
    """
    keys = getattr(StatisticMetaData, "__annotations__", {})
    meta: dict[str, Any] = {
        "has_sum": True,
        "name": name,
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_of_measurement": unit,
    }
    if "mean_type" in keys:
        from homeassistant.components.recorder.models import (  # noqa: PLC0415
            StatisticMeanType,
        )

        meta["mean_type"] = StatisticMeanType.NONE
    else:
        meta["has_mean"] = False
    if "unit_class" in keys:
        meta["unit_class"] = "volume"
    return meta  # type: ignore[return-value]


def _as_datetime(value: Any) -> datetime:
    """Recorder rows carry start as a timestamp (newer) or datetime (older)."""
    if isinstance(value, datetime):
        return value
    return dt_util.utc_from_timestamp(value)


class MwaCoordinator(DataUpdateCoordinator[None]):
    """Periodically imports hourly usage for every meter on the account."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: MwaClient) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client
        self.unit: str = entry.data.get(CONF_UNIT, DEFAULT_UNIT)
        self.backfill_days: int = int(
            entry.data.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS)
        )

        # This integration has no entities, so nothing would ever listen to
        # the coordinator and it would never refresh. Register a no-op
        # listener to keep the schedule running.
        @callback
        def _keep_polling() -> None:
            """No-op listener."""

        entry.async_on_unload(self.async_add_listener(_keep_polling))

    async def _async_update_data(self) -> None:
        """Import new hourly readings for all meters."""
        try:
            meters = await self.client.get_meters()
            # The account-wide endpoint is the one the web app uses for a
            # single meter; with several meters, ask for each one separately.
            per_meter = len(meters) > 1
            for meter in meters:
                await self._import_meter(meter, meter.meter_count if per_meter else None)
        except MwaAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except MwaApiError as err:
            raise UpdateFailed(str(err)) from err

    async def _import_meter(self, meter: Meter, meter_count: int | None) -> None:
        """Fetch and import one meter's hourly statistics."""
        stat_id = statistic_id_for(meter)
        tz = dt_util.get_default_time_zone()
        today = dt_util.now(tz).date()
        recorder = get_instance(self.hass)

        last = await recorder.async_add_executor_job(
            get_last_statistics, self.hass, 1, stat_id, True, {"sum"}
        )
        if last.get(stat_id):
            last_start = _as_datetime(last[stat_id][0]["start"])
            first_day = last_start.astimezone(tz).date() - timedelta(days=LOOKBACK_DAYS)
        else:
            first_day = today - timedelta(days=self.backfill_days)
        window_start = datetime.combine(first_day, time.min, tzinfo=tz).astimezone(UTC)

        # Running total just before the re-fetch window.
        base_sum = 0.0
        if last.get(stat_id):
            prior = await recorder.async_add_executor_job(
                statistics_during_period,
                self.hass,
                window_start - timedelta(days=7),
                window_start,
                {stat_id},
                "hour",
                None,
                {"sum"},
            )
            if prior.get(stat_id):
                base_sum = float(prior[stat_id][-1]["sum"] or 0.0)

        rows = await self.client.get_hourly_rows(first_day, today, meter_count)
        hours = trim_unreported(rows_to_hours(rows, tz))
        hours = {start: value for start, value in hours.items() if start >= window_start}
        if not hours:
            _LOGGER.debug("No reported usage for meter %s since %s", meter.serial, first_day)
            return

        total = base_sum
        stats: list[StatisticData] = []
        for start in sorted(hours):
            value = max(hours[start], 0.0)  # meters occasionally report -0.1
            total += value
            stats.append(
                StatisticData(start=start, state=round(value, 3), sum=round(total, 3))
            )

        metadata = build_metadata(
            stat_id, f"My Water Advisor meter {meter.serial}", self.unit
        )
        async_add_external_statistics(self.hass, metadata, stats)
        _LOGGER.debug(
            "Imported %d hours for meter %s (last reported hour %s, total %.1f %s)",
            len(stats),
            meter.serial,
            max(hours).astimezone(tz),
            total,
            self.unit,
        )
