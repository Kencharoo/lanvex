# NetworkManager-managed host, the default on Fedora Server. Two NICs.
FROM lanvex-target:common
# lldpd announces this host to its neighbours (LLDP). lanvex is not in _lldpd here, so Lanvex reads
# LLDP on this host only as far as it is allowed to: the warning path.
RUN apt-get install -y -qq network-manager lldpd >/dev/null
RUN printf '[main]\nplugins=keyfile\n[keyfile]\nunmanaged-devices=none\n' > /etc/NetworkManager/conf.d/90-lab.conf \
 && : > /etc/NetworkManager/conf.d/10-globally-managed-devices.conf
# NetworkManager ignores keyfiles not owned by root; set the owner explicitly whatever the builder does.
COPY --chown=0:0 eth0.nmconnection eth1.nmconnection /etc/NetworkManager/system-connections/
RUN chown root:root /etc/NetworkManager/system-connections/*.nmconnection \
 && chmod 600 /etc/NetworkManager/system-connections/*.nmconnection \
 && systemctl enable NetworkManager && systemctl disable systemd-networkd 2>/dev/null || true
