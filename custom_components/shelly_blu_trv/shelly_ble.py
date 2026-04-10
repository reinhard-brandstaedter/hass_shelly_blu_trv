"""BLE communication layer for Shelly BLU TRV devices.

Handles RPC-over-BLE protocol (JSON-RPC 2.0) and BTHome advertisement parsing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from dataclasses import dataclass, field
from typing import Any

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import establish_connection

from .const import (
    BTHOME_BATTERY,
    BTHOME_BUTTON_EVENT,
    BTHOME_PACKET_ID,
    BTHOME_TEMPERATURE,
    DEFAULT_BOOST_DURATION,
    SHELLY_RPC_DATA_UUID,
    SHELLY_RPC_RX_CTL_UUID,
    SHELLY_RPC_SERVICE_UUID,
    SHELLY_RPC_TX_CTL_UUID,
)

_LOGGER = logging.getLogger(__name__)

# RPC request ID counter
_rpc_id_counter = 0


def _next_rpc_id() -> int:
    """Get next RPC request ID."""
    global _rpc_id_counter
    _rpc_id_counter += 1
    return _rpc_id_counter


@dataclass
class ShellyBluTrvStatus:
    """Parsed TRV status from RPC response."""

    pos: int | None = None  # valve position 0-100%
    current_C: float | None = None
    target_C: float | None = None
    steps: int | None = None
    not_calibrated: bool = False
    not_mounted: bool = False
    battery_low: bool = False
    ext_temp_missing: bool = False
    boost_active: bool = False
    boost_started_at: float | None = None
    boost_duration: int | None = None
    override_active: bool = False
    override_started_at: float | None = None
    override_duration: int | None = None
    # Config fields (populated by Trv.GetConfig poll)
    min_valve_position: int | None = None
    floor_heating: bool | None = None
    silent_mode: bool | None = None


@dataclass
class BTHomeData:
    """Parsed BTHome advertisement data."""

    packet_id: int | None = None
    battery: int | None = None
    target_temperature: float | None = None
    current_temperature: float | None = None
    external_temperature: float | None = None
    button_event: int | None = None
    rssi: int | None = None


@dataclass
class ShellyBluTrvState:
    """Combined device state from advertisements and RPC."""

    # From BTHome advertisements
    bthome: BTHomeData = field(default_factory=BTHomeData)
    # From RPC status
    status: ShellyBluTrvStatus = field(default_factory=ShellyBluTrvStatus)
    # Device info
    model: str | None = None
    firmware: str | None = None
    mac: str | None = None
    # Timestamps
    last_advertisement: float = 0
    last_rpc_poll: float = 0


def parse_bthome_advertisement(
    manufacturer_data: dict[int, bytes],
    service_data: dict[str, bytes] | None = None,
    rssi: int | None = None,
) -> BTHomeData | None:
    """Parse BTHome v2 advertisement data from Shelly BLU TRV.

    The BTHome payload contains sequential object entries:
    - 0x00 (uint8): packet_id
    - 0x01 (uint8): battery percentage
    - 0x45 (sint16, scale 0.1): temperature (appears 3 times: target, current, external)
    - 0x3A (uint8): button event
    """
    data = BTHomeData(rssi=rssi)

    # Try service data first (BTHome UUID d0611e78-bbb4-4591-a5f8-487910ae4366)
    raw: bytes | None = None
    if service_data:
        for uuid, payload in service_data.items():
            if "fcd2" in uuid.lower() or "d0611e78" in uuid.lower():
                raw = payload
                break

    # Also check manufacturer data for Shelly-specific format
    if raw is None and manufacturer_data:
        from .const import SHELLY_MANUFACTURER_ID

        raw_mfr = manufacturer_data.get(SHELLY_MANUFACTURER_ID)
        if raw_mfr is not None:
            # Shelly manufacturer data may contain BTHome-like payload
            # but primary BTHome data is in service data
            pass

    if raw is None:
        return None

    # BTHome v2 format: first byte is device info (version + encryption flag)
    if len(raw) < 2:
        return None

    device_info = raw[0]
    version = (device_info >> 5) & 0x07
    encrypted = bool(device_info & 0x01)

    if version != 2:
        _LOGGER.debug("Unsupported BTHome version: %d", version)
        return None

    if encrypted:
        _LOGGER.debug("Encrypted BTHome data not yet supported")
        return None

    # Parse objects starting after device info byte
    pos = 1
    temp_index = 0

    while pos < len(raw):
        if pos >= len(raw):
            break

        obj_id = raw[pos]
        pos += 1

        if obj_id == BTHOME_PACKET_ID:
            # uint8
            if pos < len(raw):
                data.packet_id = raw[pos]
                pos += 1

        elif obj_id == BTHOME_BATTERY:
            # uint8, percentage
            if pos < len(raw):
                data.battery = raw[pos]
                pos += 1

        elif obj_id == BTHOME_TEMPERATURE:
            # sint16, scale factor 0.01 (BTHome v2)
            if pos + 1 < len(raw):
                raw_temp = struct.unpack_from("<h", raw, pos)[0]
                temp = raw_temp / 10.0
                pos += 2

                # Temperatures appear in order: target, current, external
                if temp_index == 0:
                    data.target_temperature = temp
                elif temp_index == 1:
                    data.current_temperature = temp
                elif temp_index == 2:
                    data.external_temperature = temp
                temp_index += 1

        elif obj_id == BTHOME_BUTTON_EVENT:
            # uint8
            if pos < len(raw):
                data.button_event = raw[pos]
                pos += 1

        elif obj_id == 0x54:
            # raw data (variable length), skip
            # From debug log: 0x54 followed by data bytes
            if pos < len(raw):
                # Raw sensor data - typically 2 bytes
                pos += 2

        elif obj_id == 0xF0:
            # Device type identifier
            if pos + 1 < len(raw):
                pos += 2

        elif obj_id == 0xF1:
            # Firmware version
            if pos + 3 < len(raw):
                pos += 4

        else:
            # Unknown object ID - try to skip
            _LOGGER.debug("Unknown BTHome object ID: 0x%02X at position %d", obj_id, pos)
            break

    return data


class ShellyBluTrvBleClient:
    """BLE client for Shelly BLU TRV RPC communication."""

    def __init__(
        self,
        ble_device: BLEDevice,
        address: str,
    ) -> None:
        """Initialize the BLE client."""
        self._ble_device = ble_device
        self._address = address
        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()
        self._connected = False
        self._mtu: int = 20  # Conservative default, will negotiate higher
        self._source_hint: str | None = None  # Set by coordinator before each connect
        self._rssi: int | None = None  # Last advertisement RSSI from coordinator

    @property
    def address(self) -> str:
        """Return the BLE address."""
        return self._address

    @property
    def ble_device_source(self) -> str | None:
        """Return the source (proxy address) of the current BLE device reference."""
        if self._ble_device is None:
            return None
        return self._ble_device.details.get("source")

    def set_ble_device(self, ble_device: BLEDevice, rssi: int | None = None) -> None:
        """Update the BLE device reference and last known advertisement RSSI."""
        self._ble_device = ble_device
        if rssi is not None:
            self._rssi = rssi

    def set_source_hint(self, source: str | None) -> None:
        """Set the BT proxy/scanner source used in log messages.

        Called by the coordinator (which has reliable proxy info via
        _resolve_proxy_source()) before every connect attempt so that
        all BLE log lines show which proxy is being used.
        """
        self._source_hint = source

    def _proxy_source(self) -> str:
        """Return the proxy source for log messages."""
        return self._source_hint or "?"

    async def connect(self) -> None:
        """Establish BLE connection."""
        if self._connected and self._client:
            if self._client.is_connected:
                return
            # Connection was silently dropped by the peripheral; reset state
            # so we fall through to establish_connection below.
            _LOGGER.debug(
                "Stale BLE connection detected for %s via %s, reconnecting",
                self._address,
                self._proxy_source(),
            )
            self._connected = False
            self._client = None

        _LOGGER.debug("Connecting to %s via %s", self._address, self._proxy_source())

        # Fail fast if the BLE device's source disagrees with the configured
        # proxy hint.  service_info.device in HA's BT stack can be a merged
        # "best device" object whose details["source"] points at a different
        # proxy than the one that actually triggered the callback.  If that
        # slips through our coordinator filtering we'd waste 8–20 s on a
        # connection that will either time out or fail authentication.
        device_source = self._ble_device.details.get("source") if self._ble_device else None
        if self._source_hint and device_source != self._source_hint:
            raise ConnectionError(
                f"BLE device for {self._address} has source {device_source} "
                f"but proxy hint is {self._source_hint} — refusing wrong-proxy connection"
            )

        # Snapshot the BLE device and pass it as ble_device_callback so that
        # establish_connection always uses the preferred-proxy device regardless
        # of how many internal retry attempts it makes.  Without this, HA's
        # multi-proxy wrapper of establish_connection queries
        # async_ble_device_from_address() on each attempt and falls back to
        # other proxies (e.g. a previously-bonded tempsensor-wz), bypassing our
        # preferred-proxy filtering entirely.
        # max_attempts=1: let our outer MAX_RETRIES loop handle retries with a
        # proper RETRY_DELAY between attempts.
        _device = self._ble_device
        self._client = await establish_connection(
            BleakClient,
            _device,
            self._address,
            max_attempts=1,
            ble_device_callback=lambda: _device,
        )
        self._connected = True

        # Get negotiated MTU (minus 3 bytes for ATT header)
        self._mtu = min(self._client.mtu_size - 3, 512) if self._client.mtu_size else 20

        _LOGGER.debug(
            "Connected to %s via %s, MTU: %d, RSSI: %s dBm",
            self._address,
            self._proxy_source(),
            self._mtu,
            self._rssi if self._rssi is not None else "?",
        )

        # Verify the RPC characteristics are present in the GATT table.
        # If service discovery failed or is incomplete the writes will fail
        # with "Characteristic not found"; catching it here lets the retry
        # loop reconnect instead of burning a write attempt.
        for char_uuid in (SHELLY_RPC_TX_CTL_UUID, SHELLY_RPC_DATA_UUID):
            if self._client.services.get_characteristic(char_uuid) is None:
                await self.disconnect()
                raise ConnectionError(
                    f"RPC characteristic {char_uuid} not found on {self._address} "
                    "(GATT service discovery incomplete)"
                )

    async def disconnect(self) -> None:
        """Close BLE connection."""
        if self._client and self._connected:
            _LOGGER.debug("Disconnecting from %s via %s", self._address, self._proxy_source())
            try:
                await asyncio.wait_for(self._client.disconnect(), timeout=10.0)
            except (asyncio.TimeoutError, Exception):
                _LOGGER.debug("Error during disconnect from %s", self._address, exc_info=True)
            finally:
                self._connected = False
                self._client = None

    async def async_rpc_call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any] | None:
        """Send an RPC request and wait for the response.

        Protocol:
        1. Write payload length (4-byte big-endian) to TX control characteristic
        2. Write JSON payload (chunked to MTU) to data characteristic
        3. Poll RX control characteristic until response length is available
        4. Read response from data characteristic in chunks
        5. Parse and return JSON response

        Uses polling instead of notifications for RX control, as BLE
        notifications are unreliable through ESPHome BT proxies.
        """
        async with self._lock:
            if not self._client or not self._connected:
                raise ConnectionError("Not connected to device")

            request_id = _next_rpc_id()
            request = {
                "id": request_id,
                "src": "hass_shelly_blu_trv",
                "method": method,
            }
            if params:
                request["params"] = params

            payload = json.dumps(request, separators=(",", ":")).encode("utf-8")
            payload_length = len(payload)

            _LOGGER.debug(
                "RPC call to %s via %s: %s (payload: %d bytes)",
                self._address,
                self._proxy_source(),
                method,
                payload_length,
            )

            # Step 1: Write payload length to TX control
            length_bytes = struct.pack(">I", payload_length)
            await self._client.write_gatt_char(
                SHELLY_RPC_TX_CTL_UUID, length_bytes, response=True
            )

            # Step 2: Write payload to data characteristic (chunked)
            offset = 0
            while offset < payload_length:
                chunk_size = min(self._mtu, payload_length - offset)
                chunk = payload[offset : offset + chunk_size]
                await self._client.write_gatt_char(
                    SHELLY_RPC_DATA_UUID, chunk, response=True
                )
                offset += chunk_size

            # Step 3: Poll RX control for response length
            response_length = 0
            poll_interval = 0.5  # seconds between polls
            elapsed = 0.0

            while elapsed < timeout:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

                try:
                    rx_data = await self._client.read_gatt_char(
                        SHELLY_RPC_RX_CTL_UUID
                    )
                    if rx_data and len(rx_data) >= 4:
                        response_length = struct.unpack(">I", rx_data[:4])[0]
                        if response_length > 0:
                            _LOGGER.debug(
                                "RX control: response length = %d (after %.1fs)",
                                response_length,
                                elapsed,
                            )
                            break
                except Exception as err:
                    _LOGGER.debug("Error reading RX control: %s", err)

            if response_length == 0:
                _LOGGER.error(
                    "Timeout waiting for RPC response from %s for %s (%.1fs)",
                    self._address,
                    method,
                    elapsed,
                )
                return None

            # Step 4: Read response from data characteristic
            response_data = bytearray()
            bytes_read = 0
            while bytes_read < response_length:
                chunk = await self._client.read_gatt_char(SHELLY_RPC_DATA_UUID)
                response_data.extend(chunk)
                bytes_read += len(chunk)

            # Step 5: Parse JSON response
            response_text = response_data[:response_length].decode("utf-8")
            _LOGGER.debug("RPC response: %s", response_text)

            response = json.loads(response_text)

            if "error" in response:
                _LOGGER.error(
                    "RPC error from %s: %s",
                    self._address,
                    response["error"],
                )
                return None

            return response.get("result")

    # --- High-level TRV commands ---

    async def async_get_device_info(self) -> dict[str, Any] | None:
        """Get device info (model, firmware, MAC, etc.)."""
        return await self.async_rpc_call("Shelly.GetDeviceInfo")

    async def async_get_status(self) -> ShellyBluTrvStatus:
        """Get TRV status (valve position, temperatures, flags)."""
        result = await self.async_rpc_call("TRV.GetStatus", {"id": 0})
        status = ShellyBluTrvStatus()

        if result is None:
            _LOGGER.warning(
                "TRV.GetStatus returned no result from %s (RPC call failed or timed out)",
                self._address,
            )
            return status

        status.pos = result.get("pos")
        if status.pos is None:
            _LOGGER.warning(
                "TRV.GetStatus from %s succeeded but 'pos' is missing/null — "
                "full response: %s",
                self._address,
                result,
            )
        status.current_C = result.get("current_C")
        status.target_C = result.get("target_C")
        status.steps = result.get("steps")
        errors = result.get("errors", [])
        status.not_calibrated = "not_calibrated" in errors
        status.not_mounted = "not_mounted" in errors
        status.battery_low = "battery_low" in errors
        status.ext_temp_missing = "ext_temp_missing" in errors

        boost = result.get("boost")
        if boost:
            status.boost_active = True
            status.boost_started_at = boost.get("started_at")
            status.boost_duration = boost.get("duration")

        override = result.get("override")
        if override:
            status.override_active = True
            status.override_started_at = override.get("started_at")
            status.override_duration = override.get("duration")

        return status

    async def async_set_target_temperature(self, target_c: float) -> bool:
        """Set target temperature."""
        result = await self.async_rpc_call(
            "TRV.SetTarget", {"id": 0, "target_C": target_c}
        )
        return result is not None or result is None  # null result = success

    async def async_set_external_temperature(
        self, temp_c: float | None = None
    ) -> bool:
        """Set or clear external temperature sensor value."""
        params: dict[str, Any] = {"id": 0}
        if temp_c is not None:
            params["t_C"] = temp_c
        result = await self.async_rpc_call("TRV.SetExternalTemperature", params)
        # A null result indicates success for this command
        return True

    async def async_set_boost(
        self, duration: int = DEFAULT_BOOST_DURATION
    ) -> bool:
        """Start boost mode (valve 100% open)."""
        await self.async_rpc_call("TRV.SetBoost", {"id": 0, "duration": duration})
        return True

    async def async_clear_boost(self) -> bool:
        """Stop boost mode."""
        await self.async_rpc_call("TRV.ClearBoost", {"id": 0})
        return True

    async def async_set_override(
        self, target_c: float, duration: int = 3600
    ) -> bool:
        """Set temporary target temperature override."""
        await self.async_rpc_call(
            "TRV.SetOverride",
            {"id": 0, "target_C": target_c, "duration": duration},
        )
        return True

    async def async_clear_override(self) -> bool:
        """Clear target temperature override."""
        await self.async_rpc_call("TRV.ClearOverride", {"id": 0})
        return True

    async def async_calibrate(self) -> bool:
        """Run stepper motor calibration."""
        await self.async_rpc_call("TRV.Calibrate", {"id": 0})
        return True

    async def async_set_position(self, pos: int) -> bool:
        """Set manual valve position (disables thermostat)."""
        await self.async_rpc_call("TRV.SetPosition", {"id": 0, "pos": pos})
        return True

    async def async_get_config(self) -> dict[str, Any] | None:
        """Get TRV configuration."""
        return await self.async_rpc_call("Trv.GetConfig", {"id": 0})

    async def async_set_config(self, config: dict[str, Any]) -> bool:
        """Set TRV configuration."""
        params = {"id": 0, "config": config}
        await self.async_rpc_call("Trv.SetConfig", params)
        return True

    async def async_set_enable(self, enable: bool) -> bool:
        """Enable or disable the thermostat."""
        return await self.async_set_config({"enable": enable})

    async def async_set_time(self) -> bool:
        """Sync device time to current UTC."""
        utc_now = int(time.time())
        await self.async_rpc_call(
            "Sys.SetTime", {"unixtime": utc_now, "tz": "UTC0"}
        )
        return True

    async def async_show_message(self, message: str) -> bool:
        """Display a message on the TRV screen (max 10 chars)."""
        await self.async_rpc_call(
            "Trv.ShowMessage", {"id": 0, "msg": message[:10]}
        )
        return True

    async def async_reboot(self) -> bool:
        """Reboot the device."""
        await self.async_rpc_call("Shelly.Reboot")
        return True
