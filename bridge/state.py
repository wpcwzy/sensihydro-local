"""Shared state snapshot, refreshed in the background.

Reading every indoor unit costs about a dozen Modbus round-trips (~2.4 s on
the RS485 bus).  Doing that inside an HTTP request made the web UI feel very
slow, so a worker thread keeps a snapshot up to date and every API request is
answered from memory in microseconds.

The worker only polls while somebody is actually using the API (a request in
the last ``ACTIVE_WINDOW`` seconds) or right after a write; otherwise it stays
quiet so the vendor cloud keeps the bus to itself.
"""

from __future__ import annotations

import logging
import threading
import time

from . import hostmode, units

_LOGGER = logging.getLogger(__name__)


class StateStore:
    """Cached unit/host-mode snapshot with a lazy background refresher."""

    def __init__(self, bridge, interval: float = 3.5, active_window: float = 45.0) -> None:
        self.bridge = bridge
        # A full refresh costs ~2.5 s on the RS485 bus, so keep the duty cycle
        # moderate: poll only while a UI/HA client has been active recently.
        self.interval = interval
        self.active_window = active_window

        self._lock = threading.Lock()
        self._state: dict[str, dict] | None = None
        self._host: dict | None = None
        self._ts = 0.0
        self._dirty = True
        self._last_request = 0.0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ api

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="state-store", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def invalidate(self) -> None:
        """Mark the snapshot as dirty (after a write)."""
        with self._lock:
            self._dirty = True
        self._wake.set()

    def touch(self) -> None:
        """Note that a client is active (keeps the background refresh running)."""
        with self._lock:
            self._last_request = time.monotonic()

    def get(self) -> tuple[dict[str, dict], dict, float]:
        """Return ``(units, host_mode, age_seconds)`` without waiting."""
        self.touch()
        with self._lock:
            state, host, ts, dirty = self._state, self._host, self._ts, self._dirty
        if state is None:
            # Cold start: the very first request has to wait once.
            self.refresh()
            with self._lock:
                state, host, ts = self._state, self._host, self._ts
            if state is None:
                raise RuntimeError("state snapshot unavailable")
            return state, host or {}, time.monotonic() - ts
        if dirty:
            self._wake.set()
        return state, host or {}, time.monotonic() - ts

    # ------------------------------------------------------------- internals

    def refresh(self) -> None:
        """Read all units + host mode and publish a new snapshot."""
        try:
            state = units.read_all(self.bridge)
            try:
                host = hostmode.read_host_mode(self.bridge)
            except Exception as err:  # noqa: BLE001 - host mode is optional
                _LOGGER.debug("host mode read failed: %s", err)
                host = self._host or {}
        except Exception as err:  # noqa: BLE001 - keep serving the old snapshot
            _LOGGER.warning("state refresh failed: %s", err)
            return
        with self._lock:
            self._state = state
            self._host = host
            self._ts = time.monotonic()
            self._dirty = False

    def _should_refresh(self) -> bool:
        with self._lock:
            if self._state is None:
                return True
            if self._dirty:
                return True
            age = time.monotonic() - self._ts
            active = (time.monotonic() - self._last_request) < self.active_window
            return active and age >= self.interval

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._should_refresh():
                self.refresh()
                self._wake.wait(min(0.5, self.interval))
                self._wake.clear()
            else:
                self._wake.wait(1.0)
                self._wake.clear()
