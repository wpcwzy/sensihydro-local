#!/usr/bin/env python3
"""Force the HF5111B DTU to reconnect (e.g. after changing a DNS rewrite).

The DTU is a transparent TCP client that keeps its vendor-cloud connection open
for hours.  When the DNS rewrite points to a new server (the bridge container),
the running connection has to be dropped so that the DTU resolves the name
again.  This script impersonates the High-Flying IOTService tool: it sends the
UDP "server announcement" on port 48899, waits for the DTU to connect back and
finally sends the official JCMD ``reload`` command (CID 20001).

Usage
-----
    python3 dtu_reload.py                       # auto-detect everything
    python3 dtu_reload.py --dtu 192.168.1.20 --our-ip 192.168.1.50
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time

ANNOUNCE = {
    "Agent": 0,
    "Type": "EP_Tool",
    "Version": "2.1.01b",
    "ServEn": 1,
    "ServAddr": "",
    "PasswordEn": 0,
    "ClientEn": 0,
    "Zone": "EPort",
    "ConnectMode": "Force",
    "ServPort": 0,
}


def local_ip_towards(target: str) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target, 48899))
        return sock.getsockname()[0]
    finally:
        sock.close()


def reload_dtu(dtu_ip: str, our_ip: str, listen_port: int, timeout: float = 40.0) -> bool:
    announce = dict(ANNOUNCE)
    announce["ServAddr"] = our_ip
    announce["ServPort"] = listen_port
    payload = json.dumps(announce).encode()

    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", listen_port))
    server.listen(20)
    server.settimeout(2.0)

    print(f"announcing ourselves ({our_ip}:{listen_port}) to {dtu_ip} ...")
    for _ in range(6):
        udp.sendto(payload, (dtu_ip, 48899))
        udp.sendto(payload, ("255.255.255.255", 48899))
        time.sleep(0.5)

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn, addr = server.accept()
        except socket.timeout:
            continue
        conn.settimeout(1.0)
        try:
            probe = conn.recv(8)
        except socket.timeout:
            probe = b""
        if probe == b"":
            conn.close()
            continue  # the DTU opens a few empty probe connections first
        print(f"DTU connected from {addr}, sending reload command")
        conn.sendall(json.dumps({"CID": 20001, "JCMD": 1}).encode())
        try:
            answer = conn.recv(256)
        except socket.timeout:
            answer = b""
        conn.close()
        print("DTU answered:", answer or b"(no reply)")
        return b'"RC":0' in answer or b"RC\":0" in answer
    print("timed out waiting for the DTU to connect back", file=sys.stderr)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dtu", default="192.168.1.20", help="DTU IP address")
    parser.add_argument("--our-ip", default=None, help="IP of this computer on the LAN")
    parser.add_argument("--port", type=int, default=48907, help="TCP port to listen on")
    args = parser.parse_args()
    our_ip = args.our_ip or local_ip_towards(args.dtu)
    ok = reload_dtu(args.dtu, our_ip, args.port)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
