"""Config flow for Shelly BLU TRV integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

from .const import (
    CONF_DEVICE_ADDRESS,
    CONF_DEVICE_FW,
    CONF_DEVICE_MODEL,
    CONF_DEVICE_NAME,
    DOMAIN,
    SHELLY_MANUFACTURER_ID,
    SHELLY_RPC_SERVICE_UUID,
)
from .shelly_ble import ShellyBluTrvBleClient

_LOGGER = logging.getLogger(__name__)


class ShellyBluTrvConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Shelly BLU TRV."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}
        self._address: str | None = None
        self._name: str | None = None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle bluetooth discovery."""
        _LOGGER.debug(
            "Bluetooth discovery: %s (%s)",
            discovery_info.name,
            discovery_info.address,
        )

        await self.async_set_unique_id(
            discovery_info.address.replace(":", "").lower()
        )
        self._abort_if_unique_id_configured()

        self._discovery_info = discovery_info
        self._address = discovery_info.address
        self._name = discovery_info.name or f"Shelly BLU TRV {discovery_info.address}"

        self.context["title_placeholders"] = {"name": self._name}

        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm bluetooth discovery."""
        if user_input is not None:
            self._name = user_input.get("name", self._name)
            return await self.async_step_pair()

        return self.async_show_form(
            step_id="bluetooth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required("name", default=self._name): str,
                }
            ),
            description_placeholders={
                "name": self._name,
                "address": self._address,
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle user-initiated setup (manual add)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._address = user_input[CONF_ADDRESS]
            self._name = user_input.get("name", f"Shelly BLU TRV {self._address}")

            await self.async_set_unique_id(
                self._address.replace(":", "").lower()
            )
            self._abort_if_unique_id_configured()

            return await self.async_step_pair()

        # Find discovered Shelly BLU devices
        discovered = async_discovered_service_info(self.hass)
        shelly_devices: dict[str, str] = {}
        for info in discovered:
            if SHELLY_MANUFACTURER_ID in (info.manufacturer_data or {}):
                label = f"{info.name or 'Unknown'} ({info.address})"
                shelly_devices[info.address] = label
                self._discovered_devices[info.address] = info

        if shelly_devices:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_ADDRESS): vol.In(shelly_devices),
                        vol.Optional("name"): str,
                    }
                ),
                errors=errors,
            )

        # No devices found - allow manual MAC entry
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): str,
                    vol.Optional("name"): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "no_devices": "No Shelly BLU devices found. Enter the MAC address manually."
            },
        )

    async def async_step_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle pairing step.

        Instructs user to put the TRV into pairing mode, then attempts
        to connect and verify communication via Shelly.GetDeviceInfo.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            # User confirmed TRV is in pairing mode - try to connect
            try:
                ble_device = None
                if self._discovery_info:
                    ble_device = self._discovery_info.device
                elif self._address and self._address in self._discovered_devices:
                    ble_device = self._discovered_devices[self._address].device

                if ble_device is None:
                    # Try to find the device by address
                    discovered = async_discovered_service_info(self.hass)
                    for info in discovered:
                        if info.address == self._address:
                            ble_device = info.device
                            break

                if ble_device is None:
                    errors["base"] = "device_not_found"
                    return self.async_show_form(
                        step_id="pair",
                        errors=errors,
                        description_placeholders={
                            "name": self._name,
                            "address": self._address,
                        },
                    )

                # Attempt connection and device info query
                client = ShellyBluTrvBleClient(ble_device, self._address)
                try:
                    await client.connect()
                    device_info = await client.async_get_device_info()
                finally:
                    await client.disconnect()

                if device_info is None:
                    errors["base"] = "cannot_connect"
                    return self.async_show_form(
                        step_id="pair",
                        errors=errors,
                        description_placeholders={
                            "name": self._name,
                            "address": self._address,
                        },
                    )

                # Successfully connected and got device info
                model = device_info.get("model", "SBLUTRV-001")
                firmware = device_info.get("fw_id", "unknown")
                mac = device_info.get("mac", self._address)

                _LOGGER.info(
                    "Successfully paired with %s (%s), model: %s, fw: %s",
                    self._name,
                    self._address,
                    model,
                    firmware,
                )

                return self.async_create_entry(
                    title=self._name,
                    data={
                        CONF_DEVICE_ADDRESS: self._address,
                        CONF_DEVICE_NAME: self._name,
                        CONF_DEVICE_MODEL: model,
                        CONF_DEVICE_FW: firmware,
                    },
                )

            except TimeoutError:
                errors["base"] = "timeout"
            except Exception:
                _LOGGER.exception("Error during pairing")
                errors["base"] = "cannot_connect"

            return self.async_show_form(
                step_id="pair",
                errors=errors,
                description_placeholders={
                    "name": self._name,
                    "address": self._address,
                },
            )

        # Show pairing instructions
        return self.async_show_form(
            step_id="pair",
            description_placeholders={
                "name": self._name,
                "address": self._address,
            },
        )
