"""Config flow for Shelly BLU TRV integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
    async_scanner_devices_by_address,
)
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback

from .const import (
    CONF_DEVICE_ADDRESS,
    CONF_DEVICE_FW,
    CONF_DEVICE_MODEL,
    CONF_DEVICE_NAME,
    CONF_PREFERRED_PROXY,
    DOMAIN,
    SHELLY_MANUFACTURER_ID,
    SHELLY_RPC_SERVICE_UUID,
)

_PROXY_AUTO = ""  # sentinel: let HA pick the best available proxy
from .shelly_ble import ShellyBluTrvBleClient

_LOGGER = logging.getLogger(__name__)


class ShellyBluTrvConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Shelly BLU TRV."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ShellyBluTrvOptionsFlow:
        """Return the options flow."""
        return ShellyBluTrvOptionsFlow(config_entry)

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

        Shows a proxy selector alongside pairing instructions, then connects
        through the selected proxy and verifies communication via Shelly.GetDeviceInfo.
        The selected proxy is saved as the preferred_proxy option so the coordinator
        uses it exclusively from the first connection onward.
        """
        errors: dict[str, str] = {}

        # Build proxy options list
        proxy_options: dict[str, str] = {_PROXY_AUTO: "Auto (best available)"}
        if self._address:
            for sd in async_scanner_devices_by_address(
                self.hass, self._address, connectable=True
            ):
                source = sd.scanner.source
                name = getattr(sd.scanner, "name", source)
                proxy_options[source] = f"{name} ({source})"

        if user_input is not None:
            selected_proxy = user_input.get(CONF_PREFERRED_PROXY, _PROXY_AUTO)
            if selected_proxy == _PROXY_AUTO:
                selected_proxy = None

            try:
                ble_device = None

                # Prefer the selected proxy's device reference
                if selected_proxy and self._address:
                    for sd in async_scanner_devices_by_address(
                        self.hass, self._address, connectable=True
                    ):
                        if sd.scanner.source != selected_proxy:
                            continue
                        device_source = sd.ble_device.details.get("source") if sd.ble_device else None
                        if device_source == selected_proxy:
                            ble_device = sd.ble_device
                            break

                # Fall back to any available device
                if ble_device is None:
                    if self._discovery_info:
                        ble_device = self._discovery_info.device
                    elif self._address and self._address in self._discovered_devices:
                        ble_device = self._discovered_devices[self._address].device
                if ble_device is None:
                    discovered = async_discovered_service_info(self.hass)
                    for info in discovered:
                        if info.address == self._address:
                            ble_device = info.device
                            break

                if ble_device is None:
                    errors["base"] = "device_not_found"
                else:
                    client = ShellyBluTrvBleClient(ble_device, self._address)
                    if selected_proxy:
                        client.set_source_hint(selected_proxy)
                    try:
                        await client.connect()
                        device_info = await client.async_get_device_info()
                    finally:
                        await client.disconnect()

                    if device_info is None:
                        errors["base"] = "cannot_connect"
                    else:
                        model = device_info.get("model", "SBLUTRV-001")
                        firmware = device_info.get("fw_id", "unknown")

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
                            options={
                                CONF_PREFERRED_PROXY: selected_proxy,
                            },
                        )

            except TimeoutError:
                errors["base"] = "timeout"
            except Exception:
                _LOGGER.exception("Error during pairing")
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="pair",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_PREFERRED_PROXY, default=_PROXY_AUTO
                    ): vol.In(proxy_options),
                }
            ),
            errors=errors,
            description_placeholders={
                "name": self._name,
                "address": self._address,
            },
        )


class ShellyBluTrvOptionsFlow(OptionsFlow):
    """Handle options for Shelly BLU TRV."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize the options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options form."""
        if user_input is not None:
            selected = user_input.get(CONF_PREFERRED_PROXY, _PROXY_AUTO)
            return self.async_create_entry(
                data={CONF_PREFERRED_PROXY: selected if selected != _PROXY_AUTO else None}
            )

        address = self._config_entry.data[CONF_DEVICE_ADDRESS]
        proxy_options: dict[str, str] = {_PROXY_AUTO: "Auto (best available)"}
        for sd in async_scanner_devices_by_address(
            self.hass, address, connectable=True
        ):
            source = sd.scanner.source
            name = getattr(sd.scanner, "name", source)
            proxy_options[source] = f"{name} ({source})"

        current = self._config_entry.options.get(CONF_PREFERRED_PROXY) or _PROXY_AUTO

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PREFERRED_PROXY, default=current): vol.In(
                        proxy_options
                    ),
                }
            ),
        )
