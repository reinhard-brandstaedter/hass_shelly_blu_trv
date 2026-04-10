"""Shelly BLU TRV integration for Home Assistant."""

from __future__ import annotations

import logging

from bleak import BleakError
from homeassistant.components.bluetooth import (
    async_ble_device_from_address,
    async_scanner_devices_by_address,
)
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

    # Find the BLE device, preferring the configured preferred proxy so that
    # the initial device reference already points at the correct proxy rather
    # than whatever proxy most recently cached an advertisement.
    preferred_proxy = entry.options.get(CONF_PREFERRED_PROXY)
    ble_device = None
    if preferred_proxy:
        for sd in async_scanner_devices_by_address(hass, address, connectable=True):
            if sd.scanner.source == preferred_proxy:
                ble_device = sd.ble_device
                break
    if ble_device is None:
        if preferred_proxy:
            # Never fall back to a different proxy when a preferred one is
            # configured — the TRV is bonded exclusively to that proxy, and
            # async_ble_device_from_address() may return a merged/cached
            # BLEDevice from a different proxy (e.g. a previously-bonded one)
            # whose details["source"] is missing or wrong.  Starting with the
            # wrong device bypasses all subsequent source guards and causes
            # failed connections through the wrong proxy at runtime.
            raise ConfigEntryNotReady(
                f"Preferred proxy {preferred_proxy} has not seen "
                f"{device_name} ({address}) recently. "
                "Ensure the proxy is online and in range, then restart."
            )
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
        preferred_proxy=preferred_proxy,
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
