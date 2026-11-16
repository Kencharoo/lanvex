# Ubuntu Server default stack: netplan rendering to systemd-networkd, DNS via systemd-resolved.
# dnsmasq and ufw as Ubuntu leaves them once installed: dnsmasq enabled but failing at boot (systemd-
# resolved holds port 53), ufw inactive.
FROM lanvex-target:common
RUN apt-get install -y -qq netplan.io systemd-resolved dnsmasq ufw >/dev/null
COPY 50-lab.yaml /etc/netplan/50-lab.yaml
RUN chmod 600 /etc/netplan/50-lab.yaml && systemctl enable systemd-networkd systemd-resolved
