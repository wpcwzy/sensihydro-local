"""Entry point of the SensiHydro LAN bridge container.

Responsibilities
----------------
1. Listen on TCP 5000 and pretend to be the vendor's data server for the
   HF5111B DTU (the DTU is redirected here with a DNS rewrite).
2. Relay that connection to the real vendor server, so the official app keeps
   working exactly as before.
3. Offer a JSON HTTP API (default port 8090) that reads/writes the indoor
   units directly over the Modbus/TCP tunnel of the DTU -- this is the local
   control path used by the Home Assistant integration.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time

from .config import Config
from .dnsd import DnsServer
from .http_api import create_server
from .relay import DtuBridge


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def main() -> int:
    config = Config()
    _setup_logging(config.log_level)
    log = logging.getLogger("sensihydro.bridge")
    log.info("starting SensiHydro bridge: %r", config)

    bridge = DtuBridge(
        vendor_ip=config.vendor_ip,
        vendor_port=config.vendor_port,
        listen_host=config.listen_host,
        listen_port=config.listen_port,
        cloud_idle_timeout=config.cloud_idle_timeout,
        dtu_idle_timeout=config.dtu_idle_timeout,
    )
    bridge.start()

    dns: DnsServer | None = None
    if config.dns_port:
        dns = DnsServer(
            listen_host="0.0.0.0",
            port=config.dns_port,
            hijack_ip=config.lan_ip,
            upstreams=config.dns_upstream,
        )
        if dns.start():
            bridge.dns_server = dns  # exposed through /health
        else:
            dns = None

    server = create_server(bridge, "0.0.0.0", config.http_port)
    http_thread = threading.Thread(
        target=server.serve_forever, name="http-api", daemon=True
    )
    http_thread.start()
    log.info("HTTP API listening on 0.0.0.0:%d", config.http_port)

    stop = threading.Event()

    def _shutdown(signum, _frame):  # noqa: ANN001
        log.info("signal %s received, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        while not stop.wait(1.0):
            pass
    finally:
        server.shutdown()
        store = getattr(server, "state_store", None)
        if store is not None:
            store.stop()
        if dns is not None:
            dns.stop()
        bridge.stop()
        log.info("bye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
