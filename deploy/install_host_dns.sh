#!/bin/sh
# Install the split-DNS responder OUTSIDE Docker on the NAS (run as root).
#
#   sh install_host_dns.sh [BRIDGE_IP]
#
# Default BRIDGE_IP is 192.168.110.28.  The responder answers
# sensihydro.copeland.cn / sensihydro.insinkerator.com.cn with that IP and
# forwards everything else to public resolvers, so the DTU can always find the
# bridge -- even while the bridge container is being rebuilt or Docker is being
# upgraded (AdGuard no longer has to fall back to the real vendor address).
set -e

BRIDGE_IP="${1:-192.168.110.28}"
DIR=/volume1/docker/sensihydro-dns
HERE=$(cd "$(dirname "$0")" && pwd)
PYTHON=$(command -v python3 || true)

if [ -z "$PYTHON" ]; then
    echo "python3 not found. Install it first (apt install python3) or run the responder inside a container." >&2
    exit 1
fi

mkdir -p "$DIR"
cp "$HERE/../tools/host_dns.py" "$DIR/host_dns.py"

cat > /etc/systemd/system/sensihydro-dns.service <<EOF
[Unit]
Description=SensiHydro split DNS for the HF5111B DTU
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=$PYTHON $DIR/host_dns.py --answer $BRIDGE_IP --port 53
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now sensihydro-dns
sleep 1
systemctl --no-pager --lines=5 status sensihydro-dns || true

echo
echo "Verify with:  dig @192.168.110.28 sensihydro.copeland.cn +short"
echo "Expected:     $BRIDGE_IP"
echo
echo "Then point the DTU's AdGuard client upstream at 192.168.110.28:53"
echo "(AdGuard -> Settings -> Client settings -> AC-DTU -> Upstream DNS servers)."
