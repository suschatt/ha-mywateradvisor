"""Fetch hourly usage and import it into Home Assistant long-term statistics."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, tzinfo
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

from .api import (
    Meter,
    MwaApiError,
    MwaAuthError,
    MwaClient,
    plan_chunks,
    rows_to_hours,
    trim_unreported,
)
from .const import (
    BACKFILL_DONE,
    CHUNK_DAYS,
    CONF_BACKFILL_DAYS,
    CONF_BACKFILL_STATE,
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


def _local_midnight(day: date, tz: tzinfo) -> datetime:
    """Return local midnight of `day` as a UTC datetime."""
    return datetime.combine(day, time.min, tzinfo=tz).astimezone(UTC)


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

    def _backfill_state(self) -> dict[str, str]:
        """Return per-statistic backfill progress stored in the config entry."""
        return dict(self.config_entry.data.get(CONF_BACKFILL_STATE, {}))

    def _save_backfill_state(self, statistic_id: str, value: str) -> None:
        """Persist backfill progress so an interrupted import resumes."""
        state = self._backfill_state()
        state[statistic_id] = value
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_BACKFILL_STATE: state},
        )

    async def _sum_before(self, statistic_id: str, start: datetime) -> float:
        """Return the running total of the last imported hour before `start`.

        Looks at the previous week hour by hour first (the usual case), then
        falls back to daily rows over a few years so older history separated
        by a gap still anchors the total. Returns 0 if there is none.
        """
        for lookback, period in (
            (timedelta(days=7), "hour"),
            (timedelta(days=3 * 366), "day"),
        ):
            prior = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                start - lookback,
                start,
                {statistic_id},
                period,
                None,
                {"sum"},
            )
            if prior.get(statistic_id):
                return float(prior[statistic_id][-1]["sum"] or 0.0)
        return 0.0

    async def _import_meter(self, meter: Meter, meter_count: int | None) -> None:
        """Fetch and import one meter's hourly statistics, a chunk at a time."""
        stat_id = statistic_id_for(meter)
        tz = dt_util.get_default_time_zone()
        today = dt_util.now(tz).date()

        last = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, stat_id, True, {"sum"}
        )
        has_stats = bool(last.get(stat_id))
        progress = self._backfill_state().get(stat_id)

        if progress == BACKFILL_DONE and has_stats:
            # Normal update: re-read a few days before the last imported hour.
            last_start = _as_datetime(last[stat_id][0]["start"])
            first_day = last_start.astimezone(tz).date() - timedelta(days=LOOKBACK_DAYS)
            backfilling = False
        elif progress not in (None, BACKFILL_DONE) and has_stats:
            # Resume an interrupted backfill where it stopped.
            first_day = date.fromisoformat(progress)
            backfilling = True
        else:
            # New setup (even if older statistics exist) or statistics deleted:
            # import the full history window again.
            first_day = today - timedelta(days=self.backfill_days)
            backfilling = True
        first_day = min(first_day, today)

        total = (
            await self._sum_before(stat_id, _local_midnight(first_day, tz))
            if has_stats
            else 0.0
        )
        metadata = build_metadata(
            stat_id, f"My Water Advisor meter {meter.serial}", self.unit
        )

        chunks = plan_chunks(first_day, today, CHUNK_DAYS)
        for chunk_first, chunk_last in chunks:
            is_newest = chunk_last >= today
            rows = await self.client.get_hourly_rows(chunk_first, chunk_last, meter_count)
            start_utc = _local_midnight(chunk_first, tz)
            end_utc = _local_midnight(chunk_last + timedelta(days=1), tz)
            hours = {
                start: value
                for start, value in rows_to_hours(rows, tz).items()
                if start_utc <= start < end_utc
            }
            if is_newest:
                # Only the newest chunk can contain hours the portal hasn't
                # received yet (padded with zeros).
                hours = trim_unreported(hours)

            if hours:
                stats: list[StatisticData] = []
                for start in sorted(hours):
                    value = max(hours[start], 0.0)  # meters occasionally report -0.1
                    total += value
                    stats.append(
                        StatisticData(start=start, state=round(value, 3), sum=round(total, 3))
                    )
                async_add_external_statistics(self.hass, metadata, stats)

            if backfilling:
                self._save_backfill_state(
                    stat_id,
                    BACKFILL_DONE
                    if is_newest
                    else (chunk_last + timedelta(days=1)).isoformat(),
                )
            _LOGGER.debug(
                "Meter %s: imported %s..%s (%d hours, total %.1f %s)",
                meter.serial,
                chunk_first,
                chunk_last,
                len(hours),
                total,
                self.unit,
            )

        if backfilling:
            _LOGGER.info(
                "Meter %s: history import complete from %s (total %.1f %s)",
                meter.serial,
                first_day,
                total,
                self.unit,
            )
