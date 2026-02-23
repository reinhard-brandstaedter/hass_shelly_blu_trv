"""Number entity for Shelly BLU TRV external temperature feed."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ShellyBluTrvCoordinator
from .entity import ShellyBluTrvEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Shelly BLU TRV number entities."""
    coordinator: ShellyBluTrvCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        ShellyBluTrvExternalTemp(coordinator),
        ShellyBluTrvMinValvePosition(coordinator),
    ])


class ShellyBluTrvExternalTemp(ShellyBluTrvEntity, NumberEntity):
    """Number entity to feed external temperature to the TRV.

    Writing a value sends TRV.SetExternalTemperature to the device,
    allowing it to use an external temperature sensor for regulation
    instead of its built-in sensor.
    """

    _attr_translation_key = "external_temperature"
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = -40.0
    _attr_native_max_value = 60.0
    _attr_native_step = 0.1
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:thermometer-probe"

    def __init__(self, coordinator: ShellyBluTrvCoordinator) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.base_unique_id}-external-temp"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        ext_temp = self.coordinator.state.bthome.external_temperature
        if ext_temp is not None:
            self._attr_native_value = ext_temp
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        """Set external temperature value on the TRV."""
        _LOGGER.debug(
            "Setting external temperature for %s to %.1f",
            self.coordinator.device_name,
            value,
        )
        await self.coordinator.async_set_external_temperature(value)

        self._attr_native_value = value
        self.async_write_ha_state()


class ShellyBluTrvMinValvePosition(ShellyBluTrvEntity, NumberEntity):
    """Number entity to configure the minimum valve position.

    Sets how far the valve stays open even at the lowest heat demand,
    useful to prevent the valve from fully closing (e.g. for floor heating
    systems or to reduce water hammer).
    """

    _attr_translation_key = "min_valve_position"
    _attr_native_unit_of_measurement = "%"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_icon = "mdi:valve"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: ShellyBluTrvCoordinator) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.base_unique_id}-min-valve-position"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        value = self.coordinator.state.status.min_valve_position
        self._attr_native_value = value
        self._attr_available = value is not None
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        """Set minimum valve position on the TRV."""
        pos = int(value)
        await self.coordinator.async_rpc_command(
            "Trv.SetConfig", {"id": 0, "config": {"min_valve_position": pos}}
        )
        self.coordinator.state.status.min_valve_position = pos
        self.async_write_ha_state()
