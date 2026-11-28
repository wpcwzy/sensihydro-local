# 常见问题

## bridge 侧

| 现象 | 可能原因 | 处理 |
|---|---|---|
| `/health` 里 `dtu_connected=false` | DTU 还没被指过来 / 还连着旧地址 | 检查 AdGuard 客户端上游配置；执行 `python3 tools/dtu_reload.py --dtu <dtu-ip>`；或给空调主控板断电重启 |
| `cloud_connected=false` | bridge 无法访问厂商云 | 检查 NAS 出网、DNS、`VENDOR_HOST`；容器日志会有连接错误 |
| `injected_requests` 不增长 | 请求没发出去 | 检查容器日志、RS485 是否被占用 |
| 5000 端口起不来 | NAS 上有别的服务占了 5000 | 关掉占用服务（DTU 固定连 5000，无法改端口） |
| 网页打不开 | 不是 host 网络且没映射 8090 | 用 `ports:` 映射，或改 host 网络 |
| 网页控制延迟大 | 后台快照被禁用/RS485 被云端占满 | 确认容器是新版本（`/health` 有 `dns` 字段）；看日志 |
| 主机模式切换不生效 | 写太密被控制器丢弃 | 已内置"写→回读→重试"；仍失败时手动间隔 5 秒重试 |

## DNS / 网络

| 现象 | 可能原因 | 处理 |
|---|---|---|
| HA 云端接口超时、实体 unavailable | 仍在用**全局** `$dnsrewrite`，把 HTTPS 也劫持到 NAS | 删掉全局规则，改按客户端分流（`tools/switchover.py`） |
| 手机 App 在家连不上云端 | 同上 | 同上 |
| DTU 反复重连 | bridge 没启动或 DNS 指向错误 | 先启动容器再切 DNS；确认 `LAN_IP`/`DNS_PORT` |
| AdGuard 里客户端 IP 不对 | DTU 换了 IP | 更新 AdGuard 客户端条目的 IP |

## Home Assistant

| 现象 | 可能原因 | 处理 |
|---|---|---|
| `本地桥接` = off | 集成选项里地址填错 / 容器没起 | 集成 → 配置 → 本地桥接地址 = `http://<bridge-ip>:8090` |
| 集成整体 unavailable | 云端慢接口超时（旧版插件） | 更新到本仓库的集成（扩展数据失败不再致命） |
| 室温不刷新 | 没走本地通道 | 检查 `本地桥接` 状态；确认内机名与官方 App 一致 |
| 控制没反应但状态正常 | 云端写入延迟 | 看 bridge `/health` 的 `last_dtu_frame_age`，或直接看容器日志 |
| 主机模式 select 报错 | bridge 版本旧 | 更新容器（需要 `/api/hostmode`） |

## 语音助手（可选）

| 现象 | 处理 |
|---|---|
| 米家看不到设备 | 确认巴法云控制台里主题"在线"；米家 App 里重新添加"巴法" |
| 空调指令延迟十几秒 | 正常（云推送 + 控制器响应）；灯光类 1–2 秒 |
| 模式对不上 | 检查 `extras/bemfa_xiaoai/bridge.py` 里的映射表 |

## 回滚

```bash
# 1) 撤销 DNS 分流（删除 AdGuard 客户端条目）
python3 tools/switchover.py --bridge-ip <bridge-ip> --dtu <dtu-ip> \
    --adguard http://<adguard-ip>:8080 --adguard-user admin --remove

# 2) 停容器
docker compose down
```

> 如果 DNS 还指向 bridge 而容器停了，DTU 会连不上、App 显示离线；
> 先撤 DNS 或重新启动容器。

## 为什么 Docker 升级后 DTU 会绕过 bridge？（重要）

DTU 靠 DNS 被引导到 bridge：AdGuard 把这个客户端（`192.168.110.3`）的解析转发给 bridge 内置 DNS
（`192.168.110.28:15353`）。Docker 引擎升级 / 容器重建时 bridge 会短暂消失，AdGuard 在内置 DNS
不可用时**回退到公共 DNS**，DTU 于是拿到厂商真实地址、直接连到厂商云，并会一直保持这条连接——
bridge 再起来也叫不回来，只能给 DTU 断电重启。表现为：**App 正常，但本地遥控/HA 全部失灵**。

### 根治办法（按推荐顺序）

1. **路由器上禁止 DTU 上网（最彻底）**
   在路由器"设备管理 / 上网管控"里把 DTU（`192.168.110.3`）设为禁止访问外网、只允许局域网。
   DTU 就永远不可能直连厂商；bridge 重启期间它连不上任何地方、只会不停重试，
   bridge 一恢复就自动连回来。（App 仍正常：App 的数据本来就经 bridge 转发到厂商云。）

2. **把 DNS 应答搬到 AdGuard 那台机器上（不依赖 Docker，推荐）**
   AdGuard 所在的常开设备（本方案里是 `192.168.110.92`，Armbian）跑一个 systemd 服务：

   ```bash
   # 解压部署包后，在 AdGuard 那台机器上以 root 执行
   sh deploy/install_host_dns.sh 192.168.110.28 5333
   # 然后把 AdGuard 里 AC-DTU 客户端的上游改成 127.0.0.1:5333
   ```

   这样 DNS 应答完全不经过 NAS / Docker：Docker 升级、bridge 重建、甚至 NAS 重启期间，
   DTU 都只会解析到 NAS IP、连不上就一直重试，bridge 一恢复就自动连回来。
   （也可以把服务装在 NAS 宿主机上，此时 AdGuard 上游填 `192.168.110.28:5333`。）

3. **给 Docker 开 live-restore（减少重启影响）**
   NAS 的 `/etc/docker/daemon.json` 加入 `{ "live-restore": true }` 后 `systemctl restart docker`；
   之后重启/升级 Docker 引擎时容器保持运行（对 `docker compose up --build` 不生效）。

> 已经绕回厂商云时：先按上面任一项修好，再给空调主控板断电重启一次让 DTU 重新注册。

### 掉线提醒（可选）

bridge 掉线时 `binary_sensor.ao_yuan_120_2_ben_di_qiao_jie` 会变成 `off`，可以加一条自动化：

```yaml
automation:
  - alias: 空调本地桥接离线提醒
    trigger:
      - platform: state
        entity_id: binary_sensor.ao_yuan_120_2_ben_di_qiao_jie
        to: "off"
        for: "00:05:00"
    action:
      - service: persistent_notification.create
        data:
          title: 空调本地桥接离线
          message: 已 5 分钟未连上本地 bridge；若刚升级过 Docker，DTU 可能已绕回厂商云，必要时给空调主控板断电重启一次。
```

## 诊断实体突然全部"不可用"（v1.2.0 已修复）

现象：外机/内机的温度、压力、转速等几十个诊断实体同时变成 `unavailable`。

原因：这些实体以前只在厂商云参数表 `selectConfigById` 成功返回时才创建。该接口很慢
（10~40 秒）且偶尔超时，只要 HA 重启时它失败一次，实体就不会被创建，直到手动重载集成。

修复后：参数实体由静态定义创建，数值优先从 bridge 本地读取（见"诊断数据：本地优先"）。
排查时看实体的 `value_source` 属性：`local` = 本地寄存器，`cloud` = 云端参数表。

如果仍看到个别实体 `unknown`（如 `用电统计(原始)`），那是纯云端字段且厂商接口为空，属正常。
