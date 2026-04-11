"""Shelly BLU TRV integration for Home Assistant."""

from __future__ import annotations

import logging

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
            # Preferred proxy hasn't seen this device recently (TRV asleep or
            # just reloading after options save).  Fall back to any cached
            # BLEDevice so the coordinator can start; the wrong-proxy guards in
            # _refresh_ble_device() and connect() will prevent any actual
            # connection through the wrong proxy.  The startup probe will fire
            # once a device from the correct proxy appears.
            _LOGGER.debug(
                "Preferred proxy %s has no recent advertisement for %s (%s); "
                "starting with any available device reference",
                preferred_proxy,
                device_name,
                address,
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

    # Fire an immediate connection attempt so that BLE bonding can complete
    # within the TRV's 30-second pairing window without waiting for the first
    # advertisement-triggered poll (which can take 60-90 s).
    # Pairing flow: put TRV in pairing mode → save integration options →
    # this probe fires within ~2 s of the reload → bonding completes.
    hass.async_create_task(coordinator.async_startup_probe())

    async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Reload the config entry when options change.

        A full reload is required so that async_setup_entry re-runs with the
        new preferred_proxy, creates a fresh coordinator, and fires the startup
        probe.  Scheduling a task on the existing coordinator is unreliable —
        the task can be silently dropped before the event loop yields.
        """
        _LOGGER.debug("Options updated for %s — reloading config entry", entry.title)
        await hass.config_entries.async_reload(entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
