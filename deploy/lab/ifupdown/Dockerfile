# Classic Debian stack: ifupdown with /etc/network/interfaces.
FROM lanvex-target:common
RUN apt-get install -y -qq ifupdown >/dev/null
COPY interfaces /etc/network/interfaces
RUN systemctl enable networking
