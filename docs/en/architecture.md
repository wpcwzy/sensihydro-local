# Architecture

## Overview

```
                         ┌───────────────────── NAS / mini-PC ─────────────────────┐
                         │                                                          │
   DTU HF5111B           │   bridge container (host network)                        │
   ┌──────────────┐      │   ┌────────────────────┐        ┌──────────────────────┐ │
   │ TCP client   │──────┼──▶│ :5000 relay        │───────▶│ vendor cloud :5000   │ │
   │ RTU over 485 │      │   │ transparent + inject│       │ sensihydro.copeland.cn│ │
   └──────┬───────┘      │   └─────────┬──────────┘        └──────────────────────┘ │
          │ RS485        │             │ Modbus injection                            │
   ┌──────▼───────┐      │   ┌─────────▼──────────┐        ┌──────────────────────┐ │
   │ controller    │      │   │ :8090 Web UI + API │◀──────▶│ Home Assistant       │ │
   │ 6 indoor      │      │   └────────────────────┘        │ integration (LAN)     │ │
   └──────────────┘      │                                 └──────────────────────┘ │
                         └──────────────────────────────────────────────────────────┘
```

## Why not a global DNS rewrite

The DTU must resolve the vendor host name to the bridge. The same DNS server also serves
phones and Home Assistant, which use the same name over **HTTPS 443** for the cloud API.
Rewriting the name globally means:

- `NAS:443` has no service → every app/HA cloud request times out;
- the HA integration enters `setup_retry` and all entities become `unavailable`;
- the official app stops working at home.

**Correct approach: split DNS.** Only the DTU gets a different answer:

```
DTU (192.168.1.20) ──query──▶ AdGuard ──(per-client upstream)──▶ bridge:15353 ──▶ 192.168.1.10
other devices      ──query──▶ AdGuard ──(default upstream)────▶ public DNS ──▶ real vendor IP
```

- the bridge embeds a tiny DNS server (`bridge/dnsd.py`, UDP+TCP port 15353) that answers
  only `sensihydro.copeland.cn` / `sensihydro.insinkerator.com.cn` with the bridge IP and
  forwards everything else to `223.5.5.5 / 119.29.29.29`;
- AdGuard Home gets a *client* entry for the DTU whose upstream is `bridge-ip:15353`;
- global `$dnsrewrite` rules must be removed.

`tools/switchover.py` configures this through the AdGuard API (and cleans up legacy rules).

## How the relay works

`bridge/relay.py`:

1. accepts the DTU connection on port 5000 and immediately opens a second connection to the
   real vendor server;
2. forwards bytes in both directions with `selectors` (non-blocking);
3. to read/write registers it builds its own Modbus TCP frames towards the DTU, using
   transaction ids in the `0x8000`–`0x8FFF` range so they cannot collide with the cloud;
4. responses carrying our transaction id are delivered to the waiting request and are
   **not** forwarded to the cloud; everything else passes through untouched;
5. all injected requests are serialised (one lock) so the RS485 bus never sees interleaved
   transactions.

Because the cloud still receives the complete DTU traffic, the official app keeps working.

## Request/response model

The relay exposes synchronous `read_registers()` / `write_register()`:
build frame → enqueue → wait for the matching transaction id (with timeout) → return/raise.
The HTTP API, the background state poller and the HA integration all use these primitives.

## State snapshot (performance)

A full read is 6 units × 2 reads = 12 Modbus round trips ≈ 2.4 s. Doing that per HTTP
request made the web UI feel extremely slow. `bridge/state.py` keeps a background snapshot:

- refreshed every 3.5 s while an API client is active; stops after 45 s of inactivity
  (so the bus is not monopolised);
- `/api/state` answers from memory in milliseconds;
- writes do not read back; they return immediately and the next refresh confirms.

## Cloud vs LAN

| Path | Purpose |
|---|---|
| Cloud (HTTPS API) | data source of the OEM app; fallback control path for HA; fault bits and text states not readable locally |
| LAN (Modbus) | real-time state and the preferred control path; outdoor diagnostics; host mode |

Every HA poll:
1. fetches the cloud unit list;
2. fetches `/api/state` from the bridge and overlays power/mode/target/fan/room temperature
   matched **by unit name**;
3. overlays parameter tables with locally readable values;
4. writes prefer the bridge and fall back to the cloud on failure.

> Matching relies on unit names: the bridge unit names must equal the room names in the
> OEM app.

## Ports

| Port | Listener | Notes |
|---|---|---|
| 5000/TCP | bridge | DTU reconnect port; requires host networking or port mapping |
| 8090/TCP | bridge | web remote + JSON API (no auth, LAN only) |
| 15353/UDP+TCP | bridge | split DNS, used only by the DTU |
| 48899/UDP, 48907/TCP | local scripts | High-Flying IOTService protocol, used only for switching/troubleshooting |

## Deployment forms

- recommended: Docker on a NAS with `network_mode: host`;
- without host networking, map 5000/8090 (and the split-DNS UDP port);
- the bridge runs on any Linux host with Python 3.11+ (stdlib only).

## Security

- the API/UI on 8090 is unauthenticated: keep it on the LAN, never expose it to the internet;
- the bridge never stores vendor credentials — those live in the HA integration;
- the DTU configuration is untouched; rollback = delete the AdGuard client entry and stop
  the container.
