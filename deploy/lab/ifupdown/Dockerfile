# Classic Debian stack: ifupdown with /etc/network/interfaces.
FROM lanvex-target:common
# lldpd announces this host and records its neighbours; lanvex may ask it, as an
# administrator sets up per docs/operations.md (the group that may run lldpcli: adm on Ubuntu).
RUN apt-get install -y -qq ifupdown lldpd >/dev/null && usermod -aG "$(stat -c %G /usr/sbin/lldpcli)" lanvex
COPY interfaces /etc/network/interfaces
RUN systemctl enable networking
