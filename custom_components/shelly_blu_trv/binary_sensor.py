"""Binary sensor entities for Shelly BLU TRV."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ShellyBluTrvCoordinator
from .entity import ShellyBluTrvEntity
from .shelly_ble import ShellyBluTrvState


@dataclass(frozen=True, kw_only=True)
class ShellyBluTrvBinarySensorDescription(BinarySensorEntityDescription):
    """Binary sensor entity description for Shelly BLU TRV."""

    value_fn: Callable[[ShellyBluTrvState], bool | None]


BINARY_SENSOR_DESCRIPTIONS: list[ShellyBluTrvBinarySensorDescription] = [
    ShellyBluTrvBinarySensorDescription(
        key="battery_low",
        translation_key="battery_low",
        device_class=BinarySensorDeviceClass.BATTERY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: state.status.battery_low,
    ),
    ShellyBluTrvBinarySensorDescription(
        key="not_calibrated",
        translation_key="not_calibrated",
        icon="mdi:tune-vertical",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: state.status.not_calibrated,
    ),
    ShellyBluTrvBinarySensorDescription(
        key="not_mounted",
        translation_key="not_mounted",
        icon="mdi:radiator-off",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: state.status.not_mounted,
    ),
    ShellyBluTrvBinarySensorDescription(
        key="boost_active",
        translation_key="boost_active",
        icon="mdi:fire",
        value_fn=lambda state: state.status.boost_active,
    ),
    ShellyBluTrvBinarySensorDescription(
        key="override_active",
        translation_key="override_active",
        icon="mdi:thermometer-alert",
        value_fn=lambda state: state.status.override_active,
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Shelly BLU TRV binary sensors."""
    coordinator: ShellyBluTrvCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        ShellyBluTrvBinarySensor(coordinator, description)
        for description in BINARY_SENSOR_DESCRIPTIONS
    )


class ShellyBluTrvBinarySensor(ShellyBluTrvEntity, BinarySensorEntity):
    """Binary sensor entity for Shelly BLU TRV."""

    entity_description: ShellyBluTrvBinarySensorDescription

    def __init__(
        self,
        coordinator: ShellyBluTrvCoordinator,
        description: ShellyBluTrvBinarySensorDescription,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.base_unique_id}-{description.key}"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._attr_is_on = self.entity_description.value_fn(
            self.coordinator.state
        )
        self.async_write_ha_state()
