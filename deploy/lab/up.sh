#!/bin/sh
# Start the Lanvex lab targets: three Ubuntu 24.04 hosts, each running a different network stack
# under systemd, reachable over SSH from the host. Requires the images from build.sh.
set -eu

# systemd as PID 1 needs a writable cgroup tree. With cgroup v2 (current distributions, GitHub's
# runners) a privileged container in its own cgroup namespace gets one; mounting the host's tree
# into it makes systemd exit. With cgroup v1 the host's tree is mounted, as before.
if [ "$(stat -fc %T /sys/fs/cgroup)" = cgroup2fs ]; then
  CGROUP="--cgroupns=private"
else
  CGROUP="-v /sys/fs/cgroup:/sys/fs/cgroup:rw"
fi

run() { # name image ip
  docker rm -f "$1" >/dev/null 2>&1 || true
  # shellcheck disable=SC2086  # CGROUP is one or two arguments
  docker run -d --name "$1" --hostname "$1" --privileged $CGROUP \
    --network lanvex-lab --ip "$3" \
    --tmpfs /run --tmpfs /run/lock \
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
  until state=$(docker exec "$c" systemctl is-system-running 2>/dev/null) && { [ "$state" = running ] || [ "$state" = degraded ]; }; do
    i=$((i + 1))
    if [ $i -gt 90 ] || [ "$(docker inspect -f '{{.State.Running}}' "$c")" != true ]; then
      echo "$c did not boot: ${state:-container stopped}"; docker logs --tail 40 "$c" 2>&1; exit 1
    fi
    sleep 1
  done
  # Occasionally the socket unit is skipped on first boot in a container; starting it is idempotent.
  docker exec "$c" systemctl start ssh-hostkeys.service ssh.socket
  echo "$c: $state"
done
