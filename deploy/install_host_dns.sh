#!/bin/sh
# Install the split-DNS responder OUTSIDE Docker (run as root on the target host).
#
#   sh install_host_dns.sh [BRIDGE_IP] [PORT] [INSTALL_DIR]
#
# Defaults: BRIDGE_IP=192.168.110.28, PORT=5333, INSTALL_DIR=/opt/sensihydro-dns
#
# Recommended target: the host that already runs AdGuard Home (it must be
# always on and it is where the DTU's DNS lookups are decided).  The responder
# answers the vendor domains with BRIDGE_IP and forwards everything else, so
# AdGuard can now use 127.0.0.1:PORT as the AC-DTU client upstream: the DTU
# always resolves to the bridge, even while the bridge container is being
# rebuilt or Docker is being upgraded, and it reconnects automatically.
#
# Alternative target: the NAS host itself (outside Docker) -- then use
# 192.168.110.28:PORT in AdGuard instead of 127.0.0.1:PORT.
set -e

BRIDGE_IP="${1:-192.168.110.28}"
PORT="${2:-5333}"
DIR="${3:-/opt/sensihydro-dns}"
HERE=$(cd "$(dirname "$0")" && pwd)
PYTHON=$(command -v python3 || true)

if [ -z "$PYTHON" ]; then
    echo "python3 not found -- install it first (apt install python3)." >&2
    exit 1
fi

mkdir -p "$DIR"
cp "$HERE/../tools/host_dns.py" "$DIR/host_dns.py"

cat > /etc/systemd/system/sensihydro-dns.service <<EOF
[Unit]
Description=SensiHydro split DNS for the HF5111B DTU (bridge redirect)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$PYTHON $DIR/host_dns.py --answer $BRIDGE_IP --port $PORT --listen 0.0.0.0
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
echo "Verify:   python3 - <<'PY'"
echo "  ... or: dig @127.0.0.1 -p $PORT sensihydro.copeland.cn +short   (expect $BRIDGE_IP)"
echo "PY"
echo
echo "Then set the AC-DTU client upstream in AdGuard Home to 127.0.0.1:$PORT"
echo "(AdGuard Home -> Settings -> Client settings -> AC-DTU -> Upstream DNS servers)."
