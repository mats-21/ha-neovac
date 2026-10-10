"""Import hourly NeoVac values as long-term statistics (external statistics ``neovac:*``).

The sensors of this integration only expose period totals. When Home Assistant is
switched off for a while, the whole missed consumption would be booked into the hour
of the next update. This module instead fetches the quarter-hourly / hourly values
from the NeoVac API and writes them into the statistics with the correct hour, so the
Energy dashboard stays right after a gap.

It continues where the last stored statistic ends. Only complete hours are written.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

try:  # Home Assistant 2025.11+
    from homeassistant.components.recorder.models import StatisticMeanType
except ImportError:  # older versions
    StatisticMeanType = None

from .api import NeoVacApiClient
from .const import (
    CATEGORY_COLD_WATER,
    CATEGORY_HEATING,
    CATEGORY_WARM_WATER,
    DOMAIN,
    RESOLUTION_HOUR,
    RESOLUTION_QUARTER_HOUR,
)

_LOGGER = logging.getLogger(__name__)

FMT = "%Y-%m-%dT%H:%M:%S"
FIRST_RUN_DAYS = 7  # if a statistic does not exist yet
QUARTER = timedelta(minutes=15)
HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class Series:
    """One long-term statistic."""

    key: str  # statistic_id is f"{DOMAIN}:{key}"
    name: str
    unit: str
    unit_class: str  # "energy" or "volume"

    @property
    def statistic_id(self) -> str:
        return f"{DOMAIN}:{self.key}"


# The statistic ids and names match tools/ha_import.py, so both write to the same series.
GRID = Series("strom_netzbezug", "NeoVac Strom Netzbezug", "kWh", "energy")
SOLAR = Series("strom_solar", "NeoVac Strom Solar (ZEV)", "kWh", "energy")
PV_PRODUCTION = Series("pv_produktion_haus", "NeoVac PV Produktion Haus", "kWh", "energy")
PV_SELF = Series("pv_eigenverbrauch_haus", "NeoVac PV Eigenverbrauch Haus", "kWh", "energy")
WATER_COLD = Series("wasser_kalt", "NeoVac Kaltwasser", "L", "volume")
WATER_WARM = Series("wasser_warm", "NeoVac Warmwasser", "L", "volume")
HEAT = Series("waerme", "NeoVac Wärme", "kWh", "energy")

ALL_SERIES = [GRID, SOLAR, PV_PRODUCTION, PV_SELF, WATER_COLD, WATER_WARM, HEAT]


# --- Pure helpers (no Home Assistant needed, covered by tests) ----------------------


def parse_values(block: list[dict[str, Any]] | None) -> dict[datetime, float]:
    """[{date, value, isInterpolated}] -> {naive local datetime: value}. None values are skipped."""
    out: dict[datetime, float] = {}
    for point in block or []:
        if point.get("value") is None:
            continue
        out[datetime.strptime(point["date"][:19], FMT)] = float(point["value"])
    return out


def grid_and_solar(
    total: dict[datetime, float], solar: dict[datetime, float]
) -> tuple[dict[datetime, float], dict[datetime, float]]:
    """Grid = total - solar (never below 0), only for timestamps present in both."""
    both = sorted(set(total) & set(solar))
    return (
        {t: max(total[t] - solar[t], 0.0) for t in both},
        {t: solar[t] for t in both},
    )


def complete_hours(
    points: dict[datetime, float], step: timedelta
) -> dict[datetime, float]:
    """Sum the points per hour and keep only hours that are complete.

    An hour counts as complete when the latest data point reaches its end, i.e. an hour
    ``H`` is kept if ``latest + step >= H + 1h``. Hours without any value are not written.
    """
    if not points:
        return {}
    limit = max(points) + step
    sums: dict[datetime, float] = {}
    for t, v in points.items():
        h = t.replace(minute=0, second=0, microsecond=0)
        sums[h] = sums.get(h, 0.0) + v
    return {h: v for h, v in sorted(sums.items()) if h + HOUR <= limit}


def cumulative(
    hours: dict[datetime, float], after: datetime | None, start_sum: float
) -> list[tuple[datetime, float]]:
    """Cumulative sums for all hours strictly after ``after``, starting from ``start_sum``."""
    total, rows = start_sum, []
    for h, v in hours.items():
        if after is not None and h <= after:
            continue
        total += v
        rows.append((h, round(total, 6)))
    return rows


# --- Home Assistant glue --------------------------------------------------------------


class NeoVacStatisticsImporter:
    """Writes the hourly statistics. Call ``async_update`` regularly."""

    def __init__(self, hass: HomeAssistant, client: NeoVacApiClient, unit_id: str) -> None:
        self._hass = hass
        self._client = client
        self._unit_id = unit_id
        self._lock = asyncio.Lock()

    async def async_update(self, _now: datetime | None = None) -> None:
        """Run one import. Accepts the timestamp that the HA time helpers pass in."""
        if self._lock.locked():
            return
        async with self._lock:
            try:
                await self._run()
            except Exception:  # never break the integration because of statistics
                _LOGGER.exception("NeoVac statistics import failed")

    async def _last(self, series: Series) -> tuple[datetime | None, float]:
        """(naive local start of the last stored hour, its sum) or (None, 0)."""
        res = await get_instance(self._hass).async_add_executor_job(
            get_last_statistics, self._hass, 1, series.statistic_id, True, {"sum"}
        )
        rows = res.get(series.statistic_id)
        if not rows:
            return None, 0.0
        start = rows[0]["start"]
        if not isinstance(start, datetime):
            start = dt_util.utc_from_timestamp(start)
        local = dt_util.as_local(start).replace(tzinfo=None)
        return local, float(rows[0].get("sum") or 0.0)

    async def _run(self) -> None:
        now = dt_util.now().replace(tzinfo=None)
        last: dict[str, tuple[datetime | None, float]] = {}
        for s in ALL_SERIES:
            last[s.key] = await self._last(s)

        first_run_start = (now - timedelta(days=FIRST_RUN_DAYS)).replace(
            minute=0, second=0, microsecond=0
        )
        end = now.strftime("%Y-%m-%d %H:%M")

        def start_for(*series: Series) -> str:
            starts = [last[s.key][0] or first_run_start for s in series]
            return min(starts).strftime("%Y-%m-%d %H:%M")

        uid = self._unit_id
        result: dict[Series, dict[datetime, float]] = {}

        autarky = await self._client.get_autarky(
            uid, start_for(GRID, SOLAR), end, RESOLUTION_QUARTER_HOUR
        )
        if autarky:
            grid, solar = grid_and_solar(
                parse_values(autarky.get("currentElectricityValues")),
                parse_values(autarky.get("currentPhotovoltaicValues")),
            )
            result[GRID] = complete_hours(grid, QUARTER)
            result[SOLAR] = complete_hours(solar, QUARTER)

        zev = await self._client.get_zev_photovoltaic(
            uid, start_for(PV_PRODUCTION, PV_SELF), end, RESOLUTION_QUARTER_HOUR
        )
        if zev:
            generation = parse_values(zev.get("currentPowerGenerationValues"))
            consumption = parse_values(zev.get("currentConsumptionValues"))
            both = set(generation) & set(consumption)
            result[PV_PRODUCTION] = complete_hours(
                {t: generation[t] for t in both}, QUARTER
            )
            result[PV_SELF] = complete_hours({t: consumption[t] for t in both}, QUARTER)

        for series, category in (
            (WATER_COLD, CATEGORY_COLD_WATER),
            (WATER_WARM, CATEGORY_WARM_WATER),
            (HEAT, CATEGORY_HEATING),
        ):
            data = await self._client.get_consumption(
                uid, category, RESOLUTION_HOUR, start_for(series), end
            )
            if data:
                result[series] = complete_hours(
                    parse_values(data.get("currentPeriodValues")), HOUR
                )

        for series, hours in result.items():
            after, start_sum = last[series.key]
            rows = cumulative(hours, after, start_sum)
            if rows:
                self._write(series, rows)
                _LOGGER.debug(
                    "NeoVac statistics %s: %d new hours up to %s",
                    series.statistic_id,
                    len(rows),
                    rows[-1][0],
                )

    def _write(self, series: Series, rows: list[tuple[datetime, float]]) -> None:
        meta: dict[str, Any] = {
            "has_sum": True,
            "name": series.name,
            "source": DOMAIN,
            "statistic_id": series.statistic_id,
            "unit_of_measurement": series.unit,
        }
        if StatisticMeanType is not None:
            meta["mean_type"] = StatisticMeanType.NONE
            meta["unit_class"] = series.unit_class
        else:
            meta["has_mean"] = False
        tz = dt_util.get_default_time_zone()
        stats = [
            StatisticData(start=dt_util.as_utc(h.replace(tzinfo=tz)), state=total, sum=total)
            for h, total in rows
        ]
        async_add_external_statistics(self._hass, StatisticMetaData(**meta), stats)
