# 心海悦本地化 — 艾默生/谷轮「心海悦」中央空调局域网控制

[English](README.md) · [逆向过程](docs/zh/reverse-engineering.md) · [寄存器表](docs/zh/registers.md)

这是一套完整的、**逆向出来的**艾默生/谷轮 **心海悦 (Sensi Hydro)** 热泵地暖中央空调
局域网控制方案。官方 App（Sensi Home / 心海悦）通过 `sensihydro.copeland.cn` 与云端通信，
本方案用一个 Docker 桥接服务在局域网里"冒充"厂商数据服务器接住 DTU 的连接，
同时把数据原样转发给真正的厂商云（**官方 App 照常可用**），
并在同一条连接上插入自己的 Modbus 读写请求，**直接控制 6 台内机**。

```
                    ┌──────────────────── NAS / 软路由 / 小主机 ─────────────────┐
                    │                                                           │
  DTU HF5111B       │   bridge 容器（host 网络）                                │
  ┌────────────┐    │   ┌──────────────────┐         ┌───────────────────────┐  │
  │ Modbus RTU │────┼──▶│ :5000 透明转发    │────────▶│ 厂商云 :5000          │  │
  │ 19200 8E1  │    │   │                  │         │ （官方 App 使用）      │  │
  └─────┬──────┘    │   └────────┬─────────┘         └───────────────────────┘  │
        │ RS485     │            │ Modbus 注入                                 │
  ┌─────▼──────┐    │   ┌────────▼─────────┐         ┌───────────────────────┐  │
  │ 主控板      │    │   │ :8090 网页 + API │◀───────▶│ Home Assistant 集成   │  │
  │ 6 台内机    │    │   └──────────────────┘         │ 本地优先 / 云端兜底    │  │
  └────────────┘    │                                 └───────────────────────┘  │
                    └───────────────────────────────────────────────────────────┘
```

## 这个仓库包含什么

| 组件 | 作用 |
|---|---|
| **`bridge/`** | Docker 服务：冒充厂商服务器接住 DTU、把数据转发给真厂商云、注入 Modbus 读写、提供网页遥控器和 JSON API，并内置迷你 DNS（只重定向 DTU）。 |
| **`custom_components/sensihydro/`** | Home Assistant 集成（目录结构可直接用 HACS 安装）。云端数据 + 局域网实时数据合并，所有控制优先走局域网，失败自动回退云端。 |
| **`tools/`** | `switchover.py`（配置 AdGuard 分流 DNS 并让 DTU 重连）、`dtu_reload.py`（用汉枫 JCMD 协议单独让 HF5111B 重连）。 |
| **`extras/bemfa_xiaoai/`** | 可选：把 HA 实体同步到巴法云，接入米家 App / 小爱同学语音控制。 |

### 功能

- 每台内机：开关、模式（冷风 / 送风 / 热风 / 地暖 / 热+暖 / 除湿）、目标温度 ±0.5℃、
  风速（自动或 1–10 档）、室温（线控器 + 回风）、地暖阀状态、风机转速；
- **主机模式**（App 里的 常规 / 节能 / 静音 / 强热，可叠加）：完整对应云端 `deviceMode` 0~8；
- 外机诊断参数（环境/出水/回水/盘管/排气/回气温度、蒸发/冷凝压力、压缩机转速、电流、膨胀阀开度）本地直读；
- 网页遥控器（手机友好）、JSON API、Home Assistant（可再经 HA 接 HomeKit）；
- **官方 App 与局域网控制同时可用**。

## 环境要求

- 艾默生/谷轮 心海悦 热泵，带 **HF5111B**（或兼容）DTU，DTU 通过 RS485 接主控板；
- 一台能 7×24 跑 Docker 的局域网设备（NAS、小主机、树莓派等）；
- 可选：支持"按客户端上游 DNS"的 DNS 服务器（如 AdGuard Home）、Home Assistant。

> 实测机型：`deviceType XMSI-V09A-TXP-D01`，6 台内机 + 1 台外机，DTU 固件 `1.34.11e`。
> 同系列其它机型大概率协议相同，但寄存器布局可能不同——请先读
> [docs/zh/registers.md](docs/zh/registers.md)，**只读确认后再写**。

## 快速开始

1. **部署 bridge**（建议 host 网络）：

   ```bash
   git clone https://github.com/wpcwzy/sensihydro-local.git
   cd sensihydro-local
   cp .env.example .env          # 把 LAN_IP 改成运行本机器的地址
   docker compose up -d --build
   curl http://127.0.0.1:8090/health
   ```

2. **只把 DTU 指过来**（分流 DNS）。DTU 自己解析厂商域名，因此它需要和家里其它设备
   得到不同的解析结果。以 AdGuard Home 为例：

   ```bash
   python3 tools/switchover.py --bridge-ip <bridge-ip> --dtu <dtu-ip> \
       --adguard http://<adguard-ip>:8080 --adguard-user admin
   ```

   脚本会为 DTU 建一个"按客户端上游 DNS"条目，指向 bridge 内置 DNS
   （`<bridge-ip>:15353`），并清理历史遗留的全局 `$dnsrewrite` 规则
   （全局重定向会把 HA/手机的 HTTPS 也劫持到 NAS，导致云端接口超时——
   见 [docs/zh/architecture.md](docs/zh/architecture.md)）。

3. **验证**：

   ```bash
   curl http://<bridge-ip>:8090/health
   # {"ok": true, "dtu_connected": true, "cloud_connected": true, ...}
   ```

   浏览器打开 `http://<bridge-ip>:8090/` 即可用网页遥控器。

4. **Home Assistant**（可选）：
   - **HACS 安装**：HACS → 集成 → 右上角 ⋮ → 自定义仓库 → 填本仓库地址、类别选「集成」→
     安装 "Sensi Hydro Local" → 重启 HA；
   - **手动安装**：把 `custom_components/sensihydro/` 拷到 `/config/custom_components/` → 重启 HA；
   - 用官方 App 的账号（手机号+密码）添加集成，然后在集成「配置」里把「本地桥接地址」填
     `http://<bridge-ip>:8090`。本地通道正常时会出现 `binary_sensor.…_本地桥接` = `on`。

5. **小爱同学**（可选）：见 [extras/bemfa_xiaoai](extras/bemfa_xiaoai/README.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/zh/reverse-engineering.md](docs/zh/reverse-engineering.md) | 逆向全过程：APK 反编译、DTU 探测、中继抓包、寄存器试探 |
| [docs/zh/architecture.md](docs/zh/architecture.md) | 网络架构、透明转发、分流 DNS、云端兜底 |
| [docs/zh/protocol.md](docs/zh/protocol.md) | DTU 行为、Modbus-over-TCP 帧格式、温度编码、写入怪癖 |
| [docs/zh/registers.md](docs/zh/registers.md) | 完整寄存器表（内机/外机/主机模式）+ 如何探测自己的地址 |
| [docs/zh/cloud-api.md](docs/zh/cloud-api.md) | 厂商云 HTTP API 记录（登录、内机列表、写入、`deviceMode`） |
| [docs/zh/troubleshooting.md](docs/zh/troubleshooting.md) | 常见问题与处理 |
| [English docs](docs/en/) | 以上文档的英文版本 |

## 安全、保修与法律提示

- bridge **不修改 DTU 的任何配置**，只做转发 + 复用 DTU 已有的 Modbus 通道；
  `tools/dtu_reload.py` 发送的是厂商工具自己的 reload 命令，没有别的写操作。
- 本方案对真实控制器做过写操作验证，但**你是在给自己的暖通控制器写寄存器**：
  请先只读探测、设定值保持合理、准备好回滚（停容器 / 还原 DNS）。
- 本项目面向"自己拥有的设备"的互操作性逆向，仓库**不含任何厂商账号、APK、云端 token**；
  使用需要你自己的官方 App 账号。
- 厂商云随时可能变化；局域网通道不依赖云端，即使云端变了也仍可用。
- 逆向可能受当地法律或厂商协议限制，请自行判断风险，无任何担保。

## 许可证

MIT — 见 [LICENSE](LICENSE)。
