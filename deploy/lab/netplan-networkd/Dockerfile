# Ubuntu Server default stack: netplan rendering to systemd-networkd, DNS via systemd-resolved.
FROM lanvex-target:common
RUN apt-get install -y -qq netplan.io systemd-resolved >/dev/null
COPY 50-lab.yaml /etc/netplan/50-lab.yaml
RUN chmod 600 /etc/netplan/50-lab.yaml && systemctl enable systemd-networkd systemd-resolved
