"""Sensor entities for Shelly BLU TRV."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ShellyBluTrvCoordinator
from .entity import ShellyBluTrvEntity
from .shelly_ble import ShellyBluTrvState

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class ShellyBluTrvSensorDescription(SensorEntityDescription):
    """Sensor entity description for Shelly BLU TRV."""

    value_fn: Callable[[ShellyBluTrvState], float | int | str | None]


SENSOR_DESCRIPTIONS: list[ShellyBluTrvSensorDescription] = [
    ShellyBluTrvSensorDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda state: state.bthome.battery,
    ),
    ShellyBluTrvSensorDescription(
        key="current_temperature",
        translation_key="current_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=lambda state: (
            state.bthome.current_temperature
            if state.bthome.current_temperature is not None
            else state.status.current_C
        ),
    ),
    ShellyBluTrvSensorDescription(
        key="target_temperature",
        translation_key="target_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=lambda state: (
            state.bthome.target_temperature
            if state.bthome.target_temperature is not None
            else state.status.target_C
        ),
    ),
    ShellyBluTrvSensorDescription(
        key="external_temperature",
        translation_key="external_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=lambda state: state.bthome.external_temperature,
    ),
    ShellyBluTrvSensorDescription(
        key="valve_position",
        translation_key="valve_position",
        icon="mdi:valve",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda state: state.status.pos,
    ),
    ShellyBluTrvSensorDescription(
        key="rssi",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda state: state.bthome.rssi,
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Shelly BLU TRV sensors."""
    coordinator: ShellyBluTrvCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        ShellyBluTrvSensor(coordinator, description)
        for description in SENSOR_DESCRIPTIONS
    )


class ShellyBluTrvSensor(ShellyBluTrvEntity, SensorEntity):
    """Sensor entity for Shelly BLU TRV."""

    entity_description: ShellyBluTrvSensorDescription

    def __init__(
        self,
        coordinator: ShellyBluTrvCoordinator,
        description: ShellyBluTrvSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.base_unique_id}-{description.key}"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._attr_native_value = self.entity_description.value_fn(
            self.coordinator.state
        )
        self.async_write_ha_state()
