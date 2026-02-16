"""Constants for the Shelly BLU TRV integration."""

DOMAIN = "shelly_blu_trv"
MANUFACTURER = "Shelly"

# BLE UUIDs - Shelly RPC over BLE
SHELLY_RPC_SERVICE_UUID = "5f6d4f53-5f52-5043-5f53-56435f49445f"
SHELLY_RPC_DATA_UUID = "5f6d4f53-5f52-5043-5f64-6174615f5f5f"
SHELLY_RPC_TX_CTL_UUID = "5f6d4f53-5f52-5043-5f74-785f63746c5f"
SHELLY_RPC_RX_CTL_UUID = "5f6d4f53-5f52-5043-5f72-785f63746c5f"

# Additional BLE characteristic UUIDs
SHELLY_UNIX_TIME_UUID = "d56a3410-115e-41d1-945b-3a7f189966a1"

# Shelly BLE manufacturer ID (Allterco Robotics)
SHELLY_MANUFACTURER_ID = 0x0BA9  # 2985

# BTHome object IDs
BTHOME_PACKET_ID = 0x00
BTHOME_BATTERY = 0x01
BTHOME_TEMPERATURE = 0x45
BTHOME_BUTTON_EVENT = 0x3A

# Config entry keys
CONF_DEVICE_ADDRESS = "device_address"
CONF_DEVICE_NAME = "device_name"
CONF_DEVICE_MODEL = "device_model"
CONF_DEVICE_FW = "device_fw"

# TRV temperature limits
TRV_MIN_TEMP = 5.0
TRV_MAX_TEMP = 30.0
TRV_TEMP_STEP = 0.5

# Default boost duration in seconds (30 minutes)
DEFAULT_BOOST_DURATION = 1800

# Polling interval in seconds
POLL_INTERVAL = 300  # 5 minutes for full RPC status poll

# BLE connection idle timeout in seconds
BLE_IDLE_TIMEOUT = 10

# Platforms
PLATFORMS = ["climate", "sensor", "binary_sensor", "button", "number"]
