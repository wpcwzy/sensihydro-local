"""Configuration from environment variables."""

from __future__ import annotations

import os
import socket
import struct
import logging

_LOGGER = logging.getLogger(__name__)

PUBLIC_DNS_SERVERS = ("223.5.5.5", "114.114.114.114", "8.8.8.8")


def _dns_query_a(server: str, host: str, timeout: float = 2.5) -> str | None:
    """Ask a specific DNS server for the first A record (bypasses system DNS)."""
    tid = 0x1234
    qname = b"".join(bytes((len(part),)) + part.encode() for part in host.split(".")) + b"\x00"
    packet = struct.pack("!HHHHHH", tid, 0x0100, 1, 0, 0, 0) + qname + struct.pack("!HH", 1, 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(packet, (server, 53))
        data, _ = sock.recvfrom(1024)
    except OSError:
        return None
    finally:
        sock.close()
    if len(data) < 12 or (data[3] & 0x0F) != 0:
        return None
    answered = struct.unpack("!H", data[6:8])[0]
    offset = 12
    # skip question
    while offset < len(data) and data[offset] != 0:
        offset += data[offset] + 1
    offset += 5
    for _ in range(answered):
        if offset + 12 > len(data):
            break
        if data[offset] & 0xC0 == 0xC0:
            offset += 2
        else:
            while offset < len(data) and data[offset] != 0:
                offset += data[offset] + 1
            offset += 1
        rtype, _rclass, _ttl, rdlen = struct.unpack("!HHIH", data[offset : offset + 10])
        offset += 10
        if rtype == 1 and rdlen == 4:
            return socket.inet_ntoa(data[offset : offset + 4])
        offset += rdlen
    return None


def _detect_lan_ip() -> str:
    """Best effort: the LAN address of this host (used for the DNS hijack)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("223.5.5.5", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def resolve_vendor_ip(host: str, explicit: str | None = None) -> str:
    """Resolve the vendor server address without trusting the local DNS.

    The local DNS (AdGuard) is deliberately rewritten to point this host name
    to the bridge itself, so resolving it through the system resolver would
    create a loop.  We therefore prefer an explicit IP and fall back to public
    DNS servers.
    """
    if explicit:
        return explicit
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET)
        if infos:
            ip = infos[0][4][0]
            if not ip.startswith("127."):
                return ip
    except OSError:
        pass
    for server in PUBLIC_DNS_SERVERS:
        ip = _dns_query_a(server, host)
        if ip:
            _LOGGER.info("resolved %s -> %s via %s", host, ip, server)
            return ip
    raise RuntimeError("cannot resolve vendor host %r" % host)


class Config:
    """Runtime configuration."""

    def __init__(self) -> None:
        self.listen_host = os.environ.get("LISTEN_HOST", "0.0.0.0")
        self.listen_port = int(os.environ.get("LISTEN_PORT", "5000"))
        self.http_port = int(os.environ.get("HTTP_PORT", "8090"))
        self.vendor_host = os.environ.get("VENDOR_HOST", "sensihydro.copeland.cn")
        self.vendor_port = int(os.environ.get("VENDOR_PORT", "5000"))
        vendor_ip = os.environ.get("VENDOR_IP") or None
        self.vendor_ip = resolve_vendor_ip(self.vendor_host, vendor_ip)
        self.log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

        # Split DNS for the DTU: AdGuard forwards the DTU's queries to this
        # port; the bridge answers the vendor names with LAN_IP and relays
        # everything else to the public upstream.  Set DNS_PORT=0 to disable.
        self.dns_port = int(os.environ.get("DNS_PORT", "15353"))
        self.dns_upstream = [
            part.strip()
            for part in os.environ.get("DNS_UPSTREAM", "223.5.5.5").split(",")
            if part.strip()
        ]
        self.lan_ip = os.environ.get("LAN_IP") or _detect_lan_ip()

        # Link watchdogs: the vendor cloud sometimes keeps the TCP connection
        # open while it stopped polling, and the DTU can stop answering after a
        # bus lock-up.  Both leave a "connected but dead" link, so the relay
        # forces a reconnect after these idle periods.  0 disables a watchdog.
        self.cloud_idle_timeout = float(os.environ.get("CLOUD_IDLE_TIMEOUT", "300"))
        self.dtu_idle_timeout = float(os.environ.get("DTU_IDLE_TIMEOUT", "180"))

    def __repr__(self) -> str:  # pragma: no cover
        return "<Config dtup=%s:%d http=%d vendor=%s:%d>" % (
            self.listen_host,
            self.listen_port,
            self.http_port,
            self.vendor_ip,
            self.vendor_port,
        )
