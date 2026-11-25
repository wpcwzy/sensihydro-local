# Sensi Hydro Local — LAN control for Emerson / Copeland "Sensi Hydro" central AC

[中文说明](README.zh-CN.md) · [Reverse-engineering notes](docs/en/reverse-engineering.md) · [Register map](docs/en/registers.md)

A complete, reverse-engineered **local (LAN) control stack** for the Emerson/Copeland
**Sensi Hydro (心海悦)** heat-pump floor-heating air conditioner — the one whose OEM
mobile app (Sensi Home / 心海悦) talks to `sensihydro.copeland.cn`.

The vendor's cloud is still used, but **only as one of two paths**: a Docker bridge
impersonates the vendor data server for the DTU, relays every frame to the real cloud
(so the official app keeps working), and at the same time injects its own Modbus
requests to read and control the indoor units directly over RS485.

```
                    ┌────────────────────── NAS / mini-PC ──────────────────────┐
                    │                                                           │
  DTU HF5111B       │   bridge container (host network)                         │
  ┌────────────┐    │   ┌──────────────────┐         ┌───────────────────────┐  │
  │ Modbus RTU │────┼──▶│ :5000 relay      │────────▶│ vendor cloud :5000    │  │
  │ 19200 8E1  │    │   │ (transparent)    │         │ (official mobile app) │  │
  └─────┬──────┘    │   └────────┬─────────┘         └───────────────────────┘  │
        │ RS485     │            │ Modbus injection                            │
  ┌─────▼──────┐    │   ┌────────▼─────────┐         ┌───────────────────────┐  │
  │ controller │    │   │ :8090 web + API  │◀───────▶│ Home Assistant        │  │
  │ 6 units    │    │   └──────────────────┘         │ custom integration    │  │
  └────────────┘    │                                 └───────────────────────┘  │
                    └───────────────────────────────────────────────────────────┘
```

## What you get

| Component | What it does |
|---|---|
| **`bridge/`** | Docker service. Impersonates the vendor server for the DTU, relays to the real cloud, injects Modbus reads/writes, exposes a web remote + JSON API, and runs a tiny split-DNS server so only the DTU is redirected. |
| **`custom_components/sensihydro/`** | Home Assistant integration (HACS-compatible layout). Cloud data + live LAN data merged; all control commands prefer the LAN path and fall back to the cloud. |
| **`tools/`** | `switchover.py` (configure AdGuard split DNS + make the DTU reconnect), `dtu_reload.py` (force the HF5111B to reconnect via its JCMD protocol). |
| **`extras/bemfa_xiaoai/`** | Optional: sync a few HA entities to Bemfa cloud so Xiaomi Mijia / Xiaoai voice assistants can control them. |

### Features

- Per-unit: power, mode (cool / fan / heat / floor-heating / heat+floor / dry), target
  temperature ±0.5 °C, fan speed (auto or 1–10), room temperature (wired controller and
  return air), floor valve state, fan rpm.
- **Host mode** (the app's 常规 / 节能 / 静音 / 强热): normal, eco, quiet, boost and all
  combinations — mapped to the cloud's `deviceMode` 0..8.
- Outdoor-unit diagnostics (ambient/water/coil/discharge temps, pressures, compressor
  rpm, currents, EEV positions) read locally.
- Diagnostics that stay readable: one aggregated **fault summary** sensor (`正常` or a
  comma-separated list of active faults) and parameter sensors that keep their last
  value instead of flapping to `unknown`, so the history graphs stay continuous.
- Web remote control (mobile friendly), JSON API, Home Assistant + HomeKit via HA.
- Both the official app **and** local control work at the same time.

## Requirements

- Emerson/Copeland Sensi Hydro heat pump with an **HF5111B** (or compatible) DTU
  connected to the indoor controller over RS485.
- A device that can run Docker 24/7 on the same LAN (NAS, mini-PC, Raspberry Pi…).
- Optional: AdGuard Home (or another DNS server that supports per-client upstreams)
  and Home Assistant.

> Tested on: `deviceType XMSI-V09A-TXP-D01`, 6 indoor units + 1 outdoor unit,
> DTU firmware `1.34.11e`. Other models of the same family likely share the protocol,
> but register layouts may differ — read [docs/en/registers.md](docs/en/registers.md)
> and verify before writing anything.

## Quick start

1. **Deploy the bridge** (host networking recommended):

   ```bash
   git clone https://github.com/wpcwzy/sensihydro-local.git
   cd sensihydro-local
   cp .env.example .env          # set LAN_IP to the address of this machine
   docker compose up -d --build
   curl http://127.0.0.1:8090/health
   ```

2. **Redirect only the DTU** (split DNS). The DTU resolves the vendor host name itself,
   so it needs a different answer than every other device. With AdGuard Home:

   ```bash
   python3 tools/switchover.py --bridge-ip <bridge-ip> --dtu <dtu-ip> \
       --adguard http://<adguard-ip>:8080 --adguard-user admin
   ```

   The script creates a per-client upstream for the DTU pointing at the bridge's
   embedded DNS (`<bridge-ip>:15353`) and removes any legacy global `$dnsrewrite`
   rules (those break HTTPS for Home Assistant and phones — see
   [docs/en/architecture.md](docs/en/architecture.md#why-not-a-global-dns-rewrite)).

3. **Verify**:

   ```bash
   curl http://<bridge-ip>:8090/health
   # {"ok": true, "dtu_connected": true, "cloud_connected": true, ...}
   ```

   Open `http://<bridge-ip>:8090/` for the web remote control.

4. **Home Assistant** (optional):
   - **HACS**: HACS → Integrations → ⋮ → Custom repositories → this repo URL,
     category *Integration* → install "Sensi Hydro Local" → restart HA;
   - **manual**: copy `custom_components/sensihydro/` into `/config/custom_components/`
     and restart HA;
   - add the integration with the OEM app account (phone + password), then set
     *Local bridge URL* to `http://<bridge-ip>:8090` in the integration options. A
     `binary_sensor.…_local_bridge` entity turns `on` when the local path is healthy.

5. **Xiaomi / Xiaoai** (optional): see [extras/bemfa_xiaoai](extras/bemfa_xiaoai/README.md).

## Documentation

| Document | Content |
|---|---|
| [docs/en/reverse-engineering.md](docs/en/reverse-engineering.md) | How the protocol was recovered: APK decompilation, DTU inspection, relay sniffing, register probing. |
| [docs/en/architecture.md](docs/en/architecture.md) | Network design, relay behaviour, split DNS, cloud fallback. |
| [docs/en/protocol.md](docs/en/protocol.md) | DTU behaviour, Modbus-over-TCP framing, temperature encoding, write quirks. |
| [docs/en/registers.md](docs/en/registers.md) | Full register map (units, outdoor unit, host mode) + how to discover your own. |
| [docs/en/cloud-api.md](docs/en/cloud-api.md) | OEM cloud HTTP API notes (login, units, writes, `deviceMode`). |
| [docs/en/troubleshooting.md](docs/en/troubleshooting.md) | Common failures and fixes. |
| [中文文档](docs/zh/) | 以上文档的中文版本 |

## Safety, warranty and legal notes

- The bridge **never writes the DTU configuration** — it only relays traffic and uses
  the DTU's existing Modbus channel. `tools/dtu_reload.py` sends the vendor tool's own
  "reload" command, nothing else.
- Local register writes were verified on real hardware, but **you are writing to your
  own HVAC controller**: start with read-only exploration, keep setpoints sane, and
  have a rollback (stop the container / restore DNS) ready.
- This is interoperability work for equipment you own. It contains **no vendor
  credentials, no APK, no cloud tokens**. You need your own OEM app account.
- The OEM cloud can change at any time; the LAN path keeps working even if it does.
- Reverse engineering may be restricted by local law / the vendor's EULA — use at your
  own risk, no warranty.

## License

MIT — see [LICENSE](LICENSE).
