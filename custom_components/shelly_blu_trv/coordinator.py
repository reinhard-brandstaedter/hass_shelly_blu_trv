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
from homeassistant.components.persistent_notification import async_create as pn_create
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

MAX_RETRIES = 2
RETRY_DELAY = 10  # seconds between retries
DEVICE_STARTUP_TIMEOUT = 300

# How long to wait for a per-proxy semaphore slot for user-initiated commands.
# Polls wait indefinitely (see below) because skipping a poll resets the base
# class timer and delays the next attempt by a full POLL_INTERVAL.  Commands
# fail after CMD_LOCK_TIMEOUT to avoid hanging the HA UI indefinitely.
CMD_LOCK_TIMEOUT = 120.0   # seconds: fail user command if BLE is busy

# How long to back off after an auth failure (error 19) before retrying.
# Short enough that re-pairing works without a restart: user puts TRV in
# pairing mode, then saves integration options (which resets the timer) and
# the next advertisement triggers a fresh connection attempt.
# set_preferred_proxy() also resets the timer for the same reason.
AUTH_RETRY_INTERVAL = 1800  # 30 minutes

# Per-proxy semaphore pool – serialises BLE connections through the same
# BT proxy while letting TRVs on different proxies connect in parallel.
# PROXY_SLOTS=1: all 3 TRVs share one proxy; allowing 2 concurrent
# connections caused proxy overload (20 s timeouts, cascading retries).
# 3 polls × ~3 s each = ~9 s total, well within the 5-min poll interval.
PROXY_SLOTS = 1
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


def _is_auth_error(err: Exception) -> bool:
    """Return True when *err* is a BLE authentication / bonding failure.

    Covers two distinct failure modes:

    1. Error 19 (HCI 0x13 "Remote User Terminated Connection"): proxy has no
       bonding keys for the TRV, so the TRV drops the connection during GATT
       discovery or after a write.

    2. GATT error 259 (0x103, ATT "Write Not Permitted"): the proxy has stale/
       mismatched bonding keys — it connects successfully but the link is never
       encrypted, so the TRV rejects writes to secured characteristics.

    Both require re-pairing to fix.  Backing off stops the repeated connection
    attempts that wear the proxy's NVS bond entry and accelerate corruption.

    Surfaces by BT backend:
    - Linux/BlueZ:    OSError errno 19, or message contains "error 19" /
                      "[errno 19]" / "authentication" / "unauthoriz"
    - ESPHome proxy:  "ESP_GATT_CONN_TERMINATE_PEER_USER",
                      "Unknown error (19)", or "error=259"
    """
    if isinstance(err, OSError) and err.errno == 19:
        return True
    msg = str(err).lower()
    return (
        "error 19" in msg
        or "[errno 19]" in msg
        or "unknown error (19)" in msg
        or "terminate_peer_user" in msg
        or "authentication" in msg
        or "unauthoriz" in msg
        or "error=259" in msg
    )


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
        self._device_lock = asyncio.Lock()  # serialises all BLE ops on this TRV
        self._last_poll_time: float = 0
        self._last_config_poll: float = 0
        self._preferred_proxy: str | None = preferred_proxy
        self._last_ext_temp_sent: float | None = None
        self._last_ext_temp_time: float = 0
        self._auth_failed_at: float = 0  # epoch time of last auth failure; 0 = never
        self._bond_failure_notified: bool = False  # True after HA notification sent
        self._probe_in_progress: bool = True  # True until startup probe completes,
        # blocking polls from firing before the probe has had a chance to run.
        # Set back to False in async_startup_probe() finally block.

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
        if self._probe_in_progress:
            return False  # don't race with the startup probe for _device_lock
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
            self._client.set_ble_device(service_info.device, rssi=service_info.rssi)

        # --- Status poll (TRV.GetStatus) ---
        if self._auth_failed_at and (time.time() - self._auth_failed_at) < AUTH_RETRY_INTERVAL:
            _LOGGER.debug(
                "Skipping poll for %s: auth failure %.0f min ago, retrying after %d min "
                "(save integration options to reset sooner)",
                self.device_name,
                (time.time() - self._auth_failed_at) / 60,
                AUTH_RETRY_INTERVAL // 60,
            )
            return

        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            # Always refresh the BLE device reference before each attempt.
            # When a preferred proxy is set, service_info.device is intentionally
            # not applied (it may be from the wrong proxy), so _refresh_ble_device
            # is the only place the correct proxy device gets set.
            # _refresh_ble_device raises ConnectionError when the current device
            # reference points at the wrong proxy — skip the attempt in that case
            # rather than waste time on a connection that will fail auth.
            try:
                self._refresh_ble_device()
            except ConnectionError as err:
                last_error = err
                _LOGGER.debug(
                    "Poll attempt %d/%d skipped for %s: %s",
                    attempt,
                    MAX_RETRIES,
                    self.device_name,
                    err,
                )
            else:
                # Wait indefinitely for the per-proxy semaphore.  Skipping a poll
                # (returning early) resets the base class poll timer, which would
                # delay the next attempt by a full POLL_INTERVAL – causing the
                # valve position to stay empty.  Waiting is safe: the disconnect
                # timeout in shelly_ble.py bounds the worst-case lock hold time.
                async with self._device_lock:
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
                        # Successful connection — clear any auth backoff so
                        # follow-up RPCs (Trv.GetConfig, ext temp) are not blocked.
                        self._auth_failed_at = 0
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
                        if _is_auth_error(err):
                            self._auth_failed_at = time.time()
                            _LOGGER.warning(
                                "Bond/auth failure polling %s — "
                                "backing off for %d min to protect bond table. "
                                "Save integration options to retry sooner (e.g. after re-pairing).",
                                self.device_name,
                                AUTH_RETRY_INTERVAL // 60,
                            )
                            self._notify_bond_failure()
                            return

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
        # Guard also on service_info.device.details["source"]: HA may supply a
        # "merged/best" BLEDevice whose source differs from service_info.source
        # (the scanner that actually received the ad).  Updating _ble_device with
        # such a merged device silently corrupts our proxy reference.
        if not self._preferred_proxy or service_info.source == self._preferred_proxy:
            device = service_info.device
            device_source = device.details.get("source") if device else None
            if not self._preferred_proxy or device_source == self._preferred_proxy:
                self.ble_device = device
                self._client.set_ble_device(device, rssi=service_info.rssi)

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
        """Update the preferred BT proxy at runtime (called from options listener).

        Also resets the auth-failure backoff so that saving integration options
        serves as a "retry now" trigger after re-pairing — no restart required.
        """
        self._preferred_proxy = proxy_source
        if self._auth_failed_at:
            _LOGGER.debug(
                "Auth failure backoff cleared for %s (options saved)",
                self.device_name,
            )
            self._auth_failed_at = 0
        self._bond_failure_notified = False
        _LOGGER.debug(
            "Preferred proxy for %s set to: %s",
            self.device_name,
            proxy_source or "auto",
        )

    def _notify_bond_failure(self) -> None:
        """Fire a HA persistent notification when a bond mismatch is detected.

        Only fires once per bond-failure event; reset by set_preferred_proxy()
        (called when the user saves integration options to trigger re-pairing).
        """
        if self._bond_failure_notified:
            return
        self._bond_failure_notified = True
        notification_id = f"shelly_blu_trv_bond_{self.base_unique_id}"
        pn_create(
            self.hass,
            (
                f"**{self.device_name}** has a BLE bond mismatch — "
                "connections succeed but GATT writes are rejected (error 259 / error 19). "
                "The TRV and its BT proxy have inconsistent bonding keys.\n\n"
                "**To fix:**\n"
                "1. Factory-reset the BT proxy NVS (ESPHome `factory_reset` button)\n"
                "2. Factory-reset the TRV (hold button >20 s)\n"
                "3. Put TRV in pairing mode\n"
                "4. Disable → Enable the integration entry in HA\n\n"
                "Connections are paused for 30 min to protect the bond table. "
                "Saving integration options resets the timer immediately."
            ),
            title=f"Shelly BLU TRV: re-pairing required ({self.device_name})",
            notification_id=notification_id,
        )
        _LOGGER.warning(
            "Bond mismatch detected for %s — HA notification created (id: %s)",
            self.device_name,
            notification_id,
        )

    async def async_startup_probe(self) -> None:
        """Attempt a single BLE connection immediately after setup.

        Gives the TRV a chance to complete bonding within its 30-second pairing
        window.  Uses self._client.connect() under _device_lock so that all proxy
        source guards (source-hint check, _refresh_ble_device) are applied — this
        prevents bleak-retry-connector from internally falling back to the wrong
        proxy (e.g. tempsensor-wz) when the raw establish_connection path was used.

        _probe_in_progress suppresses advertisement-triggered polls while the
        probe holds _device_lock, so there is no deadlock or race condition.

        Does NOT set _auth_failed_at on failure so a concurrent poll can still
        succeed (e.g. TRV bonds during the poll window instead of the probe).

        Pairing flow:
          1. Put TRV in pairing mode (30 s window).
          2. Save integration options → probe fires within ~2 s.
          3. Bonding completes; subsequent polls work normally.
        """
        self._probe_in_progress = True
        _LOGGER.debug("Startup probe: attempting connection to %s", self.device_name)
        try:
            # Acquire the per-proxy semaphore so simultaneous startup probes from
            # multiple TRVs on the same proxy don't overwhelm the ESP32 BLE stack.
            # _probe_in_progress suppresses advertisement-triggered polls while we
            # hold _device_lock, so there is no deadlock risk.
            proxy_source = self._resolve_proxy_source()
            semaphore = _get_proxy_semaphore(proxy_source)
            try:
                async with semaphore:
                    async with self._device_lock:
                        # _refresh_ble_device() sets the source hint and updates
                        # the BLE device reference to the preferred proxy.  It
                        # raises ConnectionError if no valid device is available,
                        # which is caught below.  connect() then enforces the
                        # source-hint guard before calling establish_connection,
                        # preventing bleak-retry-connector from falling back to
                        # the wrong proxy (e.g. tempsensor-wz).
                        self._refresh_ble_device()
                        # Hard timeout on connect: bleak-retry-connector retries
                        # aggressively for fast failures (TERMINATE_PEER_USER)
                        # and can spend 80+ seconds on 9 internal attempts, blowing
                        # past the TRV's 30-second pairing window.  25 s gives one
                        # real connection attempt with GATT discovery headroom.
                        await asyncio.wait_for(self._client.connect(), timeout=25.0)
                        try:
                            # A GATT write is required to trigger BLE bonding.
                            # A bare connect+disconnect only does service
                            # discovery, which works without authentication and
                            # never triggers the pairing handshake.  The TRV's
                            # 30-second pairing window waits for a secured write.
                            # Sys.SetTime is the call Shelly documents for the
                            # pairing flow and also syncs the TRV clock as a
                            # useful side effect.
                            await asyncio.wait_for(
                                self._client.async_sync_time(), timeout=10.0
                            )
                            _LOGGER.debug(
                                "Startup probe RPC succeeded for %s — bonding confirmed",
                                self.device_name,
                            )
                        except Exception as rpc_err:
                            # Error 19 = TRV not in pairing mode, or bonded to
                            # a different proxy.  Any other error = transient.
                            # Either way the connection itself worked; log and
                            # continue so the subsequent poll can try normally.
                            _LOGGER.debug(
                                "Startup probe RPC failed for %s: %s",
                                self.device_name,
                                rpc_err,
                            )
                        finally:
                            await self._client.disconnect()
                if self._auth_failed_at:
                    _LOGGER.debug(
                        "Startup probe succeeded for %s — clearing auth backoff",
                        self.device_name,
                    )
                    self._auth_failed_at = 0
                else:
                    _LOGGER.debug("Startup probe succeeded for %s", self.device_name)
            except ConnectionError as err:
                _LOGGER.debug("Startup probe skipped for %s: %s", self.device_name, err)
            except Exception as err:
                _LOGGER.debug("Startup probe failed for %s: %s", self.device_name, err)
        finally:
            self._probe_in_progress = False

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
                if sd.scanner.source != self._preferred_proxy:
                    continue
                # Also verify the BLEDevice itself points at the right proxy.
                # HA may supply a merged/cached BLEDevice whose details["source"]
                # differs from the scanner source — using it would route through
                # the wrong proxy and fail auth.
                device_source = sd.ble_device.details.get("source") if sd.ble_device else None
                if device_source != self._preferred_proxy:
                    _LOGGER.debug(
                        "Skipping sd from %s: ble_device.details['source']=%s (merged/stale)",
                        sd.scanner.source,
                        device_source,
                    )
                    continue
                self.ble_device = sd.ble_device
                self._client.set_ble_device(sd.ble_device)
                return
            # Preferred proxy has no recent advertisement for this device.
            # Check whether the current device reference is already pointing at
            # the correct proxy.  If it is, keep it and attempt the connection
            # (the proxy may still be able to reach the device).  If it points
            # at a confirmed wrong proxy, raise immediately so the retry loop
            # skips this attempt.
            #
            # Special case: source=None means a merged/startup BLEDevice from
            # async_ble_device_from_address.  Rather than raising, construct a
            # pinned device reference with source=preferred_proxy so the
            # connection attempt is routed correctly.  This handles power_save
            # TRVs that advertise infrequently via the preferred proxy but are
            # seen via another proxy — the preferred proxy can still connect
            # even without a recent advertisement.  connect()'s source-hint
            # guard remains the final safety net.
            current_source = self._client.ble_device_source
            if current_source is None:
                # Build a pinned device reference pointing at the preferred proxy.
                cur = self._client._ble_device  # noqa: SLF001
                _details = dict(cur.details) if isinstance(cur.details, dict) else {}
                _details["source"] = self._preferred_proxy
                pinned = BLEDevice(cur.address, cur.name, _details)
                self.ble_device = pinned
                self._client.set_ble_device(pinned)
                _LOGGER.debug(
                    "Preferred proxy %s has no recent advertisement for %s "
                    "— pinning device reference to preferred proxy (was source=None)",
                    self._preferred_proxy,
                    self.device_name,
                )
            elif current_source != self._preferred_proxy:
                raise ConnectionError(
                    f"Preferred proxy {self._preferred_proxy} has no recent "
                    f"advertisement for {self.device_name} and current device "
                    f"reference is from {current_source} — skipping attempt to "
                    f"avoid wrong-proxy connection"
                )
            else:
                _LOGGER.debug(
                    "Preferred proxy %s has no recent advertisement for %s "
                    "— keeping existing device reference",
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
        verify_delay: float = 2.0,
    ) -> bool:
        """Set target temperature and verify the TRV accepted it.

        Uses two separate BLE connections per attempt: one to send
        TRV.SetTarget and a second to read back TRV.GetStatus. Keeping
        them separate avoids the RX_CTL stale response_length bug that
        corrupts the second RPC response when two calls share one connection.

        Returns True if verified, False if all attempts failed.
        """
        if self._auth_failed_at and (time.time() - self._auth_failed_at) < AUTH_RETRY_INTERVAL:
            _LOGGER.warning(
                "Cannot set target temperature for %s: "
                "auth failure %.0f min ago, retrying after %d min "
                "(save integration options to reset sooner)",
                self.device_name,
                (time.time() - self._auth_failed_at) / 60,
                AUTH_RETRY_INTERVAL // 60,
            )
            return False

        _LOGGER.info(
            "Setting target temperature for %s to %.1f°C",
            self.device_name,
            target_c,
        )
        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                self._refresh_ble_device()
            except ConnectionError as err:
                last_error = err
                _LOGGER.warning(
                    "Set target verified attempt %d/%d skipped for %s: %s",
                    attempt,
                    MAX_RETRIES,
                    self.device_name,
                    err,
                )
            else:
                async with self._device_lock:
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
                        # Connection 1: send the set command
                        await self._client.connect()
                        await self._client.async_rpc_call(
                            "TRV.SetTarget", {"id": 0, "target_C": target_c}
                        )
                        await self._client.disconnect()

                        # Wait for the TRV to apply the new target before reading back
                        await asyncio.sleep(verify_delay)

                        # Connection 2: read back status to verify (separate connection
                        # required — two RPCs in one connection corrupt the response via
                        # stale RX_CTL response_length)
                        self._refresh_ble_device()
                        await self._client.connect()
                        status = await self._client.async_get_status()
                        await self._client.disconnect()

                        if (
                            status.target_C is not None
                            and abs(status.target_C - target_c) < 0.1
                        ):
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
                                "(attempt %d/%d)",
                                self.device_name,
                                status.target_C,
                                attempt,
                                MAX_RETRIES,
                            )
                            return True

                        _LOGGER.warning(
                            "Target temperature mismatch for %s: "
                            "requested=%.1f, got=%.1f (attempt %d/%d)",
                            self.device_name,
                            target_c,
                            status.target_C or 0,
                            attempt,
                            MAX_RETRIES,
                        )
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
                        if _is_auth_error(err):
                            self._auth_failed_at = time.time()
                            _LOGGER.warning(
                                "Bond/auth failure setting target for %s — "
                                "backing off for %d min to protect bond table. "
                                "Save integration options to retry sooner (e.g. after re-pairing).",
                                self.device_name,
                                AUTH_RETRY_INTERVAL // 60,
                            )
                            self._notify_bond_failure()
                    finally:
                        proxy_sem.release()

                    if self._auth_failed_at:
                        return False

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
        if self._auth_failed_at and (time.time() - self._auth_failed_at) < AUTH_RETRY_INTERVAL:
            _LOGGER.warning(
                "Cannot execute RPC %s for %s: "
                "auth failure %.0f min ago, retrying after %d min "
                "(save integration options to reset sooner)",
                method,
                self.device_name,
                (time.time() - self._auth_failed_at) / 60,
                AUTH_RETRY_INTERVAL // 60,
            )
            return COMMAND_FAILED

        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            # Get fresh BLE device reference before each attempt.
            # Raises ConnectionError when the current device reference points at
            # the wrong proxy — skip rather than waste time on a doomed attempt.
            try:
                self._refresh_ble_device()
            except ConnectionError as err:
                last_error = err
                _LOGGER.warning(
                    "RPC %s attempt %d/%d skipped for %s: %s",
                    method,
                    attempt,
                    MAX_RETRIES,
                    self.device_name,
                    err,
                )
            else:
                async with self._device_lock:
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
                        if _is_auth_error(err):
                            self._auth_failed_at = time.time()
                            _LOGGER.warning(
                                "Bond/auth failure executing RPC %s for %s — "
                                "backing off for %d min to protect bond table. "
                                "Save integration options to retry sooner (e.g. after re-pairing).",
                                method,
                                self.device_name,
                                AUTH_RETRY_INTERVAL // 60,
                            )
                            self._notify_bond_failure()
                    finally:
                        proxy_sem.release()

                    if self._auth_failed_at:
                        return COMMAND_FAILED

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
