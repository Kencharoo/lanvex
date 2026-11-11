#!/bin/sh
# Start the Lanvex lab targets: three Ubuntu 24.04 hosts, each running a different network stack
# under systemd, reachable over SSH from the host. Requires the images from build.sh.
set -eu

run() { # name image ip
  docker rm -f "$1" >/dev/null 2>&1 || true
  docker run -d --name "$1" --hostname "$1" --privileged \
    --network lanvex-lab --ip "$3" \
    --tmpfs /run --tmpfs /run/lock -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
    "$2" >/dev/null
}

docker network inspect lanvex-lab >/dev/null 2>&1 || \
  docker network create --driver bridge --subnet 172.30.0.0/24 --gateway 172.30.0.1 lanvex-lab >/dev/null
docker network inspect lanvex-lab-b >/dev/null 2>&1 || \
  docker network create --driver bridge --subnet 172.31.0.0/24 --gateway 172.31.0.1 lanvex-lab-b >/dev/null

run lab-netplan lanvex-target:netplan-networkd 172.30.0.11
run lab-nm      lanvex-target:networkmanager   172.30.0.12
run lab-ifup    lanvex-target:ifupdown         172.30.0.13
docker network connect --ip 172.31.0.12 lanvex-lab-b lab-nm

# Wait for systemd to finish booting.
for c in lab-netplan lab-nm lab-ifup; do
  i=0
  until state=$(docker exec "$c" systemctl is-system-running 2>/dev/null) && [ "$state" = running -o "$state" = degraded ]; do
    i=$((i + 1)); [ $i -gt 60 ] && { echo "$c did not boot: $state"; exit 1; }; sleep 1
  done
  # Occasionally the socket unit is skipped on first boot in a container; starting it is idempotent.
  docker exec "$c" systemctl start ssh-hostkeys.service ssh.socket
  echo "$c: $state"
done
