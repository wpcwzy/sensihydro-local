# Protocol details

## 1. The DTU (HF5111B)

| Item | Value |
|---|---|
| Role | TCP client (dials out only) |
| Server | `sensihydro.copeland.cn:5000` |
| UART | `19200 8E1` |
| Wire | Modbus RTU on the serial side, **Modbus TCP (MBAP)** on the TCP side |
| Heartbeat | periodic ASCII `00` + 12-digit MAC after connect |
| Configuration | High-Flying IOTService protocol (UDP 48899 announce → DTU dials back to TCP 48907 → JSON JCMD) |

The heartbeat is not a Modbus frame; the bridge just relays it.

## 2. Modbus TCP frame (vendor cloud on port 5000)

```
offset  size  field
0       2     transaction id
2       2     protocol id 0x0000
4       2     length of the following bytes (unit id + PDU)
6       1     unit id (always 0x01 for this device)
7       1     function code
8       ...   data
```

Functions observed:

| Code | Meaning | Request | Response |
|---|---|---|---|
| `0x03` | read holding registers | address(2) + count(2) | byte count + data |
| `0x06` | write single register | address(2) + value(2) | echo |
| `0x10` | write multiple registers | address(2) + count(2) + bytes(1) + data | address + count |

**At most 20 registers per read** — larger reads time out or return exceptions, so the bridge
reads in blocks of ≤20.

## 3. Value encoding

All temperatures use a fixed-point encoding:

```
raw = 3000 + ℃ × 100
℃  = (raw - 3000) / 100
```

e.g. `4800` → 18.00 ℃; `1780` → -12.20 ℃ (values below 3000 are legitimate).

| Field | Encoding |
|---|---|
| power | `0` / `1` |
| mode | `mode << 8 \| power bit` (mode 0–5) |
| target temperature | `3000 + ℃ × 100` (0.5 ℃ steps) |
| fan | `0x0100` = auto, `1`–`10` = manual gear |
| float valve / valve / pump | `0` / `1` |

Modes: `0` cool, `1` fan only, `2` heat, `3` floor heating, `4` heat+floor, `5` dry.

## 4. Unit slots

The controller has 7 indoor-unit slots (one empty), laid out linearly:

```
state block:   0x00C8 + 20 × (slot - 1)     (20 registers)
control block: 0x03E8 + 15 × (slot - 1)     (4 registers)
```

Measured (6 units):

| Unit | Slot | State block | Control block |
|---|---|---|---|
| 餐厅 (dining) | 1 | `0x00C8` | `0x03E8` |
| 三楼南 | 2 | `0x00DC` | `0x03F7` |
| (empty) | 3 | `0x00F0` | `0x0406` |
| 二楼南 | 4 | `0x0104` | `0x0415` |
| 三楼北 | 5 | `0x0118` | `0x0424` |
| 客厅 (living) | 6 | `0x012C` | `0x0433` |
| 二楼北 | 7 | `0x0140` | `0x0442` |

The `UNITS_FILE` environment variable accepts a JSON file overriding names/slots/addresses
(see `examples/units.example.json`), which makes it easy to adapt to other installations.

## 5. Write quirks

All of these are handled in the code:

1. writes take effect after 0.2–1 s;
2. too-rapid consecutive writes are dropped — after a mode change a target-temperature write
   is often lost → wait 2 s and retry the temperature write (verify by read-back);
3. target temperature writes are ignored while the unit is off;
4. host-mode writes need 2–5 s spacing and the order quiet → eco → setpoint;
5. there is no local timer register — the app's timers live in the cloud, use HA automations.

## 6. Transaction ids and injection

Injected requests use transaction ids `0x8000`–`0x8FFF`:

- their responses are intercepted by the bridge and not forwarded to the cloud;
- cloud requests and responses pass through untouched;
- injections are serialised so the RS485 bus never sees interleaved requests.

## 7. Heartbeat and reconnects

- the DTU sends `00<MAC>` every few seconds;
- after a disconnect the DTU reconnects within seconds to tens of seconds;
- to force a DNS re-resolution use `tools/dtu_reload.py` (JCMD `reload`) or power-cycle the
  AC controller.

## 8. Debugging

- `/api/read` and `/api/write` allow raw register access;
- `LOG_LEVEL=DEBUG` logs every frame (transaction id, function, address);
- check `/health` (`dtu_connected`, `cloud_connected`) and whether `injected_requests` grows.
