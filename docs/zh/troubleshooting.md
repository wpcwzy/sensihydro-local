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
