# How this was reverse engineered

> Target: Emerson/Copeland **Sensi Hydro** heat-pump floor-heating central AC,
> 6 indoor units + 1 outdoor unit, DTU model **HF5111B** (High-Flying), firmware `1.34.11e`.
> The OEM app talks to `sensihydro.copeland.cn`; the app vendor is gone, so the goal was
> to control the system locally without breaking the official app.

## 0. TL;DR

The chain is surprisingly standard:

```
App ──HTTPS──▶ vendor cloud ──TCP:5000──▶ DTU (HF5111B) ──RS485 Modbus RTU 19200 8E1──▶ controller ──▶ 6 units
```

The DTU is a **transparent serial-to-TCP gateway that only dials out**. It converts bytes
between its TCP connection and Modbus RTU frames on the RS485 bus. The vendor cloud speaks
**standard Modbus TCP (MBAP framing)** on port 5000, so anything that accepts the DTU
connection can read and write the controller registers directly.

## 1. Finding the DTU

1. Look at the router's ARP table for the gateway MAC — High-Flying modules use the
   `34:EA:E7:xx:xx:xx` prefix — and note its IP (`192.168.1.20` in the examples).
2. Port scan:

   | Port | Protocol | Purpose |
   |---|---|---|
   | 23 | TCP | Telnet configuration console (some firmware) |
   | 48899 | UDP | High-Flying IOTService discovery/configuration |
   | 80 | TCP | Built-in web configuration (some firmware) |

3. The IOTService protocol (a JSON announce broadcast on UDP 48899; the DTU then connects
   back to TCP 48907) can read/write the DTU configuration and issue a `reload`.
   `tools/dtu_reload.py` re-implements exactly this part. The relevant settings were:

   - work mode: `TCP Client`
   - server: `sensihydro.copeland.cn:5000`
   - UART: `19200, 8, E, 1`
   - heartbeat: periodic ASCII `00` + the 12-digit MAC after connect

> The DTU configuration is only *read*, never modified — that is what keeps the official
> app working alongside the local path.

## 2. Decompiling the OEM app

The OEM Android app was decompiled with jadx. Findings:

- API base: `https://sensihydro.copeland.cn/api/v2`
- login: `POST /login` (phone + password) returning a JWT, sent as `x-access-token`
- `GET /device/list` (gateways), `GET /unit/list/{deviceId}` (indoor units)
- every control action is `POST /unit/update` with a `modifyVal` discriminator:
  `1` power, `2` mode, `3` sleep, `4` temperature (±0.5 flags), `5` fan speed
- host mode (normal/eco/quiet/boost) is `POST /device/updateMode` with `deviceMode` 0..8
- parameter tables: `GET /customservice/selectConfigById/{serialno}` — **slow**, ~15 s

These endpoints are undocumented and unstable; `custom_components/sensihydro/api.py`
is the client implementation and will need maintenance if the cloud changes.

## 3. Catching the DTU with DNS

The DTU only dials `sensihydro.copeland.cn:5000`, so pointing that name at our machine makes
the DTU connect to us. But rewriting the name **globally** breaks everything else:

- the OEM app and Home Assistant also use that name over **HTTPS 443**;
- nothing listens on 443 on the NAS → all cloud calls time out and the HA integration dies.

The final design is **split DNS**: only the DTU's own queries are redirected (AdGuard
per-client upstream → the bridge's built-in DNS on `:15353`), everyone else resolves the
real vendor address. See [architecture.md](architecture.md).

## 4. Relay + sniff: port 5000 is Modbus TCP

The bridge on `:5000` does two things at once:

1. relays every byte to/from the real vendor server (`40.73.97.71:5000`), so the official
   app is unaffected;
2. parses frames on the side:
   `[transaction id 2][protocol 0x0000 2][length 2][unit 1][function 1][data…]` —
   standard Modbus TCP. Observed functions: `0x03` read holding registers,
   `0x06` write single register, `0x10` write multiple registers.

Watching which addresses the cloud polls revealed the state and control blocks
(see [registers.md](registers.md)).

## 5. Probing registers (read first, write small, verify three ways)

1. **Read-only scan**: read 20-register blocks starting at `0x00C8`; values like `4800`
   or `1780` turned out to be temperatures with `raw = 3000 + ℃ × 100`;
2. **Slot layout**: state blocks every 20 registers (`0x00C8 + 20n`), control blocks every
   15 registers (`0x03E8 + 15n`), 7 slots (one empty);
3. **Write verification**: change the target temperature by ±0.5 ℃ and cross-check
   (a) the read-back register, (b) the OEM app, (c) the actual unit / wired controller.
   This identified the control block: `+0` power, `+1` `mode<<8|power`, `+2` target
   temperature, `+3` fan;
4. **Limits**: the controller accepts at most 20 registers per read (larger reads time out),
   writes take a few hundred milliseconds, and too-rapid consecutive writes are dropped.

## 6. Cracking host mode

Toggling host mode in the app produced a burst of register writes at fixed addresses:

- `0x05DD`: water setpoint (45 ℃ normal/eco/quiet, 50 ℃ boost);
- `0x05DE`: quiet flag (1 on / 2 off);
- `0x05E0`: eco flag (1 on / 2 off).

Three flags combine into the 8 modes that map to the cloud's `deviceMode` 0..8. Important:

- write order: flags first, setpoint last;
- the controller needs **2–5 s between writes** (the cloud itself spaced them 4–5 s);
  anything faster is silently dropped → implementation does write → verify → retry.

## 7. Verification

Everything is considered correct only when three views agree:

1. register read-back;
2. the OEM app (cloud sync is delayed 15–30 s, that is normal);
3. the physical unit / wired controller.

The HA integration works this way: LAN reads/writes are primary, the cloud is the fallback
and provides things that cannot be read locally (fault/protection bits, some text states).

## 8. Pitfalls we hit

| Pitfall | Symptom | Fix |
|---|---|---|
| Global DNS rewrite | HA cloud API timeouts, app broken at home | per-client split DNS |
| `$dnsrewrite` to a host without 443 | TLS handshake failures | ditto |
| Reading >20 registers at once | timeouts | read in blocks |
| Setting temperature right after a mode change | temperature write dropped | wait 2 s + retry |
| Re-reading 12 registers per HTTP request | 2–3 s per click in the UI | background snapshot + optimistic UI |
| Slow cloud `selectConfigById` (15 s+) | whole HA integration unavailable | make extra data failures non-fatal |
| DTU keeps the old connection | DNS change has no effect | JCMD `reload` (or power-cycle the AC) |
