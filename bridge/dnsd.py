"""Tiny split-DNS server for the DTU only.

The HF5111B resolves the vendor host name itself, so it has to be pointed at
the bridge without hijacking the name for every other device in the LAN (that
would break HTTPS access to the vendor cloud from Home Assistant, phones…).

AdGuard Home answers DNS for the DTU (its DHCP DNS is AdGuard); a per-client
upstream entry for the DTU forwards its queries here (port 5353).  We answer
the vendor host names with the bridge IP and relay every other query to a
public resolver.

    client 192.168.1.20  ->  AdGuard  ->  bridge:5353  ->  192.168.1.10
    every other client    ->  AdGuard  ->  public DNS   ->  real vendor IP
"""

from __future__ import annotations

import logging
import socket
import struct
import threading

_LOGGER = logging.getLogger(__name__)

#: host names the DTU connects to (the vendor data server and its alias)
HIJACK_NAMES: frozenset[str] = frozenset(
    {"sensihydro.copeland.cn", "sensihydro.insinkerator.com.cn"}
)

_DEFAULT_UPSTREAM = "223.5.5.5"
_TCP_READ_TIMEOUT = 5.0


class DnsServer:
    """UDP + TCP DNS responder: hijack a few names, forward the rest."""

    def __init__(
        self,
        listen_host: str,
        port: int,
        hijack_ip: str,
        upstreams: list[str] | None = None,
    ) -> None:
        self.listen_host = listen_host
        self.port = port
        self.hijack_ip = hijack_ip
        self.upstreams = upstreams or [_DEFAULT_UPSTREAM]
        self._udp: socket.socket | None = None
        self._tcp: socket.socket | None = None
        self._stop = threading.Event()
        self.queries = 0
        self.hijacked = 0
        self.forwarded = 0

    # ------------------------------------------------------------------ public

    def start(self) -> bool:
        """Start the UDP and TCP listeners.  Returns False when the port is busy."""
        try:
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            udp.bind((self.listen_host, self.port))
            udp.settimeout(0.5)
            self._udp = udp
        except OSError as err:
            _LOGGER.error("DNS server cannot bind UDP %s:%d: %s", self.listen_host, self.port, err)
            return False
        try:
            tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            tcp.bind((self.listen_host, self.port))
            tcp.listen(16)
            tcp.settimeout(0.5)
            self._tcp = tcp
        except OSError as err:
            _LOGGER.warning("DNS server cannot bind TCP %s:%d: %s", self.listen_host, self.port, err)
        threading.Thread(target=self._run_udp, name="dns-udp", daemon=True).start()
        if self._tcp is not None:
            threading.Thread(target=self._run_tcp, name="dns-tcp", daemon=True).start()
        _LOGGER.info(
            "split DNS listening on %s:%d, hijacking %s -> %s, upstream %s",
            self.listen_host, self.port, ", ".join(sorted(HIJACK_NAMES)),
            self.hijack_ip, ", ".join(self.upstreams),
        )
        return True

    def stop(self) -> None:
        self._stop.set()
        for sock in (self._udp, self._tcp):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def status(self) -> dict:
        return {
            "listening": self.port if self._udp else None,
            "hijack": sorted(HIJACK_NAMES),
            "hijack_ip": self.hijack_ip,
            "upstream": self.upstreams,
            "queries": self.queries,
            "hijacked": self.hijacked,
            "forwarded": self.forwarded,
        }

    # ------------------------------------------------------------------ loops

    def _run_udp(self) -> None:
        assert self._udp is not None
        while not self._stop.is_set():
            try:
                data, peer = self._udp.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                answer = self.handle(data)
            except Exception as err:  # noqa: BLE001 - never kill the loop
                _LOGGER.debug("DNS query failed: %s", err)
                continue
            if answer:
                try:
                    self._udp.sendto(answer, peer)
                except OSError:
                    pass

    def _run_tcp(self) -> None:
        assert self._tcp is not None
        while not self._stop.is_set():
            try:
                conn, _peer = self._tcp.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_tcp, args=(conn,), daemon=True).start()

    def _handle_tcp(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(_TCP_READ_TIMEOUT)
            header = conn.recv(2)
            if len(header) != 2:
                return
            length = struct.unpack("!H", header)[0]
            data = b""
            while len(data) < length:
                chunk = conn.recv(length - len(data))
                if not chunk:
                    return
                data += chunk
            answer = self.handle(data, tcp=True)
            if answer:
                conn.sendall(struct.pack("!H", len(answer)) + answer)
        except OSError:
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    # ---------------------------------------------------------------- handling

    def handle(self, data: bytes, tcp: bool = False) -> bytes | None:
        """Answer a DNS message (or relay it upstream)."""
        if len(data) < 12:
            return None
        self.queries += 1
        qdcount = struct.unpack("!H", data[4:6])[0]
        if qdcount != 1:
            return self._forward(data, tcp)
        try:
            name, offset = _read_name(data, 12)
            qtype, qclass = struct.unpack("!HH", data[offset:offset + 4])
        except (IndexError, ValueError):
            return self._forward(data, tcp)
        if name in HIJACK_NAMES and qclass == 1 and qtype in (1, 28):
            self.hijacked += 1
            return self._answer(data, offset + 4, qtype)
        return self._forward(data, tcp)

    def _answer(self, query: bytes, question_end: int, qtype: int) -> bytes:
        """Build an authoritative-ish answer for a hijacked name."""
        rd = (query[2] & 0x01)  # copy the "recursion desired" bit
        flags = 0x8000 | (rd << 8) | 0x0080  # QR=1, RD=copy, RA=1
        question = query[12:question_end]
        if qtype == 1:
            ip = socket.inet_aton(self.hijack_ip)
            answer = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 30, 4) + ip
            header = struct.pack("!HHHHHH", struct.unpack("!H", query[0:2])[0], flags, 1, 1, 0, 0)
            return header + question + answer
        # AAAA: no IPv6 address -> empty NOERROR answer
        header = struct.pack("!HHHHHH", struct.unpack("!H", query[0:2])[0], flags, 1, 0, 0, 0)
        return header + question

    def _forward(self, query: bytes, tcp: bool = False) -> bytes | None:
        self.forwarded += 1
        for upstream in self.upstreams:
            try:
                if tcp:
                    return _forward_tcp(upstream, query)
                return _forward_udp(upstream, query)
            except OSError:
                continue
        return None


def _read_name(data: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    jumps = 0
    while True:
        if offset >= len(data):
            raise ValueError("truncated name")
        length = data[offset]
        if length == 0:
            offset += 1
            break
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise ValueError("truncated pointer")
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if jumps > 8:
                raise ValueError("pointer loop")
            jumps += 1
            name_part, _ = _read_name(data, pointer)
            labels.append(name_part)
            offset += 2
            break
        labels.append(data[offset + 1:offset + 1 + length].decode("ascii", "ignore"))
        offset += 1 + length
    return ".".join(labels).rstrip(".").lower(), offset


def _forward_udp(upstream: str, query: bytes) -> bytes:
    host, _, port_text = upstream.partition(":")
    port = int(port_text) if port_text else 53
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2.5)
    try:
        sock.sendto(query, (host, port))
        data, _ = sock.recvfrom(4096)
        return data
    finally:
        sock.close()


def _forward_tcp(upstream: str, query: bytes) -> bytes:
    host, _, port_text = upstream.partition(":")
    port = int(port_text) if port_text else 53
    sock = socket.create_connection((host, port), timeout=2.5)
    try:
        sock.sendall(struct.pack("!H", len(query)) + query)
        header = sock.recv(2)
        if len(header) != 2:
            raise OSError("short TCP answer")
        length = struct.unpack("!H", header)[0]
        data = b""
        while len(data) < length:
            chunk = sock.recv(length - len(data))
            if not chunk:
                raise OSError("short TCP answer")
            data += chunk
        return data
    finally:
        sock.close()
