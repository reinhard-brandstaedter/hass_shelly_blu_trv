"""Shelly BLU TRV integration for Home Assistant."""

from __future__ import annotations

import logging

from bleak import BleakError
from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    CONF_DEVICE_ADDRESS,
    CONF_DEVICE_FW,
    CONF_DEVICE_MODEL,
    CONF_DEVICE_NAME,
    CONF_PREFERRED_PROXY,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import ShellyBluTrvCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Shelly BLU TRV from a config entry."""
    address = entry.data[CONF_DEVICE_ADDRESS]
    device_name = entry.data[CONF_DEVICE_NAME]
    model = entry.data.get(CONF_DEVICE_MODEL)
    firmware = entry.data.get(CONF_DEVICE_FW)

    _LOGGER.debug("Setting up Shelly BLU TRV: %s (%s)", device_name, address)

    # Find the BLE device
    ble_device = async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        raise ConfigEntryNotReady(
            f"Could not find Shelly BLU TRV {device_name} ({address}). "
            "Ensure a Bluetooth proxy is in range."
        )

    coordinator = ShellyBluTrvCoordinator(
        hass=hass,
        ble_device=ble_device,
        address=address,
        device_name=device_name,
        model=model,
        firmware=firmware,
        preferred_proxy=entry.options.get(CONF_PREFERRED_PROXY),
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(coordinator.async_start())

    async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Apply updated options to the running coordinator without reloading."""
        coordinator.set_preferred_proxy(entry.options.get(CONF_PREFERRED_PROXY))

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
