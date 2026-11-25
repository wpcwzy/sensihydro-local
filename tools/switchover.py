#!/usr/bin/env python3
"""Point the DTU at the local bridge without breaking anyone else's DNS.

The recommended setup is *split DNS*: the AdGuard Home instance that serves
DHCP keeps resolving the vendor host names normally for phones and Home
Assistant, and only the DTU's client entry gets a dedicated upstream --
the bridge's tiny DNS server (``LAN_IP:15353``) which answers the vendor host
names with the bridge IP.

This script does that through the AdGuard Home API:

  1. create (or update) the client entry for the DTU with a per-client
     upstream pointing at the bridge DNS;
  2. remove any legacy global ``$dnsrewrite`` rules for the vendor domains
     (those hijack HTTPS for every device and break the cloud API);
  3. optionally poke the DTU so it resolves the name again.

Usage::

    python3 switchover.py --bridge-ip 192.168.1.10 --dtu 192.168.1.20 \
        --adguard http://192.168.1.2:8080 --adguard-user admin \
        --adguard-password '***'

    python3 switchover.py ... --remove     # undo: delete the client entry

Environment variables are used for the password when the argument is omitted
(``ADGUARD_PASSWORD``), so no secret has to live in shell history.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

VENDOR_DOMAINS = ("sensihydro.copeland.cn", "sensihydro.insinkerator.com.cn")
CLIENT_NAME = "AC-DTU"


class AdGuard:
    """Very small AdGuard Home API client."""

    def __init__(self, base: str, username: str, password: str) -> None:
        self.base = base.rstrip("/")
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.headers = {
            "Authorization": "Basic " + token,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            self.base + path, data=data, headers=self.headers, method=method
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, (json.loads(body) if body.strip() else None)

    def clients(self) -> list[dict]:
        _status, data = self._request("GET", "/control/clients")
        return (data or {}).get("clients") or []

    def add_client(self, payload: dict) -> None:
        self._request("POST", "/control/clients/add", payload)

    def update_client(self, name: str, payload: dict) -> None:
        self._request("POST", "/control/clients/update", {"name": name, "data": payload})

    def delete_client(self, name: str) -> None:
        self._request("POST", "/control/clients/delete", {"name": name})

    def user_rules(self) -> list[str]:
        _status, data = self._request("GET", "/control/filtering/status")
        return (data or {}).get("user_rules") or []

    def set_user_rules(self, rules: list[str]) -> None:
        self._request("POST", "/control/filtering/set_rules", {"rules": rules})


def strip_global_rewrites(rules: list[str]) -> tuple[list[str], int]:
    """Drop legacy ``$dnsrewrite`` rules for the vendor host names."""
    kept = [
        rule
        for rule in rules
        if not any(
            rule.startswith("||%s^$dnsrewrite=" % domain) for domain in VENDOR_DOMAINS
        )
    ]
    return kept, len(rules) - len(kept)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="point the DTU at the bridge via AdGuard per-client upstream"
    )
    parser.add_argument("--bridge-ip", default="192.168.1.10", help="IP of the machine running the bridge")
    parser.add_argument("--dns-port", type=int, default=15353, help="bridge split-DNS port")
    parser.add_argument("--dtu", default="192.168.1.20", help="IP address of the DTU")
    parser.add_argument("--adguard", default="http://192.168.1.2:8080", help="AdGuard Home base URL")
    parser.add_argument("--adguard-user", default="admin", help="AdGuard Home user name")
    parser.add_argument("--adguard-password", default=os.environ.get("ADGUARD_PASSWORD"))
    parser.add_argument("--remove", action="store_true", help="undo the change (delete the client entry)")
    parser.add_argument("--no-reload", action="store_true", help="do not force the DTU to reconnect")
    args = parser.parse_args()

    if not args.adguard_password:
        print("AdGuard password is required (--adguard-password or $ADGUARD_PASSWORD)", file=sys.stderr)
        return 2

    adguard = AdGuard(args.adguard, args.adguard_user, args.adguard_password)

    # 1) clean up legacy global rewrites (they break HTTPS for HA/phones)
    rules, removed = strip_global_rewrites(adguard.user_rules())
    if removed:
        adguard.set_user_rules(rules)
        print(f"removed {removed} legacy global dnsrewrite rule(s)")

    try:
        clients = adguard.clients()
    except urllib.error.URLError as err:
        print(f"cannot talk to AdGuard Home at {args.adguard}: {err}", file=sys.stderr)
        return 1

    existing = next(
        (client for client in clients if args.dtu in (client.get("ids") or [])), None
    )

    if args.remove:
        if existing:
            adguard.delete_client(existing["name"])
            print(f"deleted AdGuard client {existing['name']} ({args.dtu})")
        else:
            print("no client entry for", args.dtu)
    else:
        upstream = f"{args.bridge_ip}:{args.dns_port}"
        payload = dict(existing or {})
        payload.update(
            {
                "name": existing["name"] if existing else CLIENT_NAME,
                "ids": [args.dtu],
                "upstreams": [upstream],
                "use_global_settings": False,
                "filtering_enabled": True,
            }
        )
        if existing:
            adguard.update_client(existing["name"], payload)
            print(f"updated AdGuard client {payload['name']}: upstream {upstream}")
        else:
            adguard.add_client(payload)
            print(f"created AdGuard client {payload['name']}: {args.dtu} -> {upstream}")
        print(
            "verify: on a normal computer 'sensihydro.copeland.cn' must resolve to "
            "the real vendor address, not to the bridge"
        )

    if args.remove or args.no_reload:
        return 0

    # 2) make the DTU resolve the name again
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dtu_reload import local_ip_towards, reload_dtu  # noqa: E402

    time.sleep(2)
    our_ip = local_ip_towards(args.dtu)
    ok = reload_dtu(args.dtu, our_ip, 48907)
    if not ok:
        print(
            "warning: the DTU did not confirm the reload; power-cycling the air "
            "conditioner also makes it reconnect",
            file=sys.stderr,
        )
    print(f"now check: http://{args.bridge_ip}:8090/health")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
