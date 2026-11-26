# Troubleshooting

## Bridge

| Symptom | Likely cause | Fix |
|---|---|---|
| `dtu_connected=false` in `/health` | DTU not redirected yet / still on the old address | check the AdGuard client upstream; run `python3 tools/dtu_reload.py --dtu <dtu-ip>`; or power-cycle the AC controller |
| `cloud_connected=false` | bridge cannot reach the vendor cloud | check outbound network/DNS/`VENDOR_HOST`; container logs show connect errors |
| `injected_requests` not growing | requests are not sent | check logs; the RS485 bus may be busy |
| port 5000 will not bind | another service on the host uses it | free the port (the DTU always connects to 5000) |
| web UI not reachable | no host networking and no `ports:` mapping | map 8090 (and 5000/DNS) or switch to host networking |
| slow web control | background snapshot disabled / bus saturated | make sure `/health` includes a `dns` field (new build); check logs |
| host mode not applying | writes dropped by the controller | the code retries; retry manually with 5 s spacing |

## DNS / network

| Symptom | Cause | Fix |
|---|---|---|
| HA cloud calls time out, entities unavailable | a **global** `$dnsrewrite` still hijacks HTTPS to the NAS | delete global rules, use per-client split DNS (`tools/switchover.py`) |
| the OEM app fails at home | same | same |
| DTU keeps reconnecting | bridge down or wrong DNS answer | start the container first; check `LAN_IP`/`DNS_PORT` |
| wrong client IP in AdGuard | the DTU changed IP | update the AdGuard client entry |

## Home Assistant

| Symptom | Cause | Fix |
|---|---|---|
| `local bridge` binary sensor = off | wrong URL in options / container down | integration → configure → local bridge URL = `http://<bridge-ip>:8090` |
| whole integration unavailable | slow cloud endpoint (older build) | update to the integration in this repo (extra-data failures are non-fatal) |
| room temperature not updating | local path not used | check the binary sensor; make sure unit names match the OEM app |
| commands do nothing but state looks fine | cloud write latency | check `last_dtu_frame_age` in `/health`, container logs |
| host-mode select errors | bridge too old | update the container (needs `/api/hostmode`) |

## Voice assistants (optional)

| Symptom | Fix |
|---|---|
| Mijia cannot see the devices | verify the Bemfa topics are "online"; re-add the "Bema" platform in Mijia |
| AC commands take ~10–15 s | expected (cloud push + controller latency); lights take 1–2 s |
| wrong mode appears | check the mapping table in `extras/bemfa_xiaoai/bridge.py` |

## Rollback

```bash
# 1) undo split DNS (delete the AdGuard client entry)
python3 tools/switchover.py --bridge-ip <bridge-ip> --dtu <dtu-ip> \
    --adguard http://<adguard-ip>:8080 --adguard-user admin --remove

# 2) stop the container
docker compose down
```

> If DNS still points at the bridge while the container is down, the DTU cannot connect and
> the app shows the device offline — undo the DNS entry first, or start the container again.

## Why does the DTU bypass the bridge after a Docker upgrade? (important)

The DTU is steered to the bridge by DNS: AdGuard forwards this client's lookups to the bridge's
built-in DNS (`192.168.110.28:15353`).  While Docker is being upgraded or the container is
rebuilt, that upstream is briefly unavailable and AdGuard **falls back to public DNS** -- the DTU
learns the real vendor address, connects straight to the vendor cloud and keeps that connection.
When the bridge comes back it is not re-connected; only a DTU power cycle helps.  Symptom: **the
app works, local control / HA is dead**.

### Permanent fixes (recommended order)

1. **Block the DTU from the internet on the router (most complete).**
   In the router's device / access-control page deny internet access for `192.168.110.3`, keeping
   LAN access.  The DTU can then never reach the vendor: while the bridge is down it just keeps
   retrying and reconnects as soon as the bridge is back.  (The app keeps working, its traffic is
   relayed through the bridge anyway.)

2. **Move the DNS answer onto the AdGuard host (outside Docker, recommended).**
   On the always-on machine that runs AdGuard Home (here `192.168.110.92`, Armbian):

   ```bash
   # as root on the AdGuard host, after unpacking the deployment bundle
   sh deploy/install_host_dns.sh 192.168.110.28 5333
   # then set the AC-DTU client upstream in AdGuard Home to 127.0.0.1:5333
   ```

   The answer no longer depends on the NAS or Docker: during Docker upgrades, bridge rebuilds
   and even NAS reboots the DTU only ever resolves to the NAS IP, keeps retrying and reconnects
   as soon as the bridge is back.  (The service can also live on the NAS host; then use
   `192.168.110.28:5333` as the AdGuard upstream.)

3. **Enable Docker live-restore** (`/etc/docker/daemon.json`: `{"live-restore": true}`, then
   `systemctl restart docker`) so engine restarts/upgrades keep containers running.

> If the DTU already went straight to the vendor, apply one of the fixes above and power-cycle the
> heat pump (DTU) once.

### Optional offline notification

`binary_sensor.ao_yuan_120_2_ben_di_qiao_jie` turns `off` when the bridge is unreachable:

```yaml
automation:
  - alias: AC local bridge offline
    trigger:
      - platform: state
        entity_id: binary_sensor.ao_yuan_120_2_ben_di_qiao_jie
        to: "off"
        for: "00:05:00"
    action:
      - service: persistent_notification.create
        data:
          title: AC local bridge offline
          message: The bridge has been unreachable for 5 minutes; after a Docker upgrade the DTU may have fallen back to the vendor cloud -- power-cycle the heat pump when convenient.
```
