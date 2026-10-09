"""Sensor entities for the NeoVac MyEnergy integration.

Provides sensors compatible with the Home Assistant Energy Dashboard:
- Electricity consumption (kWh)
- Water consumption (L) - total, warm, and cold
- Heating consumption (kWh)
- Cooling consumption (kWh)

The sensor value is the cumulative total from invoicePeriods[-1].sum,
which increases over the billing period and resets when a new period
starts -- matching SensorStateClass.TOTAL_INCREASING semantics.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CATEGORY_AUTARKY,
    CATEGORY_ZEV_PV,
    CATEGORY_COLD_WATER,
    CATEGORY_COOLING,
    CATEGORY_ELECTRICITY,
    CATEGORY_HEATING,
    CATEGORY_WARM_WATER,
    CATEGORY_WATER,
    CONF_USAGE_UNIT_ID,
    CONF_USAGE_UNIT_NAME,
    DOMAIN,
)
from .coordinator import NeoVacCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class NeoVacSensorEntityDescription(SensorEntityDescription):
    """Describes a NeoVac sensor entity."""

    category: str
    # Optional: compute the value from the category data instead of
    # invoicePeriods[-1].sum (used for the grid/solar split sensors).
    value_fn: Callable[[dict[str, Any]], float | None] | None = None


def _last_period(data: dict[str, Any]) -> dict[str, Any]:
    """Return the latest invoice period of a response, or an empty dict."""
    periods = data.get("invoicePeriods")
    if isinstance(periods, list) and periods and isinstance(periods[-1], dict):
        return periods[-1]
    return {}


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _solar_consumption(data: dict[str, Any]) -> float | None:
    """Apartment electricity supplied by the building's solar plant (kWh)."""
    return _number(_last_period(data).get("photovoltaicConsumption"))


def _grid_consumption(data: dict[str, Any]) -> float | None:
    """Apartment electricity taken from the grid (kWh): total minus solar."""
    period = _last_period(data)
    total = _number(period.get("totalConsumption"))
    solar = _number(period.get("photovoltaicConsumption"))
    if total is None or solar is None:
        return None
    return max(total - solar, 0.0)


def _building_production(data: dict[str, Any]) -> float | None:
    """Solar energy produced by the whole building's plant (kWh)."""
    return _number(_last_period(data).get("powerGeneration"))


def _building_self_consumption(data: dict[str, Any]) -> float | None:
    """Solar energy consumed inside the building (kWh)."""
    return _number(_last_period(data).get("powerConsumption"))


# Sensor descriptions for each energy category.
# Water values come from the API in Liters; the invoice period sum is in m³.
# We report the invoice period sum (m³) for the energy dashboard.
SENSOR_DESCRIPTIONS: tuple[NeoVacSensorEntityDescription, ...] = (
    NeoVacSensorEntityDescription(
        key="electricity",
        translation_key="electricity",
        category=CATEGORY_ELECTRICITY,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    ),
    NeoVacSensorEntityDescription(
        key="water",
        translation_key="water",
        category=CATEGORY_WATER,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        suggested_display_precision=1,
    ),
    NeoVacSensorEntityDescription(
        key="warm_water",
        translation_key="warm_water",
        category=CATEGORY_WARM_WATER,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        suggested_display_precision=1,
    ),
    NeoVacSensorEntityDescription(
        key="cold_water",
        translation_key="cold_water",
        category=CATEGORY_COLD_WATER,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        suggested_display_precision=1,
    ),
    NeoVacSensorEntityDescription(
        key="heating",
        translation_key="heating",
        category=CATEGORY_HEATING,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    ),
    NeoVacSensorEntityDescription(
        key="cooling",
        translation_key="cooling",
        category=CATEGORY_COOLING,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    ),
    # Grid/solar split of the apartment (from the /autarky endpoint).
    NeoVacSensorEntityDescription(
        key="electricity_grid",
        translation_key="electricity_grid",
        category=CATEGORY_AUTARKY,
        value_fn=_grid_consumption,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    ),
    NeoVacSensorEntityDescription(
        key="electricity_solar",
        translation_key="electricity_solar",
        category=CATEGORY_AUTARKY,
        value_fn=_solar_consumption,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    ),
    # Solar plant of the whole building (from the /zev/photovoltaic endpoint).
    NeoVacSensorEntityDescription(
        key="building_solar_production",
        translation_key="building_solar_production",
        category=CATEGORY_ZEV_PV,
        value_fn=_building_production,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
    ),
    NeoVacSensorEntityDescription(
        key="building_solar_self_consumption",
        translation_key="building_solar_self_consumption",
        category=CATEGORY_ZEV_PV,
        value_fn=_building_self_consumption,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up NeoVac sensors from a config entry."""
    coordinator: NeoVacCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Wait for initial data
    await coordinator.async_config_entry_first_refresh()

    available_categories = coordinator.data.get("available_categories", [])
    _LOGGER.debug("Setting up sensors for categories: %s", available_categories)

    entities: list[NeoVacSensor] = []
    for description in SENSOR_DESCRIPTIONS:
        if description.category in available_categories:
            entities.append(
                NeoVacSensor(
                    coordinator=coordinator,
                    entry=entry,
                    description=description,
                )
            )
            _LOGGER.debug(
                "Added sensor: %s (%s)", description.key, description.category
            )

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Set up %d NeoVac sensors", len(entities))
    else:
        _LOGGER.warning(
            "No sensors created - no supported categories available. "
            "Available categories from API: %s",
            available_categories,
        )


def _extract_period_total(
    category_data: dict[str, Any] | None,
    category: str | None = None,
    debug_logging: bool = False,
) -> float | None:
    """Extract the invoice period total from API response data.

    Returns invoicePeriods[-1].sum, converting water units (m³ -> L)
    when necessary.  This cumulative total increases over the billing
    period and resets when a new period starts, which matches the
    TOTAL_INCREASING state class.
    """
    label = category or "unknown"

    if category_data is None or not isinstance(category_data, dict):
        if debug_logging:
            _LOGGER.warning(
                "[NeoVac debug] %s: no category data available, returning None",
                label,
            )
        return None

    invoice_periods = category_data.get("invoicePeriods")
    if not isinstance(invoice_periods, list) or not invoice_periods:
        if debug_logging:
            _LOGGER.warning(
                "[NeoVac debug] %s: no invoice periods in data, returning None",
                label,
            )
        return None

    period = invoice_periods[-1]
    total = period.get("sum")
    if total is None or not isinstance(total, (int, float)):
        if debug_logging:
            _LOGGER.warning(
                "[NeoVac debug] %s: invoice period sum is missing or invalid "
                "(value=%r), returning None",
                label,
                total,
            )
        return None

    value = float(total)

    # Convert water from m³ to Liters if the sum is in CubicMeter
    # but the measurement unit is Liter
    measurement_unit = category_data.get("measurementUnit", "")
    sum_unit = period.get("sumUnit", "")
    if measurement_unit == "Liter" and sum_unit == "CubicMeter":
        if debug_logging:
            _LOGGER.warning(
                "[NeoVac debug] %s: converting water unit m3 -> L: "
                "%.4f m3 -> %.1f L",
                label,
                value,
                value * 1000.0,
            )
        value *= 1000.0

    return value


class NeoVacSensor(CoordinatorEntity[NeoVacCoordinator], SensorEntity):
    """Representation of a NeoVac energy/water sensor."""

    entity_description: NeoVacSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: NeoVacCoordinator,
        entry: ConfigEntry,
        description: NeoVacSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description

        unit_id = entry.data[CONF_USAGE_UNIT_ID]
        unit_name = entry.data.get(CONF_USAGE_UNIT_NAME, str(unit_id))

        self._attr_unique_id = f"{unit_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(unit_id))},
            name=f"NeoVac {unit_name}",
            manufacturer="NeoVac",
            model="MyEnergy",
        )

    @property
    def native_value(self) -> float | None:
        """Return the current sensor value.

        Returns the invoice period cumulative total.
        """
        if not self.coordinator.data:
            return None

        categories = self.coordinator.data.get("categories", {})
        category_data = categories.get(self.entity_description.category)

        if category_data is None:
            return None

        if self.entity_description.value_fn is not None:
            return self.entity_description.value_fn(category_data)

        value = _extract_period_total(
            category_data,
            category=self.entity_description.category,
            debug_logging=self.coordinator.debug_logging,
        )

        if value is not None:
            _LOGGER.debug(
                "Sensor %s (%s) value: %s",
                self.entity_description.key,
                self.entity_description.category,
                value,
            )

        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return additional state attributes."""
        if not self.coordinator.data:
            return None

        categories = self.coordinator.data.get("categories", {})
        category_data = categories.get(self.entity_description.category)

        if not isinstance(category_data, dict):
            return None

        attrs: dict[str, Any] = {}

        # Add measurement unit info
        measurement_unit = category_data.get("measurementUnit")
        if measurement_unit:
            attrs["api_measurement_unit"] = measurement_unit

        # Add available resolutions
        resolutions = category_data.get("resolutions")
        if resolutions:
            attrs["available_resolutions"] = resolutions

        # Add the latest interval value (most recent reading)
        current_values = category_data.get("currentPeriodValues")
        if isinstance(current_values, list) and current_values:
            # Find last non-interpolated value, or just the last value
            latest = current_values[-1]
            for point in reversed(current_values):
                if not point.get("isInterpolated", False):
                    latest = point
                    break
            attrs["latest_reading"] = latest.get("value")
            attrs["latest_reading_date"] = latest.get("date")
            attrs["latest_reading_interpolated"] = latest.get(
                "isInterpolated", False
            )

        # Add invoice period info
        invoice_periods = category_data.get("invoicePeriods")
        if isinstance(invoice_periods, list) and invoice_periods:
            period = invoice_periods[-1]
            attrs["invoice_period_start"] = period.get("startDate")
            attrs["invoice_period_end"] = period.get("endDate")
            attrs["invoice_period_sum"] = period.get("sum")

        # Add fine-grained adjustment info
        last_sum_changed = (
            self.coordinator.data.get("last_sum_changed", {}).get(
                self.entity_description.category
            )
        )
        if last_sum_changed:
            attrs["last_sum_updated"] = last_sum_changed

        return attrs if attrs else None

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        if not super().available:
            return False
        if not self.coordinator.data:
            return False
        categories = self.coordinator.data.get("categories", {})
        return self.entity_description.category in categories
