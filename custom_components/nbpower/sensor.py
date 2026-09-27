"""Sensor platform for the NB Power integration."""

from __future__ import annotations

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
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CURRENCY, DOMAIN
from .coordinator import NBPowerCoordinator

ENERGY_KEY = "energy_usage"
COST_KEY = "cost_usage"
UNIQUE_ID_TEMPLATE = "{entry_id}_{key}"


@dataclass(frozen=True, kw_only=True)
class NBPowerSensorEntityDescription(SensorEntityDescription):
    """Describes an NB Power sensor."""

    value_fn: Callable[[dict[str, Any]], float | str | None]


SENSORS: tuple[NBPowerSensorEntityDescription, ...] = (
    NBPowerSensorEntityDescription(
        key=ENERGY_KEY,
        translation_key="energy_usage",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
        value_fn=lambda data: data.get("cumulative_kwh"),
    ),
    NBPowerSensorEntityDescription(
        key=COST_KEY,
        translation_key="cost_usage",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda data: data.get("cumulative_cost"),
    ),
    NBPowerSensorEntityDescription(
        key="last_daily_energy",
        translation_key="last_daily_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value_fn=lambda data: data.get("last_daily_kwh"),
    ),
    NBPowerSensorEntityDescription(
        key="month_to_date_energy",
        translation_key="month_to_date_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value_fn=lambda data: data.get("tentative", {}).get("so_far_kwh"),
    ),
    NBPowerSensorEntityDescription(
        key="month_to_date_cost",
        translation_key="month_to_date_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data: data.get("tentative", {}).get("so_far_cost"),
    ),
    NBPowerSensorEntityDescription(
        key="projected_energy",
        translation_key="projected_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value_fn=lambda data: data.get("tentative", {}).get("projected_kwh"),
    ),
    NBPowerSensorEntityDescription(
        key="projected_bill",
        translation_key="projected_bill",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data: data.get("tentative", {}).get("projected_cost"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up NB Power sensors from a config entry."""
    coordinator: NBPowerCoordinator = entry.runtime_data
    async_add_entities(
        NBPowerSensorEntity(coordinator, entry, description) for description in SENSORS
    )


class NBPowerSensorEntity(CoordinatorEntity[NBPowerCoordinator], SensorEntity):
    """A sensor backed by the NB Power coordinator."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: NBPowerCoordinator,
        entry: ConfigEntry,
        description: NBPowerSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = UNIQUE_ID_TEMPLATE.format(
            entry_id=entry.entry_id, key=description.key
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="NB Power",
            manufacturer="NB Power",
            model="Smart Meter",
        )

    @property
    def native_value(self) -> float | str | None:
        return self.entity_description.value_fn(self.coordinator.data or {})

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key != ENERGY_KEY:
            return None
        data = self.coordinator.data or {}
        attrs: dict[str, Any] = {
            "account_number": data.get("account_number"),
            "meter_number": data.get("meter_number"),
            "last_daily_date": (
                data["last_daily_date"].isoformat()
                if data.get("last_daily_date")
                else None
            ),
            "today_kwh": data.get("today_kwh", 0.0),
            "daily_kwh": data.get("daily_kwh"),
        }
        return attrs
