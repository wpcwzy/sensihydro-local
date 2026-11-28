"""Self-test for the locally decoded run-time parameters (no HA required).

Run with:  python3 tests/test_param_decoding.py

The parameter table used to be created from the vendor cloud only; when that
slow endpoint failed during a Home Assistant restart all diagnostic entities
disappeared.  The static table (params.py) plus the local Modbus decoding
(outdoor.py) must therefore stay complete and match the cloud's wording.
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CC = os.path.join(ROOT, "custom_components", "sensihydro")


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(CC, filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


params = _load("sensihydro_params", "params.py")
outdoor = _load("sensihydro_outdoor", "outdoor.py")

DEVICE_CODES = [
    "val100", "val101", "val102", "val103", "val104", "val105", "val106",
    "val107", "val108", "val109", "val110", "val111", "val112", "val113",
    "val114", "val115", "val116", "val117", "val118", "val119", "val120",
    "val121", "val122", "val123", "val124",
]
UNIT_CODES = [
    "val200", "val201", "val202", "val203", "val204", "val205", "val206",
    "val207", "val208", "val209", "val210", "val213",
]

# raw register value -> expected cloud-style text (verified on real hardware)
DEVICE_SAMPLES = {
    0: (3990, "9.9℃"),      # TA
    1: (5620, "26.2℃"),     # TC
    6: (0, "水流开关闭合"),   # the register is 1 while the switch is OPEN
    7: (1, "ON"),
    9: (1, "ON"),
    10: (0, "OFF"),
    11: (1028, "1028"),     # outdoor power board version
    12: (2, "制热模式"),
    13: (2200, "220V"),
    14: (224, "22.4A"),
    15: (60, "60P"),
    16: (89, "89HZ"),
    22: (61, "6.1Bar"),
    23: (140, "14Bar"),
    24: (0, "未除霜"),
}


def main() -> int:
    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    # --- static definitions ------------------------------------------------
    device = params.device_param_defs()
    unit = params.unit_param_defs()
    check(set(device) == set(DEVICE_CODES), "device definition codes changed")
    check(set(unit) == set(UNIT_CODES), "unit definition codes changed")
    check(all(item.get("value") is None for item in device.values()), "defs must start empty")

    # --- register formatters ----------------------------------------------
    codes = {code for code, _ in outdoor.PARAM_FORMATTERS.values()}
    check(codes == set(DEVICE_CODES), "PARAM_FORMATTERS must cover val100..val124")
    check(len(outdoor.PARAM_FORMATTERS) == len(DEVICE_CODES), "duplicate index mapping")

    for index, (raw, expected) in DEVICE_SAMPLES.items():
        code, formatter = outdoor.PARAM_FORMATTERS[index]
        value = formatter(raw)
        check(value == expected, f"{code} raw={raw}: {value!r} != {expected!r}")

    # water flow switch polarity: 0 = closed, 1 = open
    _, flow = outdoor.PARAM_FORMATTERS[6]
    check(flow(0) == "水流开关闭合", "water flow 0 must be closed")
    check(flow(1) == "水流开关断开", "water flow 1 must be open")

    # decode every code with a plausible raw value to make sure nothing raises
    for index, (code, formatter) in outdoor.PARAM_FORMATTERS.items():
        try:
            formatter(3000 if index < 6 else 0)
        except Exception as err:  # noqa: BLE001 - the test reports the failure
            failures.append(f"{code} raised {err!r}")

    if failures:
        print("PARAM DECODING TEST: FAIL")
        for item in failures:
            print("  -", item)
        return 1
    print(f"PARAM DECODING TEST: PASS ({len(DEVICE_CODES)} device + {len(UNIT_CODES)} unit params)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
