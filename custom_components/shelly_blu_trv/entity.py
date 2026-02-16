"""Base entity for Shelly BLU TRV."""

from __future__ import annotations

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo

from .const import DOMAIN, MANUFACTURER
from .coordinator import ShellyBluTrvCoordinator


class ShellyBluTrvEntity(PassiveBluetoothCoordinatorEntity):
    """Base class for Shelly BLU TRV entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ShellyBluTrvCoordinator) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.coordinator: ShellyBluTrvCoordinator = coordinator

        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, coordinator.state.mac or coordinator.base_unique_id)},
            manufacturer=MANUFACTURER,
            model=coordinator.state.model,
            name=coordinator.device_name,
            sw_version=coordinator.state.firmware,
        )
