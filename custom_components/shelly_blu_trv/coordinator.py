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
    async_scanner_devices_by_address,
)
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN, POLL_INTERVAL

CONFIG_POLL_INTERVAL = 3600  # Re-fetch Trv.GetConfig at most once per hour
EXT_TEMP_MIN_INTERVAL = 60    # Minimum seconds between SetExternalTemperature BLE calls
EXT_TEMP_MIN_DELTA = 0.3      # Minimum °C change required to send a new value
EXT_TEMP_KEEPALIVE = 600      # Force resend after this many seconds even if value unchanged
                               # Prevents the TRV's ext_temp_missing timeout (~32 min observed)
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

# How long to wait for a per-proxy semaphore slot for user-initiated commands.
# Polls wait indefinitely (see below) because skipping a poll resets the base
# class timer and delays the next attempt by a full POLL_INTERVAL.  Commands
# fail after CMD_LOCK_TIMEOUT to avoid hanging the HA UI indefinitely.
CMD_LOCK_TIMEOUT = 120.0   # seconds: fail user command if BLE is busy

# Per-proxy semaphore pool – allows up to PROXY_SLOTS simultaneous BLE
# connections through the same BT proxy while letting TRVs on different
# proxies connect fully in parallel.  ESP32C3 proxies have 3 slots; we
# reserve one for other HA bluetooth activity, leaving 2 for ourselves.
PROXY_SLOTS = 2
_proxy_semaphores: dict[str, asyncio.Semaphore] = {}
_fallback_semaphore = asyncio.Semaphore(1)  # conservative when proxy is unknown


def _get_proxy_semaphore(proxy_source: str | None) -> asyncio.Semaphore:
    """Return the semaphore for *proxy_source*, creating it on first use."""
    if proxy_source is None:
        return _fallback_semaphore
    if proxy_source not in _proxy_semaphores:
        _proxy_semaphores[proxy_source] = asyncio.Semaphore(PROXY_SLOTS)
    return _proxy_semaphores[proxy_source]


# Sentinel returned by async_rpc_command when all retries are exhausted.
# Distinct from None, which is a valid successful result for RPC methods
# that return null (e.g. TRV.SetExternalTemperature).
COMMAND_FAILED = object()


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
        preferred_proxy: str | None = None,
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
        self._last_config_poll: float = 0
        self._preferred_proxy: str | None = preferred_proxy
        self._last_ext_temp_sent: float | None = None
        self._last_ext_temp_time: float = 0

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

        # Update BLE device reference — but only when no preferred proxy is
        # configured.  service_info.device reflects whichever proxy triggered
        # the advertisement (could be the wrong one for bonded devices).
        # _refresh_ble_device() called inside the retry loop handles the
        # preferred-proxy case correctly.
        if not self._preferred_proxy:
            self.ble_device = service_info.device
            self._client.set_ble_device(service_info.device)

        # --- Status poll (TRV.GetStatus) ---
        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            # Always refresh the BLE device reference before each attempt.
            # When a preferred proxy is set, service_info.device is intentionally
            # not applied (it may be from the wrong proxy), so _refresh_ble_device
            # is the only place the correct proxy device gets set.
            self._refresh_ble_device()

            # Wait indefinitely for the per-proxy semaphore.  Skipping a poll
            # (returning early) resets the base class poll timer, which would
            # delay the next attempt by a full POLL_INTERVAL – causing the
            # valve position to stay empty.  Waiting is safe: the disconnect
            # timeout in shelly_ble.py bounds the worst-case lock hold time.
            try:
                async with _get_proxy_semaphore(self._resolve_proxy_source()):
                    await self._client.connect()
                    status = await self._client.async_get_status()
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
                if status.ext_temp_missing:
                    # TRV has dropped the external temperature — reset the
                    # keepalive timer so the next automation invocation
                    # bypasses the delta check and resends immediately.
                    _LOGGER.debug(
                        "ext_temp_missing detected for %s — resetting ext temp timer",
                        self.device_name,
                    )
                    self._last_ext_temp_time = 0
                old.boost_active = status.boost_active
                old.boost_started_at = status.boost_started_at
                old.boost_duration = status.boost_duration
                old.override_active = status.override_active
                old.override_started_at = status.override_started_at
                old.override_duration = status.override_duration

                self.state.last_rpc_poll = time.time()
                _LOGGER.debug(
                    "Poll result for %s: target=%.1f, current=%.1f, pos=%s",
                    self.device_name,
                    old.target_C or 0,
                    old.current_C or 0,
                    old.pos,
                )
                break  # Status poll succeeded
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
        else:
            _LOGGER.warning(
                "Failed to poll %s after %d attempts: %s",
                self.device_name,
                MAX_RETRIES,
                last_error,
            )
            return

        # --- Config poll (Trv.GetConfig) — separate connection, best-effort ---
        # Kept separate to avoid stale RX_CTL state from the status call
        # corrupting the config response when sharing the same BLE connection.
        # Only fetched on first poll (values still None) or once per hour.
        now = time.time()
        needs_config = (
            self.state.status.min_valve_position is None
            or (now - self._last_config_poll) >= CONFIG_POLL_INTERVAL
        )
        if needs_config:
            config = await self.async_rpc_command("Trv.GetConfig", {"id": 0})
            _LOGGER.debug("Trv.GetConfig result for %s: %s", self.device_name, config)
            if config and isinstance(config, dict):
                old = self.state.status
                if config.get("min_valve_position") is not None:
                    old.min_valve_position = config["min_valve_position"]
                # Flags can come back as either:
                #   list of active flag name strings: ["floor_heating", "anticlog"]
                #   list with one dict (as per older docs): [{"floor_heating": true, ...}]
                flags_raw = config.get("flags", [])
                if isinstance(flags_raw, list) and flags_raw and isinstance(flags_raw[0], dict):
                    # Dict style: [{"floor_heating": true, "silent_mode": false, ...}]
                    flags_dict = flags_raw[0]
                    old.floor_heating = flags_dict.get("floor_heating", False)
                    old.silent_mode = flags_dict.get("silent_mode", False)
                elif isinstance(flags_raw, list):
                    # String list style: ["floor_heating", "anticlog"] — active flags only
                    old.floor_heating = "floor_heating" in flags_raw
                    old.silent_mode = "silent_mode" in flags_raw
                else:
                    _LOGGER.debug(
                        "Unexpected flags format in Trv.GetConfig for %s: %r",
                        self.device_name,
                        flags_raw,
                    )
                self._last_config_poll = now

    @callback
    def _async_handle_bluetooth_event(
        self,
        service_info: BluetoothServiceInfoBleak,
        change: Any,
    ) -> None:
        """Handle a bluetooth event (advertisement received)."""
        # Only update the device reference when this advertisement arrived via
        # the preferred proxy (or when no preferred proxy is configured).
        # Advertisements from other proxies must not silently replace the
        # device reference — doing so causes the next connection attempt to
        # route through the wrong proxy, breaking bonded TRVs.
        if not self._preferred_proxy or service_info.source == self._preferred_proxy:
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

    def set_preferred_proxy(self, proxy_source: str | None) -> None:
        """Update the preferred BT proxy at runtime (called from options listener)."""
        self._preferred_proxy = proxy_source
        _LOGGER.debug(
            "Preferred proxy for %s set to: %s",
            self.device_name,
            proxy_source or "auto",
        )

    def _resolve_proxy_source(self) -> str | None:
        """Return the BT proxy source string used for semaphore selection.

        Uses the preferred proxy when configured.  Falls back to whichever
        scanner currently sees the device so that TRVs without a preferred
        proxy can still benefit from per-proxy parallelism.
        """
        if self._preferred_proxy:
            return self._preferred_proxy
        for sd in async_scanner_devices_by_address(
            self.hass, self._client.address, connectable=True
        ):
            return sd.scanner.source
        return None

    def _refresh_ble_device(self) -> None:
        """Fetch a fresh BLE device reference from HA's bluetooth stack and
        update the BLE client's proxy source hint for log messages.

        If a preferred proxy is configured, use it exclusively — never fall
        back to a different proxy.  TRVs are BLE-bonded to a specific proxy
        hardware; connecting through any other proxy will fail authentication.
        If the preferred proxy has not seen a recent advertisement, keep the
        existing device reference so the next connection attempt still targets
        the correct proxy rather than silently switching to the wrong one.
        """
        # Always keep the proxy source hint in sync so BLE log messages show
        # which proxy is being used for this attempt.
        self._client.set_source_hint(self._resolve_proxy_source())

        if self._preferred_proxy:
            for sd in async_scanner_devices_by_address(
                self.hass, self._client.address, connectable=True
            ):
                if sd.scanner.source == self._preferred_proxy:
                    self.ble_device = sd.ble_device
                    self._client.set_ble_device(sd.ble_device)
                    return
            # Preferred proxy has no recent advertisement for this device.
            # Keep the existing device reference (last known via the correct
            # proxy) and let the connection attempt fail cleanly rather than
            # silently routing through a different proxy that cannot authenticate.
            _LOGGER.warning(
                "Preferred proxy %s has no recent advertisement for %s "
                "— keeping existing device reference, skipping fallback",
                self._preferred_proxy,
                self.device_name,
            )
            return

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
        _LOGGER.info(
            "Setting target temperature for %s to %.1f°C",
            self.device_name,
            target_c,
        )
        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            self._refresh_ble_device()

            proxy_sem = _get_proxy_semaphore(self._resolve_proxy_source())
            try:
                await asyncio.wait_for(
                    proxy_sem.acquire(), timeout=CMD_LOCK_TIMEOUT
                )
            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "BLE proxy busy for >%.0fs, cannot set target temperature for %s",
                    CMD_LOCK_TIMEOUT,
                    self.device_name,
                )
                return False

            try:
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
                        self.async_update_listeners()
                        _LOGGER.info(
                            "Target temperature verified for %s: %.1f°C "
                            "(outer attempt %d/%d, verify %d/%d)",
                            self.device_name,
                            status.target_C,
                            attempt,
                            MAX_RETRIES,
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
                _LOGGER.warning(
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
            finally:
                proxy_sem.release()

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

    async def async_set_external_temperature(self, temp_c: float) -> None:
        """Push external temperature to the TRV, with debouncing and keepalive.

        The TRV is often in a low-power sleep state between advertisements
        and rejects connections when bombarded with repeated requests.
        Automations that feed an external sensor can fire every 30-60 s, so
        we gate BLE calls to at most once per EXT_TEMP_MIN_INTERVAL seconds
        and only when the value has shifted by more than EXT_TEMP_MIN_DELTA.

        Additionally, even if the value is stable we force a resend every
        EXT_TEMP_KEEPALIVE seconds to refresh the TRV's internal expiry
        timer and prevent it from entering ext_temp_missing state.
        """
        now = time.time()
        time_since_last = now - self._last_ext_temp_time
        keepalive_due = time_since_last >= EXT_TEMP_KEEPALIVE

        if not keepalive_due and (
            self._last_ext_temp_sent is not None
            and abs(self._last_ext_temp_sent - temp_c) < EXT_TEMP_MIN_DELTA
        ):
            _LOGGER.debug(
                "External temp %.1f°C unchanged (last: %.1f°C, delta < %.1f°C), skipping BLE for %s",
                temp_c,
                self._last_ext_temp_sent,
                EXT_TEMP_MIN_DELTA,
                self.device_name,
            )
            return

        if time_since_last < EXT_TEMP_MIN_INTERVAL:
            _LOGGER.debug(
                "External temp update too recent (%.0fs ago, min %ds), skipping BLE for %s",
                time_since_last,
                EXT_TEMP_MIN_INTERVAL,
                self.device_name,
            )
            return

        if keepalive_due and self._last_ext_temp_sent is not None:
            _LOGGER.debug(
                "External temp keepalive for %s: resending %.1f°C after %.0fs",
                self.device_name,
                temp_c,
                time_since_last,
            )

        # Pre-update tracking so rapid back-to-back invocations are gated
        # even if the first call is still in-flight.  If the call ultimately
        # fails we revert _last_ext_temp_sent to the previously confirmed
        # value so future calls compare against what the TRV actually has,
        # not a value that was never delivered.
        prev_sent = self._last_ext_temp_sent
        self._last_ext_temp_sent = temp_c
        self._last_ext_temp_time = now

        result = await self.async_rpc_command(
            "TRV.SetExternalTemperature", {"id": 0, "t_C": temp_c}
        )

        if result is COMMAND_FAILED:
            _LOGGER.warning(
                "Failed to deliver external temp %.1f°C to %s — "
                "reverting debounce state so next call will retry",
                temp_c,
                self.device_name,
            )
            self._last_ext_temp_sent = prev_sent
            self._last_ext_temp_time = 0  # don't rate-limit the retry

    async def async_rpc_command(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Execute an RPC command with retries (connect, send, disconnect).

        Uses a per-proxy semaphore (PROXY_SLOTS slots) to limit concurrent
        BLE connections through the same BT proxy while allowing TRVs on
        different proxies to proceed in parallel.
        Connection failures are logged but not raised for non-critical
        commands to avoid crashing the websocket API.
        """
        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            # Get fresh BLE device reference before each attempt
            self._refresh_ble_device()

            proxy_sem = _get_proxy_semaphore(self._resolve_proxy_source())
            try:
                await asyncio.wait_for(
                    proxy_sem.acquire(), timeout=CMD_LOCK_TIMEOUT
                )
            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "BLE proxy busy for >%.0fs, cannot execute %s for %s",
                    CMD_LOCK_TIMEOUT,
                    method,
                    self.device_name,
                )
                return COMMAND_FAILED

            try:
                await self._client.connect()
                result = await self._client.async_rpc_call(method, params)
                await self._client.disconnect()
                return result
            except Exception as err:
                last_error = err
                _LOGGER.warning(
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
            finally:
                proxy_sem.release()

            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)

        _LOGGER.warning(
            "RPC %s failed for %s after %d attempts: %s",
            method,
            self.device_name,
            MAX_RETRIES,
            last_error,
        )
        return COMMAND_FAILED
