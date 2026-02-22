"""Switch entities for Shelly BLU TRV."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ShellyBluTrvCoordinator
from .entity import ShellyBluTrvEntity
from .shelly_ble import ShellyBluTrvState


@dataclass(frozen=True, kw_only=True)
class ShellyBluTrvSwitchDescription(SwitchEntityDescription):
    """Switch entity description for Shelly BLU TRV."""

    value_fn: Callable[[ShellyBluTrvState], bool | None]
    config_key: str


SWITCH_DESCRIPTIONS: list[ShellyBluTrvSwitchDescription] = [
    ShellyBluTrvSwitchDescription(
        key="floor_heating",
        translation_key="floor_heating",
        icon="mdi:radiator",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda state: state.status.floor_heating,
        config_key="floor_heating",
    ),
    ShellyBluTrvSwitchDescription(
        key="silent_mode",
        translation_key="silent_mode",
        icon="mdi:volume-off",
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda state: state.status.silent_mode,
        config_key="silent_mode",
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Shelly BLU TRV switches."""
    coordinator: ShellyBluTrvCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        ShellyBluTrvSwitch(coordinator, description)
        for description in SWITCH_DESCRIPTIONS
    )


class ShellyBluTrvSwitch(ShellyBluTrvEntity, SwitchEntity):
    """Switch entity for Shelly BLU TRV."""

    entity_description: ShellyBluTrvSwitchDescription

    def __init__(
        self,
        coordinator: ShellyBluTrvCoordinator,
        description: ShellyBluTrvSwitchDescription,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.base_unique_id}-{description.key}"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        value = self.entity_description.value_fn(self.coordinator.state)
        self._attr_is_on = value
        self._attr_available = value is not None
        self.async_write_ha_state()

    async def _set_flag(self, value: bool) -> None:
        """Write a flags-nested config value to the TRV."""
        await self.coordinator.async_rpc_command(
            "Trv.SetConfig",
            {"id": 0, "config": {"flags": {self.entity_description.config_key: value}}},
        )
        setattr(self.coordinator.state.status, self.entity_description.config_key, value)
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the feature."""
        await self._set_flag(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the feature."""
        await self._set_flag(False)
