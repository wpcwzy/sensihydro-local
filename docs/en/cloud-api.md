# OEM cloud HTTP API notes (unofficial)

> Recovered from the decompiled OEM app and traffic capture. **Undocumented and subject to
> change.** The HA integration only uses it as a fallback; local control does not depend on it.

## Authentication

```
POST https://sensihydro.copeland.cn/api/v2/login
Content-Type: application/json

{"username": "<phone>", "password": "<password>"}
```

`data.token` is a JWT; send it as `x-access-token` on every request. It is valid for about
30 days and is rotated in each response (clients should keep updating it).

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/device/list` | gateways (`id`, `serialno`, `deviceMode`, …) |
| GET | `/unit/list/{deviceId}` | indoor units (`id`, `name`, `status`, `mode`, `targetTemperature`, `actualTemperature`, `windModel`, `windSpeed`, …) |
| GET | `/device/checkOnline/{serialno}` | gateway online state, `data == "ok"` when online |
| GET | `/customservice/selectConfigById/{serialno}` | parameter tables; **~15 s**, do not poll frequently |
| POST | `/unit/update` | control an indoor unit |
| POST | `/device/updateMode` | host mode |

## Controlling a unit

```
POST /unit/update
{"deviceId": <id>, "serialno": "<sn>", "unitId": <unitId>, "modifyVal": <n>, ...}
```

| modifyVal | Action | Extra fields |
|---|---|---|
| `1` | power | `status`: 0/1 |
| `2` | mode | `mode`: "0".."5" |
| `3` | sleep | `sleep`: 0/1 |
| `4` | temperature | `flag`: 0=+0.5, 1=-0.5; `targetTemperature`: base |
| `5` | fan | `windSpeed`: 0..10; `windModel`: 1=auto, 0=manual |

> The temperature API is step-based; the integration reads the current value first, then
> chooses the flag, and uses absolute writes + read-back verification when necessary.

## Host mode

```
POST /device/updateMode
{"id": <deviceId>, "deviceMode": "0".."8"}
```

Mapping between `deviceMode` and the local registers (`0x05DD/0x05DE/0x05E0`) is in
[registers.md](registers.md#3-host-mode-0x05dc-block). The cloud syncs local register
changes back into the app within ~15–30 s.

## Known quirks

1. `selectConfigById` is very slow (15 s), timeouts are normal → the integration treats
   "extra data" failures as non-fatal;
2. when the gateway is offline the cloud still serves cached unit data — use
   `/device/checkOnline` to know the real state;
3. cloud writes may take seconds to tens of seconds and can be overwritten by the device;
4. undocumented and unstable; the LAN path keeps working regardless.

## Compliance

- use your own account only;
- do not poll aggressively (rate limiting / bans) — 30–60 s is plenty;
- this project contains no accounts, tokens or APKs.
