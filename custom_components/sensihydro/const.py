"""Constants for the SensiHydro (艾默生 心海悦) integration."""

from __future__ import annotations

from typing import Final

from homeassistant.components.climate import HVACMode

DOMAIN: Final = "sensihydro"
NAME: Final = "SensiHydro"
MANUFACTURER: Final = "Emerson / Copeland"
MODEL: Final = "Sensi Hydro"

VERSION: Final = "1.0.0"

# Configuration keys
CONF_DEVICE_ID: Final = "device_id"
CONF_DEVICE_SERIALNO: Final = "device_serialno"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_NOMINAL_VOLTAGE: Final = "nominal_voltage"
CONF_LOCAL_API: Final = "local_api_url"

# Cloud API
API_BASE: Final = "https://sensihydro.copeland.cn/api/v2"
API_TIMEOUT: Final = 40
#: The run-time parameter table endpoint is much slower than the rest of the
#: API (10-40 s); give it its own, generous timeout.
PARAM_TIMEOUT: Final = 75

# Token storage
STORAGE_VERSION: Final = 1
STORAGE_KEY: Final = f"{DOMAIN}.session"

DEFAULT_DEVICE_ID: Final = 3278
# Fallback AC voltage used for the estimated input power when the outdoor
# unit reports 0 V (some firmwares do not measure the AC input voltage).
DEFAULT_NOMINAL_VOLTAGE: Final = 220.0
MIN_VALID_VOLTAGE: Final = 50.0
DEFAULT_SCAN_INTERVAL: Final = 45
# LAN bridge (docker container on the NAS).  Empty string disables local control.
DEFAULT_LOCAL_API: Final = "http://192.168.110.28:8090"
LOCAL_API_TIMEOUT: Final = 6
MIN_SCAN_INTERVAL: Final = 15
MAX_SCAN_INTERVAL: Final = 600

# Re-login when the token expires within this many seconds (6 hours)
TOKEN_REFRESH_MARGIN: Final = 6 * 60 * 60
# Persist the (rotating) token at most once a day
TOKEN_SAVE_INTERVAL: Final = 24 * 60 * 60

# Thermostat limits
MIN_TEMP: Final = 5.0
MAX_TEMP: Final = 32.0
TEMP_STEP: Final = 0.5

# modifyVal actions of POST /unit/update
MODIFY_NAME: Final = 0
MODIFY_POWER: Final = 1
MODIFY_MODE: Final = 2
MODIFY_SLEEP: Final = 3
MODIFY_TEMPERATURE: Final = 4
MODIFY_WIND: Final = 5

# Device modes
MODE_COOL: Final = 0
MODE_FAN: Final = 1
MODE_HEAT: Final = 2
MODE_FLOOR: Final = 3
MODE_HEAT_FLOOR: Final = 4
MODE_DRY: Final = 5

MODE_NAMES: Final[dict[int, str]] = {
    MODE_COOL: "冷风",
    MODE_FAN: "送风",
    MODE_HEAT: "热风",
    MODE_FLOOR: "地暖",
    MODE_HEAT_FLOOR: "热+暖",
    MODE_DRY: "除湿",
}

# Mode 3 (地暖) and 4 (热+暖) share the HVAC heat range; expose the
# difference through preset modes so HomeKit gets heat/cool/auto too.
MODE_TO_HVAC: Final[dict[int, HVACMode]] = {
    MODE_COOL: HVACMode.COOL,
    MODE_FAN: HVACMode.FAN_ONLY,
    MODE_HEAT: HVACMode.HEAT,
    MODE_FLOOR: HVACMode.HEAT,
    MODE_HEAT_FLOOR: HVACMode.HEAT_COOL,
    MODE_DRY: HVACMode.DRY,
}

HVAC_TO_MODE: Final[dict[HVACMode, int]] = {
    HVACMode.COOL: MODE_COOL,
    HVACMode.FAN_ONLY: MODE_FAN,
    HVACMode.HEAT: MODE_HEAT,
    HVACMode.HEAT_COOL: MODE_HEAT_FLOOR,
    HVACMode.DRY: MODE_DRY,
}

PRESET_FLOOR: Final = "地暖"
PRESET_HEAT_FLOOR: Final = "热+暖"

# Fan speed: 0 == auto (windModel=1), 1..10 == manual gears (windModel=0)
FAN_AUTO: Final = "auto"
FAN_MODES: Final[list[str]] = [FAN_AUTO] + [str(gear) for gear in range(1, 11)]
