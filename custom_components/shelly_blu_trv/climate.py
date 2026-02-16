"""Climate entity for Shelly BLU TRV."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DEFAULT_BOOST_DURATION,
    DOMAIN,
    TRV_MAX_TEMP,
    TRV_MIN_TEMP,
    TRV_TEMP_STEP,
)
from .coordinator import ShellyBluTrvCoordinator
from .entity import ShellyBluTrvEntity

_LOGGER = logging.getLogger(__name__)

PRESET_BOOST = "boost"
PRESET_NONE = "none"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Shelly BLU TRV climate entity."""
    coordinator: ShellyBluTrvCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ShellyBluTrvClimate(coordinator)])


class ShellyBluTrvClimate(ShellyBluTrvEntity, ClimateEntity):
    """Climate entity for Shelly BLU TRV."""

    _attr_name = None  # Use device name
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = TRV_MIN_TEMP
    _attr_max_temp = TRV_MAX_TEMP
    _attr_target_temperature_step = TRV_TEMP_STEP
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
    )
    _attr_preset_modes = [PRESET_NONE, PRESET_BOOST]

    def __init__(self, coordinator: ShellyBluTrvCoordinator) -> None:
        """Initialize the climate entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.base_unique_id}-climate"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_attrs()
        self.async_write_ha_state()

    @callback
    def _update_attrs(self) -> None:
        """Update entity attributes from coordinator state."""
        state = self.coordinator.state

        # Current temperature - prefer BTHome (more frequent), fall back to RPC
        if state.bthome.current_temperature is not None:
            self._attr_current_temperature = state.bthome.current_temperature
        elif state.status.current_C is not None:
            self._attr_current_temperature = state.status.current_C

        # Target temperature - prefer BTHome, fall back to RPC
        if state.bthome.target_temperature is not None:
            self._attr_target_temperature = state.bthome.target_temperature
        elif state.status.target_C is not None:
            self._attr_target_temperature = state.status.target_C

        # HVAC mode - based on whether thermostat is enabled
        # When valve position is available, we can determine heating action
        self._attr_hvac_mode = HVACMode.HEAT

        # HVAC action based on valve position
        if state.status.pos is not None:
            if state.status.pos > 0:
                self._attr_hvac_action = HVACAction.HEATING
            else:
                self._attr_hvac_action = HVACAction.IDLE
        else:
            self._attr_hvac_action = None

        # Preset mode
        if state.status.boost_active:
            self._attr_preset_mode = PRESET_BOOST
        else:
            self._attr_preset_mode = PRESET_NONE

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return

        _LOGGER.debug(
            "Setting target temperature for %s to %.1f",
            self.coordinator.device_name,
            temperature,
        )

        await self.coordinator.async_rpc_command(
            "TRV.SetTarget", {"id": 0, "target_C": temperature}
        )

        # Optimistically update state
        self.coordinator.state.bthome.target_temperature = temperature
        self.coordinator.state.status.target_C = temperature
        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set HVAC mode."""
        if hvac_mode == HVACMode.HEAT:
            await self.coordinator.async_rpc_command(
                "Trv.SetConfig", {"id": 0, "config": {"enable": True}}
            )
        elif hvac_mode == HVACMode.OFF:
            await self.coordinator.async_rpc_command(
                "Trv.SetConfig", {"id": 0, "config": {"enable": False}}
            )

        self._attr_hvac_mode = hvac_mode
        self.async_write_ha_state()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set preset mode."""
        if preset_mode == PRESET_BOOST:
            await self.coordinator.async_rpc_command(
                "TRV.SetBoost", {"id": 0, "duration": DEFAULT_BOOST_DURATION}
            )
            self.coordinator.state.status.boost_active = True
        elif preset_mode == PRESET_NONE:
            if self.coordinator.state.status.boost_active:
                await self.coordinator.async_rpc_command(
                    "TRV.ClearBoost", {"id": 0}
                )
            self.coordinator.state.status.boost_active = False

        self.async_write_ha_state()
