"""Transparent Modbus/TCP relay between the HF5111B DTU and the vendor cloud.

The DTU is a TCP client that normally connects to the vendor's data server.
We make it connect to us instead (split DNS), accept that connection, relay it
byte-for-byte to the real vendor server and back -- so the official app keeps
working -- while giving the local HTTP API the possibility to inject its own
Modbus TCP requests.

Injected requests use transaction IDs from ``0x8000`` and their responses are
intercepted (and *not* forwarded to the cloud, which does not know those
transaction IDs); everything else is relayed unchanged.

Robustness notes
----------------
* The byte stream parser validates MBAP headers before trusting a frame and
  drops garbage bytes instead of forwarding them (a desynchronised stream used
  to be able to poison the vendor session).
* Two watchdogs force a reconnect when the link silently stalls: the vendor
  cloud may keep the TCP connection open while it stopped polling, and the DTU
  may stop sending anything after a bus lock-up.  A stalled link is dropped so
  both ends reconnect and the DTU re-registers with the vendor.
* Send errors, queue overflows and gateway timeouts all lead to a clean drop
  instead of accumulating state.
"""

from __future__ import annotations

import logging
import selectors
import socket
import threading
import time

from . import modbus

_LOGGER = logging.getLogger(__name__)

INJECT_TID_BASE = 0x8000
INJECT_TID_MAX = 0x8FFF

#: force a reconnect when the vendor cloud stops sending anything
DEFAULT_CLOUD_IDLE_TIMEOUT = 300.0
#: force a reconnect when the DTU stops sending anything (heartbeat included)
DEFAULT_DTU_IDLE_TIMEOUT = 180.0
#: drop the connection instead of buffering without bound
MAX_QUEUE_BYTES = 1 << 20


class NotConnectedError(RuntimeError):
    """Raised when the DTU is not connected to the bridge."""


class ModbusTimeoutError(RuntimeError):
    """Raised when the DTU does not answer an injected request."""


class DtuBridge:
    """Relay + Modbus injection endpoint."""

    def __init__(
        self,
        vendor_ip: str,
        vendor_port: int = 5000,
        listen_host: str = "0.0.0.0",
        listen_port: int = 5000,
        cloud_idle_timeout: float = DEFAULT_CLOUD_IDLE_TIMEOUT,
        dtu_idle_timeout: float = DEFAULT_DTU_IDLE_TIMEOUT,
    ) -> None:
        self.vendor_ip = vendor_ip
        self.vendor_port = vendor_port
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.cloud_idle_timeout = float(cloud_idle_timeout)
        self.dtu_idle_timeout = float(dtu_idle_timeout)

        self._lock = threading.RLock()
        self._client: socket.socket | None = None
        self._upstream: socket.socket | None = None
        self._client_addr: tuple | None = None
        self._to_client = bytearray()
        self._to_upstream = bytearray()
        self._client_buf = b""
        self._upstream_buf = b""

        self._pending: dict[int, dict] = {}
        #: transaction ids of injections that already timed out -> swallow the
        #: late answers instead of leaking unsolicited frames to the vendor
        self._expired: dict[int, float] = {}
        self._inject_lock = threading.Lock()
        self._next_tid = INJECT_TID_BASE

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._force_drop = False
        self._listener: socket.socket | None = None

        # statistics
        self.started_at = time.time()
        self.cloud_frames = 0
        self.dtu_frames = 0
        self.dtu_heartbeats = 0
        self.injected = 0
        self.desync_bytes = 0
        self.watchdog_drops = 0
        self.error_drops = 0
        self.queue_drops = 0
        self.late_responses_swallowed = 0
        self.cloud_idle_drops = 0
        self.last_cloud_frame_at: float = 0.0
        self.last_dtu_frame_at: float = 0.0
        self.connected_at: float = 0.0
        self.reconnects = 0
        self.last_drop_reason: str | None = None

    # ------------------------------------------------------------------ state

    @property
    def dtu_connected(self) -> bool:
        with self._lock:
            return self._client is not None

    @property
    def cloud_connected(self) -> bool:
        with self._lock:
            return self._upstream is not None

    def status(self) -> dict:
        now = time.time()
        with self._lock:
            queued_client = len(self._to_client)
            queued_upstream = len(self._to_upstream)
        status = {
            "dtu_connected": self.dtu_connected,
            "cloud_connected": self.cloud_connected,
            "dtu_peer": self._client_addr[0] if self._client_addr else None,
            "vendor": "%s:%d" % (self.vendor_ip, self.vendor_port),
            "uptime_seconds": round(now - self.started_at, 1),
            "cloud_frames": self.cloud_frames,
            "dtu_frames": self.dtu_frames,
            "dtu_heartbeats": self.dtu_heartbeats,
            "injected_requests": self.injected,
            "last_cloud_frame_age": (
                round(now - self.last_cloud_frame_at, 1)
                if self.last_cloud_frame_at
                else None
            ),
            "last_dtu_frame_age": (
                round(now - self.last_dtu_frame_at, 1)
                if self.last_dtu_frame_at
                else None
            ),
            "reconnects": self.reconnects,
            "last_drop_reason": self.last_drop_reason,
            "desync_bytes": self.desync_bytes,
            "watchdog_drops": self.watchdog_drops,
            "error_drops": self.error_drops,
            "queue_drops": self.queue_drops,
            "late_responses_swallowed": self.late_responses_swallowed,
            "cloud_idle_drops": self.cloud_idle_drops,
            "queued_bytes": queued_client + queued_upstream,
            "watchdogs": {
                "cloud_idle_timeout": self.cloud_idle_timeout,
                "dtu_idle_timeout": self.dtu_idle_timeout,
            },
        }
        if self.cloud_idle_drops >= 2:
            status["hint"] = (
                "the vendor cloud keeps the connection open but stopped polling; "
                "if this persists, power-cycle the heat pump (DTU) once so it "
                "registers with the cloud again"
            )
        dns = getattr(self, "dns_server", None)
        if dns is not None:
            status["dns"] = dns.status()
        return status

    # --------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="dtu-relay", daemon=True)
        self._thread.start()

    def drop_connection(self) -> None:
        """Ask the relay loop to drop the current DTU/cloud connection.

        The DTU reconnects automatically a few seconds later, which also makes
        it re-resolve the vendor host name and re-register with the vendor
        cloud (useful when the cloud stopped polling without closing TCP).
        """
        self._force_drop = True

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._listener:
                self._listener.close()
        except OSError:
            pass
        self._close_sockets()

    # ------------------------------------------------------------------ public

    def read_registers(self, addr: int, count: int, timeout: float = 6.0) -> list[int]:
        """Read ``count`` holding registers starting at ``addr``."""
        frame = self._inject(modbus.read_pdu(addr, count), timeout, modbus.FUNC_READ_HOLDING)
        if frame.is_exception:
            raise ModbusTimeoutError(
                "modbus exception 0x%02x while reading 0x%04x" % (frame.pdu[2], addr)
            )
        values = frame.decode_read()
        if len(values) != count:
            raise ModbusTimeoutError(
                "short read at 0x%04x: wanted %d got %d" % (addr, count, len(values))
            )
        return values

    def write_register(self, addr: int, value: int, timeout: float = 6.0) -> None:
        """Write a single holding register (function 6)."""
        frame = self._inject(
            modbus.write_pdu(addr, value), timeout, modbus.FUNC_WRITE_SINGLE
        )
        if frame.is_exception:
            raise ModbusTimeoutError(
                "modbus exception 0x%02x while writing 0x%04x" % (frame.pdu[2], addr)
            )

    def write_registers(self, addr: int, values: list[int], timeout: float = 6.0) -> None:
        """Write consecutive holding registers (function 16)."""
        frame = self._inject(
            modbus.write_multiple_pdu(addr, values), timeout, modbus.FUNC_WRITE_MULTIPLE
        )
        if frame.is_exception:
            raise ModbusTimeoutError(
                "modbus exception 0x%02x while writing 0x%04x" % (frame.pdu[2], addr)
            )

    # ------------------------------------------------------------- injection

    def _allocate_tid(self) -> int:
        with self._lock:
            tid = self._next_tid
            self._next_tid += 1
            if self._next_tid > INJECT_TID_MAX:
                self._next_tid = INJECT_TID_BASE
            return tid

    def _inject(self, pdu: bytes, timeout: float, function: int) -> modbus.Frame:
        if not self.dtu_connected:
            raise NotConnectedError("DTU is not connected to the bridge")
        with self._inject_lock:
            tid = self._allocate_tid()
            event = threading.Event()
            entry = {
                "event": event,
                "frame": None,
                "function": function,
                "sent_at": time.time(),
            }
            with self._lock:
                self._pending[tid] = entry
                self._to_client.extend(modbus.build_frame(tid, pdu))
                self.injected += 1
            if not event.wait(timeout):
                with self._lock:
                    self._pending.pop(tid, None)
                    self._expired[tid] = time.time()
                raise ModbusTimeoutError(
                    "no answer from the heat pump for PDU %s" % pdu.hex()
                )
            with self._lock:
                self._pending.pop(tid, None)
            frame = entry["frame"]
            if frame is None:
                raise ModbusTimeoutError("empty answer for PDU %s" % pdu.hex())
            return frame

    # ---------------------------------------------------------------- relay

    def _close_sockets(self) -> None:
        with self._lock:
            for sock in (self._client, self._upstream):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            self._client = None
            self._upstream = None
            self._client_addr = None
            self._to_client.clear()
            self._to_upstream.clear()
            self._client_buf = b""
            self._upstream_buf = b""
            self.last_cloud_frame_at = 0.0
            self.last_dtu_frame_at = 0.0
            self.connected_at = 0.0
            # unblock any waiting injection immediately
            for entry in self._pending.values():
                entry["event"].set()
            self._pending.clear()

    def _connect_upstream(self) -> socket.socket:
        sock = socket.create_connection((self.vendor_ip, self.vendor_port), timeout=10)
        sock.setblocking(False)
        return sock

    def _run(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.listen_host, self.listen_port))
        listener.listen(5)
        listener.setblocking(False)
        self._listener = listener
        _LOGGER.info(
            "relay listening on %s:%d -> vendor %s:%d",
            self.listen_host,
            self.listen_port,
            self.vendor_ip,
            self.vendor_port,
        )
        sel = selectors.DefaultSelector()
        sel.register(listener, selectors.EVENT_READ, "listen")

        while not self._stop.is_set():
            if self._force_drop:
                self._force_drop = False
                if self._client is not None or self._upstream is not None:
                    self._drop(sel, "requested")
            if not self._flush_queues():
                self._drop(sel, "send error")
            self._check_watchdogs(sel)
            if self._client is not None and self._upstream is not None:
                events = sel.select(timeout=0.05)
            else:
                events = sel.select(timeout=0.2)
            for key, _mask in events:
                if key.data == "listen":
                    self._accept(sel)
                elif key.data == "client":
                    self._read_side(sel, client_side=True)
                elif key.data == "upstream":
                    self._read_side(sel, client_side=False)

        try:
            sel.close()
        except Exception:  # pragma: no cover
            pass
        try:
            listener.close()
        except OSError:
            pass

    def _accept(self, sel: selectors.DefaultSelector) -> None:
        try:
            client, addr = self._listener.accept()
        except (BlockingIOError, OSError):
            return
        if self._client is not None:
            _LOGGER.warning("second DTU connection from %s, closing it", addr)
            client.close()
            return
        try:
            upstream = self._connect_upstream()
        except OSError as err:
            _LOGGER.error("cannot connect to vendor %s:%d: %s", self.vendor_ip, self.vendor_port, err)
            client.close()
            return
        client.setblocking(False)
        now = time.time()
        with self._lock:
            self._client = client
            self._upstream = upstream
            self._client_addr = addr
            self._client_buf = b""
            self._upstream_buf = b""
            self._to_client.clear()
            self._to_upstream.clear()
            # the watchdogs measure silence since this connection started
            self.connected_at = now
            self.last_cloud_frame_at = now
            self.last_dtu_frame_at = now
        sel.register(client, selectors.EVENT_READ, "client")
        sel.register(upstream, selectors.EVENT_READ, "upstream")
        _LOGGER.info("DTU connected from %s, relay established", addr)

    def _drop(self, sel: selectors.DefaultSelector, reason: str) -> None:
        with self._lock:
            had_client = self._client is not None or self._upstream is not None
            for sock in (self._client, self._upstream):
                if sock is not None:
                    try:
                        sel.unregister(sock)
                    except (KeyError, ValueError):
                        pass
        if had_client:
            self.reconnects += 1
            self.last_drop_reason = reason
            if reason.startswith("watchdog"):
                self.watchdog_drops += 1
            elif reason == "send error":
                self.error_drops += 1
            elif reason == "queue overflow":
                self.queue_drops += 1
            _LOGGER.warning(
                "relay connection dropped (%s); the DTU will reconnect", reason
            )
        self._close_sockets()

    def _prune_expired(self) -> None:
        if not self._expired:
            return
        cutoff = time.time() - 120.0
        with self._lock:
            for tid in [t for t, ts in self._expired.items() if ts < cutoff]:
                self._expired.pop(tid, None)

    def _check_watchdogs(self, sel: selectors.DefaultSelector) -> None:
        if self._client is None or self._upstream is None:
            return
        now = time.time()
        self._prune_expired()
        dtu_idle = now - self.last_dtu_frame_at if self.last_dtu_frame_at else 0.0
        cloud_idle = now - self.last_cloud_frame_at if self.last_cloud_frame_at else 0.0
        if self.dtu_idle_timeout and dtu_idle > self.dtu_idle_timeout:
            _LOGGER.warning(
                "no traffic from the DTU for %.0fs, reconnecting", dtu_idle
            )
            self._drop(sel, "watchdog: DTU idle")
            return
        if self.cloud_idle_timeout and cloud_idle > self.cloud_idle_timeout:
            _LOGGER.warning(
                "no traffic from the vendor cloud for %.0fs, reconnecting (the DTU "
                "will re-register with the cloud)",
                cloud_idle,
            )
            self.cloud_idle_drops += 1
            self._drop(sel, "watchdog: cloud idle")

    def _read_side(self, sel: selectors.DefaultSelector, client_side: bool) -> None:
        with self._lock:
            sock = self._client if client_side else self._upstream
        if sock is None:
            return
        try:
            data = sock.recv(65536)
        except BlockingIOError:
            return
        except OSError:
            self._drop(sel, "read error")
            return
        if not data:
            self._drop(sel, "peer closed")
            return
        self._handle_bytes(data, client_side, sel)

    def _handle_bytes(
        self, data: bytes, client_side: bool, sel: selectors.DefaultSelector
    ) -> None:
        with self._lock:
            buffer = (self._client_buf if client_side else self._upstream_buf) + data
        items, rest, dropped = modbus.parse_frames(buffer)
        if dropped:
            self.desync_bytes += dropped
            _LOGGER.debug(
                "dropped %d desynchronised bytes from %s",
                dropped,
                "DTU" if client_side else "cloud",
            )
        if client_side:
            self.last_dtu_frame_at = time.time()
        else:
            self.last_cloud_frame_at = time.time()

        with self._lock:
            if client_side:
                self._client_buf = rest
            else:
                self._upstream_buf = rest

            if client_side:
                # --- DTU -> cloud ---------------------------------------
                for item in items:
                    if isinstance(item, bytes):
                        # heartbeat "00<MAC>" is relayed unchanged
                        self.dtu_frames += 1
                        self.dtu_heartbeats += 1
                        self._to_upstream.extend(item)
                        continue
                    self.dtu_frames += 1
                    if self._expired.pop(item.transaction_id, None) is not None:
                        # late answer of a timed-out injection: never leak it
                        self.late_responses_swallowed += 1
                        _LOGGER.debug(
                            "swallowed late injected response tid=0x%04x",
                            item.transaction_id,
                        )
                        continue
                    entry = self._pending.get(item.transaction_id)
                    if entry is not None and _matches_function(entry, item):
                        entry["frame"] = item
                        entry["event"].set()
                        _LOGGER.debug(
                            "injected response tid=0x%04x pdu=%s",
                            item.transaction_id,
                            item.pdu.hex(),
                        )
                        continue
                    self._to_upstream.extend(item.raw)
            else:
                # --- cloud -> DTU ---------------------------------------
                for item in items:
                    self.cloud_frames += 1
                    self._to_client.extend(item if isinstance(item, bytes) else item.raw)

            queued = len(self._to_client) + len(self._to_upstream)
        if queued > MAX_QUEUE_BYTES:
            self._drop(sel, "queue overflow")

    def _flush_queues(self) -> bool:
        """Send the queued bytes; returns False when the connection is broken."""
        with self._lock:
            client = self._client
            upstream = self._upstream
            to_client = bytes(self._to_client)
            to_upstream = bytes(self._to_upstream)
            self._to_client.clear()
            self._to_upstream.clear()
        for which, sock, payload in (
            ("client", client, to_client),
            ("upstream", upstream, to_upstream),
        ):
            if sock is None or not payload:
                continue
            try:
                sent = sock.send(payload)
            except BlockingIOError:
                sent = 0
            except OSError as err:
                _LOGGER.debug("send to %s failed: %s", which, err)
                return False
            if sent < len(payload):
                remainder = payload[sent:]
                with self._lock:
                    if which == "client":
                        self._to_client[:0] = remainder
                    else:
                        self._to_upstream[:0] = remainder
                if len(remainder) > MAX_QUEUE_BYTES:
                    return False
        return True


def _matches_function(entry: dict, frame: modbus.Frame) -> bool:
    """Only treat a DTU frame as our response when the function matches.

    Transaction IDs are 16 bit and the vendor cloud uses its own sequence; an
    injected TID can coincide with a cloud TID, so also check the function code
    (or its exception variant) before swallowing a frame.
    """
    expected = entry.get("function")
    function = frame.function
    if expected is None or function is None:
        return True
    return function == expected or function == (expected | 0x80)
