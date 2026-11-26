#!/usr/bin/env python3
"""Minimal split-DNS responder for the HF5111B DTU -- runs OUTSIDE Docker.

Why this exists
---------------
The DTU resolves ``sensihydro.copeland.cn`` and must be redirected to the
bridge.  When that redirect is served by a container, every Docker daemon
restart / engine upgrade / container rebuild makes the answer disappear, and
AdGuard then falls back to public DNS: the DTU learns the *real* vendor IP,
connects straight to the vendor and stays there (the bridge is bypassed until
the DTU is power-cycled).

Running this tiny responder on the NAS host itself (systemd service, no Docker)
keeps the answer available at all times: while the bridge is down the DTU simply
cannot connect and keeps retrying; as soon as the bridge is back the DTU
connects to it again -- no manual recovery needed.

It answers only the configured vendor domains with the configured IP and
forwards everything else to the configured upstream resolvers.

Usage
-----
    python3 host_dns.py --answer 192.168.110.28 \
        --domains sensihydro.copeland.cn,sensihydro.insinkerator.com.cn \
        --upstream 223.5.5.5 --upstream 119.29.29.29

No third-party dependencies (Python 3.8+).
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import threading
import time

#: A short TTL keeps the DTU re-resolving after a bridge restart.  TTL handling
#: in the HF5111B is unknown, so keep it small.
ANSWER_TTL = 30
UPSTREAM_TIMEOUT = 4.0


def _parse_question(query: bytes) -> tuple[bytes, int, int] | None:
    """Return (question_section, qtype, qclass) for a single-question query."""
    if len(query) < 12:
        return None
    qdcount = struct.unpack("!H", query[4:6])[0]
    if qdcount != 1:
        return None
    index = 12
    while index < len(query):
        length = query[index]
        if length == 0:
            index += 1
            break
        if length & 0xC0:
            return None  # compression pointers are not valid in a question
        index += 1 + length
    if index + 4 > len(query):
        return None
    qtype, qclass = struct.unpack("!HH", query[index:index + 4])
    return query[12:index + 4], qtype, qclass


def _qname(query: bytes) -> str | None:
    if len(query) < 12:
        return None
    labels = []
    index = 12
    while index < len(query):
        length = query[index]
        if length == 0:
            return ".".join(labels).lower()
        if length & 0xC0:
            return None
        labels.append(query[index + 1:index + 1 + length].decode("ascii", "replace"))
        index += 1 + length
    return None


def _hijack_answer(query: bytes, ip: str) -> bytes:
    parsed = _parse_question(query)
    if parsed is None:
        return b""
    question, qtype, _qclass = parsed
    tid = query[:2]
    if qtype == 1:  # A
        answer = (
            b"\xc0\x0c"
            + struct.pack("!HHIH", 1, 1, ANSWER_TTL, 4)
            + socket.inet_aton(ip)
        )
        ancount = 1
        # AAAA lookups must not fall through to public DNS (that would expose
        # the real vendor address over IPv6), answer them with no data.
    else:
        answer = b""
        ancount = 0
    header = tid + struct.pack("!HHHHH", 0x8180, 1, ancount, 0, 0)
    return header + question + answer


def _forward(query: bytes, upstreams: list[tuple[str, int]]) -> bytes | None:
    for host, port in upstreams:
        for _attempt in range(2):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(UPSTREAM_TIMEOUT)
            try:
                sock.sendto(query, (host, port))
                data, _addr = sock.recvfrom(4096)
                if len(data) >= 12:
                    return data
            except OSError:
                continue
            finally:
                sock.close()
    return None


def _servfail(query: bytes) -> bytes:
    header = query[:2] + struct.pack("!HHHHH", 0x8182, 1, 0, 0, 0)
    parsed = _parse_question(query)
    question = parsed[0] if parsed else b""
    return header + question


class DnsResponder:
    def __init__(self, answer: str, domains: set[str], upstreams: list[tuple[str, int]]) -> None:
        self.answer = answer
        self.domains = domains
        self.upstreams = upstreams
        self.hijacked = 0
        self.forwarded = 0
        self.failed = 0
        self._stop = threading.Event()

    # -- helpers ---------------------------------------------------------
    def _resolve(self, query: bytes) -> bytes:
        name = _qname(query)
        if name is not None and name in self.domains:
            self.hijacked += 1
            return _hijack_answer(query, self.answer)
        response = _forward(query, self.upstreams)
        if response is None:
            self.failed += 1
            return _servfail(query)
        self.forwarded += 1
        return response

    # -- servers ---------------------------------------------------------
    def serve_udp(self, host: str, port: int) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        print("host-dns: udp %s:%d answer=%s domains=%s" % (host, port, self.answer, sorted(self.domains)), flush=True)
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except OSError:
                continue
            if not data:
                continue
            threading.Thread(
                target=self._handle_udp, args=(sock, data, addr), daemon=True
            ).start()

    def _handle_udp(self, sock: socket.socket, data: bytes, addr) -> None:
        try:
            sock.sendto(self._resolve(data), addr)
        except OSError:
            pass

    def serve_tcp(self, host: str, port: int) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen(16)
        print("host-dns: tcp %s:%d" % (host, port), flush=True)
        while not self._stop.is_set():
            try:
                conn, _addr = server.accept()
            except OSError:
                continue
            threading.Thread(target=self._handle_tcp, args=(conn,), daemon=True).start()

    def _handle_tcp(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(10)
            try:
                header = conn.recv(2)
                if len(header) != 2:
                    return
                length = struct.unpack("!H", header)[0]
                query = b""
                while len(query) < length:
                    chunk = conn.recv(length - len(query))
                    if not chunk:
                        return
                    query += chunk
                response = self._resolve(query)
                conn.sendall(struct.pack("!H", len(response)) + response)
            except OSError:
                return

    def stop(self) -> None:
        self._stop.set()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Split DNS for the SensiHydro DTU")
    parser.add_argument("--answer", required=True, help="IP the vendor domains resolve to (the bridge host)")
    parser.add_argument("--domains", default="sensihydro.copeland.cn,sensihydro.insinkerator.com.cn")
    parser.add_argument("--upstream", action="append", default=None,
                        help="upstream resolver, repeatable (default 223.5.5.5, 119.29.29.29)")
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=53)
    parser.add_argument("--no-tcp", action="store_true")
    args = parser.parse_args(argv)

    domains = {d.strip().lower().rstrip(".") for d in args.domains.split(",") if d.strip()}
    upstreams = [u.strip() for u in (args.upstream or ["223.5.5.5", "119.29.29.29"])]
    parsed_upstreams = []
    for item in upstreams:
        if ":" in item:
            host, _, port = item.partition(":")
            parsed_upstreams.append((host, int(port)))
        else:
            parsed_upstreams.append((item, 53))

    responder = DnsResponder(args.answer, domains, parsed_upstreams)
    threads = [threading.Thread(target=responder.serve_udp, args=(args.listen, args.port), daemon=True)]
    if not args.no_tcp:
        threads.append(threading.Thread(target=responder.serve_tcp, args=(args.listen, args.port), daemon=True))
    for thread in threads:
        thread.start()

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        responder.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
