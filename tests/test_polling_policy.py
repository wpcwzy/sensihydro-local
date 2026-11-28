"""Polling policy: fast over the LAN, relaxed through the cloud."""
import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CC = os.path.join(ROOT, "custom_components", "sensihydro")


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(CC, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


poll = _load("sensihydro_polling", "polling.py")


def main() -> int:
    failures = []

    def check(condition, message):
        if not condition:
            failures.append(message)

    # LAN mode always polls at the fast interval
    check(poll.scan_interval(True, 45) == 15, "LAN mode must poll every 15 s")
    check(poll.scan_interval(True, 600) == 15, "LAN mode ignores cloud interval")
    # cloud mode honours the user option within the documented bounds
    check(poll.scan_interval(False, 45) == 45, "cloud mode default must stay 45 s")
    check(poll.scan_interval(False, 120) == 120, "cloud mode must honour the option")
    check(poll.scan_interval(False, 0) == 15, "cloud mode lower bound is 15 s")
    check(poll.scan_interval(False, 9999) == 600, "cloud mode upper bound is 600 s")
    # in LAN mode the cloud is only refreshed every 60 s (or the user interval)
    check(poll.cloud_refresh_interval(True, 45) == 60, "LAN mode cloud refresh >= 60 s")
    check(poll.cloud_refresh_interval(True, 300) == 300, "explicit slow interval wins")
    check(poll.cloud_refresh_interval(False, 45) == 45, "cloud mode refreshes every poll")

    if failures:
        print("POLLING POLICY TEST: FAIL")
        for item in failures:
            print("  -", item)
        return 1
    print("POLLING POLICY TEST: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
