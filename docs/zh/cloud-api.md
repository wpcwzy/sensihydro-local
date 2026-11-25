# 厂商云 HTTP API 记录（非官方）

> 这些接口来自 OEM App 的反编译与抓包，**没有文档、随时可能变化**。
> 本仓库的 HA 集成只把它们当作"兜底通道"，局域网控制不依赖它们。

## 认证

```
POST https://sensihydro.copeland.cn/api/v2/login
Content-Type: application/json

{"username": "<手机号>", "password": "<密码>"}
```

响应里的 `data.token` 是 JWT，之后所有请求带

```
x-access-token: <token>
```

Token 有效期约 30 天，每次响应会下发新 token（客户端应滚动更新）。

## 常用接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/device/list` | 网关/主机列表（含 `id`、`serialno`、`deviceMode`） |
| GET | `/unit/list/{deviceId}` | 内机列表（`id`、`name`、`status`、`mode`、`targetTemperature`、`actualTemperature`、`windModel`、`windSpeed` …） |
| GET | `/device/checkOnline/{serialno}` | 网关在线状态，`data == "ok"` 表示在线 |
| GET | `/customservice/selectConfigById/{serialno}` | 参数表（外机/内机参数）；**实测 15 秒左右**，不要频繁调用 |
| POST | `/unit/update` | 控制内机，见下 |
| POST | `/device/updateMode` | 主机模式 |

## 内机控制

```
POST /unit/update
{"deviceId": <id>, "serialno": "<sn>", "unitId": <unitId>, "modifyVal": <n>, ...}
```

`modifyVal` 的取值：

| modifyVal | 含义 | 额外字段 |
|---|---|---|
| `1` | 开关 | `status`: 0/1 |
| `2` | 模式 | `mode`: "0".."5" |
| `3` | 睡眠 | `sleep`: 0/1 |
| `4` | 温度 | `flag`: 0=+0.5、1=-0.5，`targetTemperature`: 基准值 |
| `5` | 风速 | `windSpeed`: 0..10，`windModel`: 1=auto / 0=手动 |

> 温度接口以"±0.5 步进"为主；实现里会先读当前值再决定 `flag`，
> 必要时用绝对写 + 回读校验。

## 主机模式

```
POST /device/updateMode
{"id": <deviceId>, "deviceMode": "0".."8"}
```

`deviceMode` 与本地寄存器（`0x05DD/0x05DE/0x05E0`）的对应见 [registers.md](registers.md)。
云端会对本地寄存器改动做轮询同步（约 15–30 秒后 App 显示一致）。

## 已知怪癖

1. `selectConfigById` 很慢（15 秒级），超时很常见 → HA 集成里把"扩展数据"失败降级为可忽略；
2. 设备离线时云端仍返回缓存的内机数据（所以要以 `/device/checkOnline` 判断真实在线）；
3. 云端写入到设备生效需要几秒到几十秒，且可能被设备状态覆盖；
4. 接口没有公开文档，字段名/行为变化即可能破坏集成——局域网通道不受影响。

## 合规提示

- 只使用你自己的账号；
- 不要高频轮询（会被限流/封禁），正常 30–60 秒一次足够；
- 本项目不包含、不传播任何账号、token 或 APK。
