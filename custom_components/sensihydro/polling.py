"""Polling policy: fast over the LAN, relaxed through the cloud.

The integration used to poll at one fixed interval, so it was either too slow
for local control or too chatty for the vendor cloud.  Now the interval depends
on where the data comes from:

* **LAN mode** (the bridge is reachable): entities refresh every
  :data:`LOCAL_SCAN_INTERVAL` seconds.  The values come from the controller
  itself, so this is cheap and gives a responsive UI.
* **Cloud mode** (no bridge): every :data:`DEFAULT_CLOUD_SCAN_INTERVAL`
  seconds, which keeps the vendor API load low -- its data is a cached
  snapshot anyway.
* In LAN mode the cloud is still polled for unit ids and the fault flags, but
  only every :data:`LOCAL_MODE_CLOUD_REFRESH` seconds at most.

This module is free of Home Assistant imports so it can be tested standalone.
"""

from __future__ import annotations

#: Refresh rate while the LAN bridge is reachable (seconds).
LOCAL_SCAN_INTERVAL = 15
#: Ask the bridge to refresh its snapshot when it is older than this (seconds).
LOCAL_STATE_MAX_AGE = 8
#: In LAN mode the cloud is refreshed at most this often (seconds).
LOCAL_MODE_CLOUD_REFRESH = 60
#: Bounds for the user-configurable cloud interval (mirrors const.py).
MIN_SCAN_INTERVAL = 15
MAX_SCAN_INTERVAL = 600
#: Default cloud-mode interval (also the default for the option).
DEFAULT_CLOUD_SCAN_INTERVAL = 45


def scan_interval(local_available: bool, cloud_interval: int) -> int:
    """Return the polling interval to use for the current data source."""
    if local_available:
        return LOCAL_SCAN_INTERVAL
    try:
        value = int(cloud_interval)
    except (TypeError, ValueError):
        value = DEFAULT_CLOUD_SCAN_INTERVAL
    return max(MIN_SCAN_INTERVAL, min(MAX_SCAN_INTERVAL, value))


def cloud_refresh_interval(local_available: bool, cloud_interval: int) -> int:
    """Return how often the vendor cloud may be queried."""
    interval = scan_interval(False, cloud_interval)
    if not local_available:
        return interval
    return max(LOCAL_MODE_CLOUD_REFRESH, interval)
