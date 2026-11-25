"""Transparent Modbus/TCP relay between the HF5111B DTU and the vendor cloud.

The DTU is configured as a TCP *client* that connects to the vendor's data
server (``sensihydro.copeland.cn:5000``).  We make it connect to us instead
(DNS rewrite), accept that connection, relay it byte-for-byte to the real
vendor server and back -- so the official app keeps working -- while giving
the local HTTP API the possibility to inject its own Modbus TCP requests.

Injected requests use transaction IDs from ``0x8000`` upwards.  Their
responses are intercepted (and *not* forwarded to the cloud, which would not
know these transaction IDs); everything else is relayed unchanged.
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
    ) -> None:
        self.vendor_ip = vendor_ip
        self.vendor_port = vendor_port
        self.listen_host = listen_host
        self.listen_port = listen_port

        self._lock = threading.RLock()
        self._client: socket.socket | None = None
        self._upstream: socket.socket | None = None
        self._client_addr: tuple | None = None
        self._to_client = bytearray()
        self._to_upstream = bytearray()
        self._client_buf = b""
        self._upstream_buf = b""

        self._pending: dict[int, dict] = {}
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
        self.injected = 0
        self.last_cloud_frame_at: float = 0.0
        self.last_dtu_frame_at: float = 0.0
        self.reconnects = 0

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
        status = {
            "dtu_connected": self.dtu_connected,
            "cloud_connected": self.cloud_connected,
            "dtu_peer": self._client_addr[0] if self._client_addr else None,
            "vendor": "%s:%d" % (self.vendor_ip, self.vendor_port),
            "uptime_seconds": round(now - self.started_at, 1),
            "cloud_frames": self.cloud_frames,
            "dtu_frames": self.dtu_frames,
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
        }
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

        The DTU reconnects automatically a few seconds later, which makes it
        re-resolve the vendor host name (useful after a DNS rewrite change).
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
        frame = self._inject(modbus.read_pdu(addr, count), timeout)
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
        frame = self._inject(modbus.write_pdu(addr, value), timeout)
        if frame.is_exception:
            raise ModbusTimeoutError(
                "modbus exception 0x%02x while writing 0x%04x" % (frame.pdu[2], addr)
            )

    def write_registers(self, addr: int, values: list[int], timeout: float = 6.0) -> None:
        """Write consecutive holding registers (function 16)."""
        frame = self._inject(modbus.write_multiple_pdu(addr, values), timeout)
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

    def _inject(self, pdu: bytes, timeout: float) -> modbus.Frame:
        if not self.dtu_connected:
            raise NotConnectedError("DTU is not connected to the bridge")
        with self._inject_lock:
            tid = self._allocate_tid()
            event = threading.Event()
            entry = {"event": event, "frame": None}
            with self._lock:
                self._pending[tid] = entry
                self._to_client.extend(modbus.build_frame(tid, pdu))
                self.injected += 1
            if not event.wait(timeout):
                with self._lock:
                    self._pending.pop(tid, None)
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
                    self._drop(sel)
            self._flush_queues()
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
        with self._lock:
            self._client = client
            self._upstream = upstream
            self._client_addr = addr
            self._client_buf = b""
            self._upstream_buf = b""
        sel.register(client, selectors.EVENT_READ, "client")
        sel.register(upstream, selectors.EVENT_READ, "upstream")
        _LOGGER.info("DTU connected from %s, relay established", addr)

    def _drop(self, sel: selectors.DefaultSelector) -> None:
        with self._lock:
            for sock in (self._client, self._upstream):
                if sock is not None:
                    try:
                        sel.unregister(sock)
                    except (KeyError, ValueError):
                        pass
        self.reconnects += 1
        _LOGGER.info("relay connection closed (DTU will reconnect)")
        self._close_sockets()

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
            self._drop(sel)
            return
        if not data:
            self._drop(sel)
            return
        self._handle_bytes(data, client_side)

    def _handle_bytes(self, data: bytes, client_side: bool) -> None:
        if client_side:
            self.last_dtu_frame_at = time.time()
            items, rest = modbus.parse_frames(self._client_buf + data)
            self._client_buf = rest
            with self._lock:
                for item in items:
                    if isinstance(item, bytes):
                        # DTU heartbeat "00<MAC>" -> relay to the cloud unchanged
                        self.dtu_frames += 1
                        self._to_upstream.extend(item)
                        continue
                    self.dtu_frames += 1
                    entry = self._pending.get(item.transaction_id)
                    if entry is not None:
                        # Answer to one of our injected requests: swallow it.
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
            self.last_cloud_frame_at = time.time()
            items, rest = modbus.parse_frames(self._upstream_buf + data)
            self._upstream_buf = rest
            with self._lock:
                for item in items:
                    self.cloud_frames += 1
                    raw = item if isinstance(item, bytes) else item.raw
                    self._to_client.extend(raw)

    def _flush_queues(self) -> None:
        with self._lock:
            client = self._client
            upstream = self._upstream
            to_client = bytes(self._to_client)
            to_upstream = bytes(self._to_upstream)
            self._to_client.clear()
            self._to_upstream.clear()
        for sock, payload in ((client, to_client), (upstream, to_upstream)):
            if sock is None or not payload:
                continue
            try:
                sent = sock.send(payload)
            except BlockingIOError:
                sent = 0
            except OSError:
                with self._lock:
                    self._close_sockets()
                return
            if sent < len(payload):
                # queue the remainder again for the next iteration
                with self._lock:
                    if sock is client:
                        self._to_client[:0] = payload[sent:]
                    elif sock is upstream:
                        self._to_upstream[:0] = payload[sent:]
