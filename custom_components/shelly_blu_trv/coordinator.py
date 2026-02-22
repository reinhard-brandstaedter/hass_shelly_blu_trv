"""Data update coordinator for Shelly BLU TRV."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from bleak.backends.device import BLEDevice
from homeassistant.components.bluetooth import (
    BluetoothScannerDevice,
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
)
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN, POLL_INTERVAL
from .shelly_ble import (
    BTHomeData,
    ShellyBluTrvBleClient,
    ShellyBluTrvState,
    ShellyBluTrvStatus,
    parse_bthome_advertisement,
)

_LOGGER = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 10  # seconds between retries
DEVICE_STARTUP_TIMEOUT = 300

# Global lock to serialize BLE connections across all TRV instances.
# Only one TRV should connect through the BT proxy at a time to avoid
# overwhelming the ESP32 proxy's limited connection capacity.
_global_ble_lock = asyncio.Lock()


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

        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            # Refresh BLE device on retry attempts (first attempt uses
            # the device from service_info, retries get a fresh reference)
            if attempt > 1:
                self._refresh_ble_device()

            try:
                async with _global_ble_lock:
                    await self._client.connect()
                    status = await self._client.async_get_status()
                    config = await self._client.async_get_config()
                    await self._client.disconnect()

                # Merge new values into existing status, preserving previous
                # values for any fields that came back as None
                old = self.state.status
                if status.pos is not None:
                    old.pos = status.pos
                if status.current_C is not None:
                    old.current_C = status.current_C
                if status.target_C is not None:
                    old.target_C = status.target_C
                if status.steps is not None:
                    old.steps = status.steps
                old.not_calibrated = status.not_calibrated
                old.not_mounted = status.not_mounted
                old.battery_low = status.battery_low
                old.ext_temp_missing = status.ext_temp_missing
                old.boost_active = status.boost_active
                old.boost_started_at = status.boost_started_at
                old.boost_duration = status.boost_duration
                old.override_active = status.override_active
                old.override_started_at = status.override_started_at
                old.override_duration = status.override_duration

                if config:
                    if config.get("min_valve_position") is not None:
                        old.min_valve_position = config["min_valve_position"]
                    # Flags are returned as a list with one dict element
                    flags_raw = config.get("flags")
                    if flags_raw:
                        flags = flags_raw[0] if isinstance(flags_raw, list) else flags_raw
                        if "floor_heating" in flags:
                            old.floor_heating = flags["floor_heating"]
                        if "silent_mode" in flags:
                            old.silent_mode = flags["silent_mode"]

                self.state.last_rpc_poll = time.time()
                _LOGGER.debug(
                    "Poll result for %s: target=%.1f, current=%.1f, pos=%s",
                    self.device_name,
                    old.target_C or 0,
                    old.current_C or 0,
                    old.pos,
                )
                return  # Success
            except Exception as err:
                last_error = err
                _LOGGER.debug(
                    "Poll attempt %d/%d failed for %s: %s",
                    attempt,
                    MAX_RETRIES,
                    self.device_name,
                    err,
                )
                try:
                    await self._client.disconnect()
                except Exception:
                    pass

            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)

        _LOGGER.warning(
            "Failed to poll %s after %d attempts: %s",
            self.device_name,
            MAX_RETRIES,
            last_error,
        )

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

    def _refresh_ble_device(self) -> None:
        """Fetch a fresh BLE device reference from HA's bluetooth stack.

        This ensures we always connect through the best available proxy
        with the most up-to-date connection info, rather than relying on
        a potentially stale reference from a previous advertisement.
        """
        fresh = async_ble_device_from_address(
            self.hass, self._client.address, connectable=True
        )
        if fresh:
            self.ble_device = fresh
            self._client.set_ble_device(fresh)

    async def async_set_target_verified(
        self,
        target_c: float,
        verify_retries: int = 3,
        verify_delay: float = 2.0,
    ) -> bool:
        """Set target temperature and verify the TRV accepted it.

        Sends TRV.SetTarget then reads back TRV.GetStatus within the same
        BLE connection to confirm the target was applied. Retries the set
        command if the readback doesn't match.

        Returns True if verified, False if all attempts failed.
        """
        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            self._refresh_ble_device()

            try:
                async with _global_ble_lock:
                    await self._client.connect()

                    for verify_attempt in range(1, verify_retries + 1):
                        await self._client.async_rpc_call(
                            "TRV.SetTarget", {"id": 0, "target_C": target_c}
                        )

                        # Wait briefly for the TRV to process the command
                        await asyncio.sleep(verify_delay)

                        # Read back status to verify
                        status = await self._client.async_get_status()

                        if (
                            status.target_C is not None
                            and abs(status.target_C - target_c) < 0.1
                        ):
                            await self._client.disconnect()
                            # Update state with verified values
                            self.state.status.target_C = status.target_C
                            self.state.bthome.target_temperature = status.target_C
                            if status.pos is not None:
                                self.state.status.pos = status.pos
                            if status.current_C is not None:
                                self.state.status.current_C = status.current_C
                            self.state.last_rpc_poll = time.time()
                            _LOGGER.debug(
                                "Verified target temperature for %s: %.1f°C "
                                "(attempt %d/%d)",
                                self.device_name,
                                status.target_C,
                                verify_attempt,
                                verify_retries,
                            )
                            return True

                        _LOGGER.warning(
                            "Target temperature mismatch for %s: "
                            "requested=%.1f, got=%.1f (verify %d/%d)",
                            self.device_name,
                            target_c,
                            status.target_C or 0,
                            verify_attempt,
                            verify_retries,
                        )

                    await self._client.disconnect()
            except Exception as err:
                last_error = err
                _LOGGER.debug(
                    "Set target verified attempt %d/%d failed for %s: %s",
                    attempt,
                    MAX_RETRIES,
                    self.device_name,
                    err,
                )
                try:
                    await self._client.disconnect()
                except Exception:
                    pass

            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)

        _LOGGER.warning(
            "Failed to verify target temperature %.1f for %s after %d attempts: %s",
            target_c,
            self.device_name,
            MAX_RETRIES,
            last_error,
        )
        return False

    async def async_rpc_command(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Execute an RPC command with retries (connect, send, disconnect).

        Uses a global lock to serialize BLE connections across all TRV
        instances, preventing the BT proxy from being overwhelmed.
        Connection failures are logged but not raised for non-critical
        commands to avoid crashing the websocket API.
        """
        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            # Get fresh BLE device reference before each attempt
            self._refresh_ble_device()

            try:
                async with _global_ble_lock:
                    await self._client.connect()
                    result = await self._client.async_rpc_call(method, params)
                    await self._client.disconnect()
                return result
            except Exception as err:
                last_error = err
                _LOGGER.debug(
                    "RPC %s attempt %d/%d failed for %s: %s",
                    method,
                    attempt,
                    MAX_RETRIES,
                    self.device_name,
                    err,
                )
                try:
                    await self._client.disconnect()
                except Exception:
                    pass

            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)

        _LOGGER.warning(
            "RPC %s failed for %s after %d attempts: %s",
            method,
            self.device_name,
            MAX_RETRIES,
            last_error,
        )
        return None
