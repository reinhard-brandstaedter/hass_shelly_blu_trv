"""Button entities for Shelly BLU TRV."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ShellyBluTrvCoordinator
from .entity import ShellyBluTrvEntity


@dataclass(frozen=True, kw_only=True)
class ShellyBluTrvButtonDescription(ButtonEntityDescription):
    """Button entity description for Shelly BLU TRV."""

    press_fn: Callable[[ShellyBluTrvCoordinator], Coroutine[Any, Any, Any]]


async def _calibrate(coordinator: ShellyBluTrvCoordinator) -> None:
    await coordinator.async_rpc_command("TRV.Calibrate", {"id": 0})


async def _sync_time(coordinator: ShellyBluTrvCoordinator) -> None:
    import time
    utc_now = int(time.time())
    await coordinator.async_rpc_command("Sys.SetTime", {"unixtime": utc_now, "tz": "UTC0"})


async def _identify(coordinator: ShellyBluTrvCoordinator) -> None:
    await coordinator.async_rpc_command("Trv.ShowMessage", {"id": 0, "msg": "HELLO"})


BUTTON_DESCRIPTIONS: list[ShellyBluTrvButtonDescription] = [
    ShellyBluTrvButtonDescription(
        key="calibrate",
        translation_key="calibrate",
        icon="mdi:tune-vertical",
        entity_category=EntityCategory.CONFIG,
        press_fn=_calibrate,
    ),
    ShellyBluTrvButtonDescription(
        key="sync_time",
        translation_key="sync_time",
        icon="mdi:clock-sync",
        entity_category=EntityCategory.CONFIG,
        press_fn=_sync_time,
    ),
    ShellyBluTrvButtonDescription(
        key="identify",
        translation_key="identify",
        icon="mdi:eye",
        press_fn=_identify,
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Shelly BLU TRV buttons."""
    coordinator: ShellyBluTrvCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        ShellyBluTrvButton(coordinator, description)
        for description in BUTTON_DESCRIPTIONS
    )


class ShellyBluTrvButton(ShellyBluTrvEntity, ButtonEntity):
    """Button entity for Shelly BLU TRV."""

    entity_description: ShellyBluTrvButtonDescription

    def __init__(
        self,
        coordinator: ShellyBluTrvCoordinator,
        description: ShellyBluTrvButtonDescription,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.base_unique_id}-{description.key}"

    async def async_press(self) -> None:
        """Handle button press."""
        await self.entity_description.press_fn(self.coordinator)
