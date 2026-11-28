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

## Diagnostics and history

The cloud parameter table (`customservice/selectConfigById`) contains two kinds of fields:

1. **Protection/fault flags** (`val125_*`…`val131_*`, `val211_*`, `val212_*`, ~131 in total)
   are aggregated into **one** `fault summary` sensor: it reads `正常` (normal) when
   everything is fine, otherwise it lists every active fault as "room fault-name",
   comma separated; the raw values of all flags are exposed in the `flags` attribute.
   Previously every flag was its own entity, and whenever the cloud returned `null` for
   them they flapped to `unknown`.
2. **Parameter values** (temperatures, pressures, speeds, versions, IDs, …) remain
   **individual sensors** — they are not aggregated.

To keep the history graphs continuous:

- parameter tables are **merged**, never replaced, so a partial cloud response cannot
  wipe previously known values;
- sensor values are **sticky**: a missing value falls back to the last known one instead
  of `unknown`;
- after a restart the previous state is restored via `RestoreEntity`, so restarts do not
  create gaps either.



## Polling policy (v1.3.0)

The interval follows the data source, balancing responsiveness against RS485-bus and vendor-API
load:

| Data source | Entity refresh | Vendor cloud API |
|---|---|---|
| **LAN mode** (bridge reachable) | **15 s** | once every **60 s** (unit ids + fault flags only) |
| **Cloud mode** (no bridge) | **45 s** (15-600 s configurable in the options) | every poll |

In LAN mode the integration requests `/api/state?max_age=8`: the bridge refreshes its snapshot
synchronously only when it is older than 8 s (one full sweep takes ~2.5 s), so a 15 s poll costs
exactly one sweep and leaves the RS485 bus mostly free; the cloud is still synced every 60 s for
unit ids and the fault flags.

Why these numbers:

* a full read is ~13 register blocks (~2.5 s), so 15 s keeps the bus duty around 20%; faster
  cycles (e.g. 5 s) would noticeably delay the vendor-polled requests and writes;
* in cloud mode the data is a vendor-side cached snapshot anyway, so 45 s is responsive enough
  and keeps the API load low;
* the cloud-mode interval stays configurable (15-600 s) in the integration options.

## Diagnostics: local-first (v1.2.0)

**The failure mode**: 95+ diagnostic entities (25 outdoor run-time parameters plus 12 per indoor
unit) were created from the vendor cloud's run-time table
(`customservice/selectConfigById`).  That endpoint is slow (10-40 s) and sometimes returns
`"--"`; if it failed once during a Home Assistant restart the entities were never created and
every one of them showed as *unavailable* until the integration was manually reloaded.

**What it does now**: the parameter set is fixed, therefore

1. `params.py` defines all parameters statically (names identical to the cloud), so the
   entities are **always created**;
2. values are read from the controller over Modbus whenever possible:
   - outdoor `val100..val124` -> holding registers `0x0064..0x007C` (all 25 values, including
     temperatures, pressures, current, compressor frequency, relay states, run mode, defrost);
   - indoor `val200..val213` -> each unit's state block `0x00C8 + 20*(slot-1)` (versions,
     identity code, return-air temperature, float switch, floor valve, drain pump, valve
     direction, fan rpm, room temperature);
3. only the protection/fault flags (`val125_*`, `val211_*`, ...) have no known register and
   still come from the cloud;
4. the cloud table is refreshed **in the background** (it never blocks startup), is retried after
   90 s on failure and now has a generous 75 s timeout;
5. when the cloud is unreachable the last known unit list is reused, so local control and the
   diagnostics keep working instead of the whole integration going unavailable.

Every parameter entity exposes a `value_source` attribute (`local` / `cloud`).
Self-test: `python3 tests/test_param_decoding.py`.

## Cloud reconciliation (LAN → cloud)

The vendor cloud only updates a unit's state when the command comes through **its own API**;
raw Modbus writes through the bridge are invisible to it, so the official app keeps showing
the old state ("turned it on in HA, the app still says off").

Every coordinator poll therefore compares the controller state with the cloud record and,
when a mismatch persists for **3 minutes** (so a command just issued in the app is not fought),
mirrors the local state through the cloud API — at most once per 5 minutes per unit:

| Item | Behaviour |
|---|---|
| Power | cloud `status` differs → cloud `modifyVal=1` |
| Mode | differs while the unit is on → cloud `modifyVal=2` |
| Target temperature | differs while the unit is on → cloud `modifyVal=4` (exact value via flag+base; the cloud rejects setpoints while off) |

Only one item per unit per cycle is mirrored, so the states converge one after another;
the actions are logged (`mirrored ...`).

## Link robustness (watchdogs & fail-safes)

The chain is a transparent relay (DTU ⇄ bridge ⇄ vendor cloud), so either side can go
silent while the TCP connection stays up. The bridge therefore protects itself:

| Mechanism | Behaviour |
|---|---|
| **Cloud-idle watchdog** | no bytes from the vendor cloud for 300 s (with the connection still open) → drop it, so the DTU reconnects and re-registers with the cloud |
| **DTU-idle watchdog** | no bytes from the DTU for 180 s (heartbeats included) → drop and wait for a reconnect |
| **Stream validation** | a frame is only trusted when the MBAP header is valid (protocol 0, sane length); garbage bytes are dropped and counted, never forwarded to the cloud (they can poison the vendor session) |
| **Heartbeat detection** | `00` + 12 hex chars (optional CRLF) is relayed as a DTU heartbeat; a valid Modbus frame whose transaction id starts with `0x00` is not mistaken for one |
| **Late-response swallowing** | when an injected request timed out, its late answer is dropped instead of leaking to the vendor as an unsolicited frame |
| **Injection TID check** | a response is only consumed when both transaction id and function code match |
| **Queue cap** | more than 1 MB pending in either direction → drop and reconnect instead of growing memory |
| **Send errors** | any send error drops the connection; the watchdogs/reconnect logic recover |

The bus load was also reduced: the state snapshot refreshes every 5 s only while an API client
is active (8 s window) and stops completely when idle, so it cannot starve the DTU heartbeat.

> If `/health` shows a growing `cloud_idle_drops` while `cloud_frames` stays frozen, the vendor
> cloud is refusing to resume polling; power-cycle the heat pump (DTU) once so it re-registers.

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
