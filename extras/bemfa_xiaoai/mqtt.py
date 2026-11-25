"""Minimal MQTT 3.1.1 client (QoS 0) for the Bemfa cloud.

Only what the bridge needs: connect with the private key as client id,
subscribe to the device topics, publish state updates and answer pings.
Implemented in plain sockets so no extra Python package is required inside
the Home Assistant container.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time

_LOGGER = logging.getLogger(__name__)


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        digit = value % 128
        value //= 128
        if value:
            digit |= 0x80
        out.append(digit)
        if not value:
            return bytes(out)


def _encode_string(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack("!H", len(raw)) + raw


class MqttClient:
    """Tiny MQTT client: CONNECT / SUBSCRIBE / PUBLISH(QoS0) / PING."""

    def __init__(self, host: str, port: int, client_id: str, keepalive: int = 60) -> None:
        self.host = host
        self.port = port
        self.client_id = client_id
        self.keepalive = keepalive

        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._packet_id = 0
        self._connected = threading.Event()
        self._last_rx = 0.0
        self._buffer = bytearray()

        #: called (in the mqtt thread) whenever a message arrives
        self.on_message = None
        #: called (in the mqtt thread) after a successful (re)connect
        self.on_connected = None

    # ----------------------------------------------------------------- public

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="bemfa-mqtt", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close()

    def publish(self, topic: str, payload: str) -> None:
        sock = self._sock
        if sock is None:
            return
        body = _encode_string(topic) + payload.encode("utf-8")
        packet = b"\x30" + _encode_varint(len(body)) + body
        try:
            with self._send_lock:
                sock.sendall(packet)
        except OSError as err:
            _LOGGER.debug("bemfa publish failed: %s", err)
            self._close()

    def subscribe(self, topics: list[str]) -> None:
        sock = self._sock
        if sock is None or not topics:
            return
        self._packet_id = (self._packet_id % 65535) + 1
        body = struct.pack("!H", self._packet_id)
        for topic in topics:
            body += _encode_string(topic) + b"\x00"  # qos 0
        packet = b"\x82" + _encode_varint(len(body)) + body
        try:
            with self._send_lock:
                sock.sendall(packet)
        except OSError as err:
            _LOGGER.debug("bemfa subscribe failed: %s", err)

    # -------------------------------------------------------------- internals

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._connect_and_loop()
            except Exception as err:  # noqa: BLE001 - keep reconnecting
                _LOGGER.warning("bemfa mqtt connection problem: %s", err)
            self._close()
            if self._stop.is_set():
                break
            time.sleep(5)

    def _close(self) -> None:
        sock, self._sock = self._sock, None
        self._connected.clear()
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _connect_and_loop(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=10)
        sock.settimeout(1.0)
        self._sock = sock

        payload = (
            _encode_string("MQTT")
            + bytes([4])  # protocol level 3.1.1
            + bytes([0x02])  # clean session
            + struct.pack("!H", self.keepalive)
            + _encode_string(self.client_id)
        )
        sock.sendall(b"\x10" + _encode_varint(len(payload)) + payload)
        packet = None
        deadline = time.monotonic() + 10
        while packet is None:
            packet = self._next_packet()
            if packet is not None:
                break
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                if time.monotonic() > deadline:
                    raise OSError("bemfa CONNACK timeout") from None
                continue
            if not chunk:
                raise OSError("connection closed while waiting for CONNACK")
            self._buffer += chunk
        packet_type, body = packet
        if packet_type != 2 or len(body) < 2 or body[1] != 0:
            raise OSError("bemfa CONNACK rejected: %r" % (body,))
        _LOGGER.info("connected to Bemfa MQTT as %s", self.client_id)

        self._buffer.clear()
        self._last_rx = time.monotonic()
        self._connected.set()
        if self.on_connected is not None:
            try:
                self.on_connected()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("on_connected callback failed")

        next_ping = time.monotonic() + 30
        while not self._stop.is_set():
            if time.monotonic() >= next_ping:
                sock.sendall(b"\xc0\x00")  # PINGREQ
                next_ping = time.monotonic() + 30
                if time.monotonic() - self._last_rx > 120:
                    raise OSError("bemfa MQTT heartbeat timeout")
            packet = self._next_packet()
            if packet is None:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    raise OSError("connection closed by bemfa")
                self._buffer += chunk
                self._last_rx = time.monotonic()
                continue
            packet_type, body = packet
            self._last_rx = time.monotonic()
            if packet_type == 3:  # PUBLISH
                self._handle_publish(body)

    def _handle_publish(self, body: bytes) -> None:
        if len(body) < 2:
            return
        topic_len = struct.unpack("!H", body[:2])[0]
        topic = body[2:2 + topic_len].decode("utf-8", "replace")
        offset = 2 + topic_len
        qos = 0
        payload = body[offset:]
        if self.on_message is not None:
            try:
                self.on_message(topic, payload.decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                _LOGGER.exception("bemfa message handler failed")

    def _next_packet(self) -> tuple[int, bytes] | None:
        """Return the next complete packet from the buffer, or None."""
        buf = self._buffer
        if not buf:
            return None
        index = 1
        multiplier = 1
        length = 0
        while True:
            if index >= len(buf):
                return None
            digit = buf[index]
            index += 1
            length += (digit & 0x7F) * multiplier
            if not digit & 0x80:
                break
            multiplier *= 128
            if multiplier > 128 ** 3:
                raise OSError("malformed remaining length")
        total = index + length
        if len(buf) < total:
            return None
        header = buf[0]
        body = bytes(buf[index:total])
        del buf[:total]
        return header >> 4, body
