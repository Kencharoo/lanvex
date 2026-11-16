#!/bin/sh
# Build the Lanvex lab target images from an Ubuntu 24.04 rootfs. Needs root, debootstrap, Docker,
# and HTTP access to an Ubuntu mirror (LANVEX_LAB_MIRROR, archive.ubuntu.com by default). No container
# registry is used.
set -eu
cd "$(dirname "$0")"
ROOT=${LANVEX_LAB_ROOTFS:-/srv/lanvex-lab/rootfs}
MIRROR=${LANVEX_LAB_MIRROR:-http://archive.ubuntu.com/ubuntu}

if ! docker image inspect lanvex-target:base >/dev/null 2>&1; then
  if [ ! -x "$ROOT/sbin/init" ]; then
    mkdir -p "$(dirname "$ROOT")"
    debootstrap --variant=minbase \
      --include=systemd,systemd-sysv,dbus,openssh-server,iproute2,ca-certificates,sudo,less,iputils-ping \
      noble "$ROOT" "$MIRROR"
  fi
  tar -C "$ROOT" -c . | docker import - lanvex-target:base
fi

docker build --network host --build-arg MIRROR="$MIRROR" -t lanvex-target:common common
for v in netplan-networkd networkmanager ifupdown; do
  docker build --network host -t "lanvex-target:$v" "$v"
done
echo "Built lanvex-target:{netplan-networkd,networkmanager,ifupdown}. Start them with ./up.sh"
