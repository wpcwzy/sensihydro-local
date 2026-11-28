"""The state cache must refresh on demand when a caller asks for fresh data."""
import importlib.util
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bridge import state as state_mod  # noqa: E402

calls = {"read": 0}


def fake_read_all(bridge):  # noqa: ARG001
    calls["read"] += 1
    return {"客厅": {"power": 1, "mode": 2, "target_temperature": 20.0}}


def fake_read_host(bridge):  # noqa: ARG001
    return {"mode": "常规", "detail": {}}


state_mod.units.read_all = fake_read_all
state_mod.hostmode.read_host_mode = fake_read_host


class FakeBridge:
    pass


def main() -> int:
    failures = []

    def check(condition, message):
        if not condition:
            failures.append(message)

    store = state_mod.StateStore(FakeBridge(), interval=5.0, active_window=5.0)

    # cold start refreshes once
    units, host, age = store.get()
    check(calls["read"] == 1, "cold start must refresh once")
    check(units and "客厅" in units, "state must be returned")
    check(host.get("mode") == "常规", "host mode must be returned")

    # fresh enough for a lenient max_age: no extra sweep
    store.get(max_age=60.0)
    check(calls["read"] == 1, "lenient max_age must not refresh")

    # stale for a strict max_age: refresh synchronously
    store.get(max_age=0.0)
    check(calls["read"] == 2, "strict max_age must refresh")

    # aged snapshot is refreshed for a normal max_age (HA polls every 15 s)
    store._ts = time.monotonic() - 30  # noqa: SLF001 - test of the internals
    store.get(max_age=8.0)
    check(calls["read"] == 3, "aged snapshot must be refreshed")
    store.get(max_age=8.0)
    check(calls["read"] == 3, "a just-refreshed snapshot must not refresh again")

    if failures:
        print("STATE CACHE TEST: FAIL")
        for item in failures:
            print("  -", item)
        return 1
    print("STATE CACHE TEST: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
