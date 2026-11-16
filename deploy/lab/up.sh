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

# Linux bridges drop LLDP frames (01:80:c2:00:00:0e) unless told to forward them; the hosts announce
# themselves to each other with LLDP, as servers on a switch would.
echo 0x4000 > "/sys/class/net/br-$(docker network inspect -f '{{.Id}}' lanvex-lab | cut -c1-12)/bridge/group_fwd_mask"

run lab-netplan lanvex-target:netplan-networkd 172.30.0.11
run lab-nm      lanvex-target:networkmanager   172.30.0.12
run lab-ifup    lanvex-target:ifupdown         172.30.0.13
docker network connect --ip 172.31.0.12 lanvex-lab-b lab-nm
hosts="lab-netplan lab-nm lab-ifup"
if docker image inspect lanvex-target:fedora >/dev/null 2>&1; then
  run lab-fedora lanvex-target:fedora 172.30.0.14
  hosts="$hosts lab-fedora"
fi

# Wait for systemd to finish booting.
for c in $hosts; do
  i=0
  # is-system-running exits non-zero when degraded; that state still counts as booted.
  until state=$(docker exec "$c" systemctl is-system-running 2>/dev/null || true) && { [ "$state" = running ] || [ "$state" = degraded ]; }; do
    i=$((i + 1))
    if [ $i -gt 90 ] || [ "$(docker inspect -f '{{.State.Running}}' "$c")" != true ]; then
      echo "$c did not boot: ${state:-container stopped}"; docker logs --tail 40 "$c" 2>&1; exit 1
    fi
    sleep 1
  done
  # Occasionally the socket unit is skipped on first boot in a container; starting it is idempotent.
  if [ "$c" = lab-fedora ]; then
    docker exec "$c" systemctl start sshd.service
  else
    docker exec "$c" systemctl start ssh-hostkeys.service ssh.socket
  fi
  echo "$c: $state"
done
