# Shelly BLU TRV Integration for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

A custom Home Assistant integration for **Shelly BLU TRV** (Thermostatic Radiator Valve) devices. Communicates directly via Bluetooth using Home Assistant's built-in Bluetooth proxy support — no Shelly cloud or gateway required.

## Features

- **Climate entity** — set target temperature, switch between heat/off modes, activate boost preset
- **Sensor entities** — battery level, current temperature, target temperature, external temperature, valve position, RSSI
- **Binary sensor entities** — battery low, not calibrated, not mounted, boost active, override active
- **Button entities** — calibrate valve, sync time, identify (show message on display)
- **External temperature feed** — number entity to push room temperature from an external sensor to the TRV for more accurate regulation
- **BTHome advertisements** — passive sensor updates every ~8 seconds without connecting
- **RPC-over-BLE** — active polling and commands via JSON-RPC 2.0 over GATT
- **Multi-device support** — each TRV is a separate config entry, connections are serialized to avoid overwhelming the BT proxy

## Requirements

- Home Assistant 2024.1.0 or newer
- At least one **Bluetooth Proxy** in range of your TRV(s) (e.g. a ESPHome ESP32 bluetooth proxy)
  - Other Shelly devices (1PM Pro, Plus, etc.) only forward BTHome advertisements — they cannot proxy active BLE connections
  - HA's built-in Bluetooth adapter also works if the machine running HA has one

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click the three dots menu (top right) → **Custom repositories**
3. Add `https://github.com/reinhard-brandstaedter/hass_shelly_blu_trv` as an **Integration**
4. Search for "Shelly BLU TRV" and install
5. Restart Home Assistant

### Manual

1. Copy `custom_components/shelly_blu_trv/` to your Home Assistant `config/custom_components/` directory
2. Restart Home Assistant

## Setup

### Pairing

Before adding the integration, you need to pair each TRV with your ESPHome Bluetooth Proxy:

1. On the TRV, turn the knob **4 times clockwise/ccw back/forth** within 10 seconds to enter the menu and then turn the know to enter BLE pairing mode (the display will show "bLe" blinking)
2. The TRV stays in pairing mode for ~30 seconds

### Adding a device

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Shelly BLU TRV**
3. Select your discovered TRV from the list
4. Enter a device name
5. The integration will attempt to connect and verify communication

### External temperature feed

The TRV has a built-in temperature sensor, but it reads higher than actual room temperature due to proximity to the radiator. You can feed a more accurate room temperature from an external sensor using an automation:

```yaml
alias: Push room temperature to TRV
description: Send room temperature to TRVs every 5 minutes
triggers:
  - trigger: time_pattern
    minutes: /5
conditions:
  - condition: not
    conditions:
      - condition: state
        entity_id: sensor.tempsensor_wohnzimmer_temperature
        state: unavailable
actions:
  - action: number.set_value
    target:
      entity_id:
        - number.shelly_blu_trv_wz1_external_temperature
    data:
      value: "{{ states('sensor.tempsensor_wohnzimmer_temperature') | float }}"
mode: single
```

## Architecture

```
BTHome Advertisements (passive, every ~8s)
  TRV → ESPHome BT Proxy → HA Bluetooth → Coordinator → Entities

RPC Commands (active, on-demand + 5min poll)
  HA → Coordinator → BLE Client → ESPHome BT Proxy → TRV
```

- **Passive path**: BTHome v2 advertisements are received through any Bluetooth adapter/proxy without connecting. These provide battery, temperatures, and button events.
- **Active path**: RPC commands (set temperature, get full status, calibrate, etc.) require a BLE connection through a bonded proxy. A global lock serializes connections across all TRV instances to avoid overwhelming the proxy.

## Troubleshooting

### "Unknown error (19)" — stale BLE bond

If you see `BluetoothGATTErrorResponse: Unknown error (19)` in the logs, the BLE bond between the ESP32 proxy and the TRV has gone stale. Fix by re-pairing:

1. Turn the TRV knob **4 times clockwise/ccw** within 10 seconds to enter the menu and then turn the know to enter BLE pairing mode
2. Wait for the next connection attempt (or restart the integration)

### TRV supports max 4 bonded peers

If you have multiple BT proxies, each TRV needs to be paired with each proxy that might be used to send commands. The TRV supports up to 4 bonded devices.

### Valve position shows "unknown"

The valve position is only available through active RPC polling (every 5 minutes by default). If the connection fails, the last known value is preserved. The position will update on the next successful poll.

## License

MIT
