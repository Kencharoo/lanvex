# NetworkManager-managed host, the default on Fedora Server. Two NICs.
FROM lanvex-target:common
RUN apt-get install -y -qq network-manager >/dev/null
RUN printf '[main]\nplugins=keyfile\n[keyfile]\nunmanaged-devices=none\n' > /etc/NetworkManager/conf.d/90-lab.conf \
 && : > /etc/NetworkManager/conf.d/10-globally-managed-devices.conf
COPY eth0.nmconnection eth1.nmconnection /etc/NetworkManager/system-connections/
RUN chmod 600 /etc/NetworkManager/system-connections/*.nmconnection \
 && systemctl enable NetworkManager && systemctl disable systemd-networkd 2>/dev/null || true
