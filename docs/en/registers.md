# Register map (verified)

> Temperatures: `℃ = (raw - 3000) / 100`; write `raw = 3000 + ℃ × 100`.
> At most 20 registers per read. Addresses come from one 6-unit + 1-outdoor installation;
> **other models/wirings may differ** — verify read-only first.

## 1. Indoor unit state block (20 registers at `0x00C8 + 20 × (slot-1)`)

| Offset | Meaning | Conversion |
|---|---|---|
| +0 | indoor control board firmware | raw |
| +1 | display panel firmware | raw |
| +2 … +4 | identification code (high/mid/low) | raw |
| **+5** | **return-air temperature** | `(raw-3000)/100` ℃ |
| +6 | float switch | 0/1 |
| +7 | floor-heating valve relay | 0/1 |
| +8 | drain pump relay | 0/1 |
| +9 | two-way valve direction | 0/1 |
| +10 | indoor fan speed | rpm |
| **+13** | **wired-controller room temperature** | `(raw-3000)/100` ℃ |
| +14 … +19 | reserved | — |

## 2. Indoor unit control block (4 registers at `0x03E8 + 15 × (slot-1)`)

| Offset | Meaning | Value |
|---|---|---|
| +0 | power | `1` on / `0` off |
| +1 | mode | `mode << 8 \| power bit` |
| +2 | target temperature | `3000 + ℃ × 100` |
| +3 | fan | `0x0100` auto; `1`–`10` manual gear |

Modes: `0` cool, `1` fan only, `2` heat, `3` floor heating, `4` heat+floor, `5` dry.

## 3. Host mode (`0x05DC` block)

| Register | Meaning | Values |
|---|---|---|
| `0x05DD` | water setpoint | `45` normal/eco/quiet; `50` boost |
| `0x05DE` | quiet flag | `1` on / `2` off |
| `0x05DF` | unknown (changes with mode, around `5`) | — |
| `0x05E0` | eco flag | `1` on / `2` off |

Combinations and the cloud `deviceMode`:

| quiet | eco | boost | setpoint | mode | `deviceMode` |
|---|---|---|---|---|---|
| off | off | off | 45 ℃ | normal | 0 |
| off | on | off | 45 ℃ | eco | 1 |
| on | on | off | 45 ℃ | eco+quiet | 2 |
| on | off | off | 45 ℃ | quiet | 3 |
| off | off | on | 50 ℃ | boost | 5 |
| off | on | on | 50 ℃ | boost+eco | 6 |
| on | off | on | 50 ℃ | boost+quiet | 7 |
| on | on | on | 50 ℃ | boost+eco+quiet | 8 |

Write rules:

1. order: `0x05DE`, `0x05E0`, then `0x05DD`;
2. spacing between writes ~2–5 s, otherwise writes are silently dropped;
3. verify by read-back and retry on mismatch.

## 4. Outdoor unit parameters (`0x0064` … `0x007C`)

The cloud parameter table `val100..val124` maps 1:1 to these registers:

| Register | Cloud param | Meaning | Conversion |
|---|---|---|---|
| `0x0064` | val100 | outdoor ambient Ta | `(raw-3000)/100` ℃ |
| `0x0065` | val101 | outdoor coil Tp | same |
| `0x0066` | val102 | discharge Td | same |
| `0x0067` | val103 | suction Ts | same |
| `0x0068` | val104 | cooling water Tc | same |
| `0x0069` | val105 | outlet water TO | same |
| `0x006A` | val106 | return water TI | same |
| `0x006B` | val107 | coil TC1 | same |
| `0x006C` | val108 | heating outlet water | same |
| `0x006D` | val109 | AC input voltage | `raw/10` V |
| `0x006E` | val110 | AC input current | `raw/10` A |
| `0x006F` | val111 | compressor speed | `raw` Hz |
| `0x0070` | val112 | main EEV position | `raw` P |
| `0x0071` | val113 | auxiliary EEV position | `raw` P |
| `0x0072` | val114 | evaporator pressure | `raw/10` |
| `0x0073` | val115 | condenser pressure | `raw/10` |
| … | … | remaining status/version/fault bits | see the cloud parameter table |

## 5. Indoor parameters (`val200..val213`)

The cloud parameter table's `val200..val210` and `val213` mirror the state block
(firmware, IDs, return-air temperature, float switch, floor valve, drain pump, two-way
valve, fan rpm, wired-controller room temperature). The HA integration overlays them with
live local values, so those sensors are real-time.

## 6. Default mapping and verification (important)

The default room -> slot mapping is verified, not guessed: the unique identity
code of every indoor board (state block +2/+3/+4) is matched against the
per-room identity codes reported by the vendor cloud.

| Room | Slot | State block | Control block |
|---|---|---|---|
| 餐厅 (dining) | 1 | `0x00C8` | `0x03E8` |
| 三楼南 | 2 | `0x00DC` | `0x03F7` |
| 二楼南 | 4 | `0x0104` | `0x0415` |
| 三楼北 | 5 | `0x0118` | `0x0424` |
| 客厅 (living) | 6 | `0x012C` | `0x0433` |
| 二楼北 | 7 | `0x0140` | `0x0442` |

After installation open `http://<bridge>:8090/api/mapping`: it lists the mapping in
use plus each board's identity code, so a mix-up is visible immediately.
A wrong mapping shows up as "tapping room A turns on room B".

Custom mapping: edit `examples/units.example.json` and point `UNITS_FILE` at it.

## 7. Discovering your own layout

1. confirm connectivity: `curl 'http://<bridge>:8090/api/read?addr=0x00c8&count=20'`;
2. find slots: read from `0x00C8` in steps of 20 and look for plausible
   return-air/room temperatures;
3. check the control block: slot *n* → `0x03E8 + 15 × (n-1)`;
4. verify by writing: change one unit's target by ±0.5 ℃ and cross-check read-back,
   the OEM app, and the physical unit;
5. host mode: use the table above, one combination at a time with read-back checks.

> ⚠️ Register writes really change how the HVAC behaves. Do it when nobody depends on it
> and keep a rollback ready (stop the container / restore DNS).

## 7. Debug endpoints

```bash
# read any registers
curl 'http://<bridge>:8090/api/read?addr=0x05dc&count=5'

# write a single register (hex string or number)
curl -X POST -H 'Content-Type: application/json' \
     -d '{"addr":"0x0435","value":"0x13ba"}' \
     http://<bridge>:8090/api/write
```
