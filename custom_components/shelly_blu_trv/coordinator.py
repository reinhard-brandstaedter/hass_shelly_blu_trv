"""Data update coordinator for Shelly BLU TRV."""

from __future__ import annotations

import logging
import time
from typing import Any

from bleak.backends.device import BLEDevice
from homeassistant.components.bluetooth import (
    BluetoothScannerDevice,
    BluetoothServiceInfoBleak,
)
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
from homeassistant.core import HomeAssistant, callback

from .const import POLL_INTERVAL, SHELLY_MANUFACTURER_ID
from .shelly_ble import (
    BTHomeData,
    ShellyBluTrvBleClient,
    ShellyBluTrvState,
    ShellyBluTrvStatus,
    parse_bthome_advertisement,
)

_LOGGER = logging.getLogger(__name__)

DEVICE_STARTUP_TIMEOUT = 300


class ShellyBluTrvCoordinator(ActiveBluetoothDataUpdateCoordinator):
    """Coordinator for Shelly BLU TRV device data."""

    def __init__(
        self,
        hass: HomeAssistant,
        ble_device: BLEDevice,
        address: str,
        device_name: str,
        model: str | None = None,
        firmware: str | None = None,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass=hass,
            logger=_LOGGER,
            address=address,
            needs_poll_method=self._needs_poll,
            poll_method=self._async_update,
            mode=1,  # PASSIVE
            connectable=True,
        )

        self.ble_device = ble_device
        self.device_name = device_name
        self.base_unique_id = address.replace(":", "").lower()

        self.state = ShellyBluTrvState()
        self.state.model = model
        self.state.firmware = firmware
        self.state.mac = address

        self._client = ShellyBluTrvBleClient(ble_device, address)
        self._last_poll_time: float = 0

    @property
    def client(self) -> ShellyBluTrvBleClient:
        """Return the BLE client."""
        return self._client

    @callback
    def _needs_poll(
        self,
        service_info: BluetoothServiceInfoBleak,
        seconds_since_last_poll: float | None,
    ) -> bool:
        """Determine if we need to poll the device for full status."""
        if seconds_since_last_poll is None:
            return True
        return seconds_since_last_poll >= POLL_INTERVAL

    async def _async_update(
        self,
        service_info: BluetoothServiceInfoBleak,
    ) -> None:
        """Poll the device for full status via RPC."""
        _LOGGER.debug("Polling %s for full status", self.device_name)

        # Update BLE device reference
        self.ble_device = service_info.device
        self._client.set_ble_device(service_info.device)

        try:
            await self._client.connect()
            status = await self._client.async_get_status()
            self.state.status = status
            self.state.last_rpc_poll = time.time()
            _LOGGER.debug(
                "Poll result for %s: target=%.1f, current=%.1f, pos=%s",
                self.device_name,
                status.target_C or 0,
                status.current_C or 0,
                status.pos,
            )
        except Exception:
            _LOGGER.warning(
                "Failed to poll %s",
                self.device_name,
                exc_info=True,
            )
        finally:
            try:
                await self._client.disconnect()
            except Exception:
                pass

    @callback
    def _async_handle_bluetooth_event(
        self,
        service_info: BluetoothServiceInfoBleak,
        change: Any,
    ) -> None:
        """Handle a bluetooth event (advertisement received)."""
        self.ble_device = service_info.device
        self._client.set_ble_device(service_info.device)

        # Parse BTHome advertisement data
        bthome = parse_bthome_advertisement(
            manufacturer_data=service_info.manufacturer_data,
            service_data=service_info.service_data,
            rssi=service_info.rssi,
        )

        if bthome:
            # Update state from advertisement
            old = self.state.bthome
            if bthome.packet_id is not None:
                old.packet_id = bthome.packet_id
            if bthome.battery is not None:
                old.battery = bthome.battery
            if bthome.target_temperature is not None:
                old.target_temperature = bthome.target_temperature
            if bthome.current_temperature is not None:
                old.current_temperature = bthome.current_temperature
            if bthome.external_temperature is not None:
                old.external_temperature = bthome.external_temperature
            if bthome.rssi is not None:
                old.rssi = bthome.rssi
            if bthome.button_event is not None:
                old.button_event = bthome.button_event

            self.state.last_advertisement = time.time()

        super()._async_handle_bluetooth_event(service_info, change)

    async def async_rpc_command(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Execute an RPC command (connect, send, disconnect)."""
        try:
            await self._client.connect()
            result = await self._client.async_rpc_call(method, params)
            return result
        finally:
            try:
                await self._client.disconnect()
            except Exception:
                pass
