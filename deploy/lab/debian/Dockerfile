# Debian 12 server: ifupdown with /etc/network/interfaces (sourcing interfaces.d, as the installer
# writes it) and dnsmasq, installed and running as Debian's package leaves it (answering DNS on every
# interface). Built from Debian's own image, so it needs a container registry; build.sh builds it only
# with LANVEX_LAB_DEBIAN=1 (CI).
ARG DEBIAN_IMAGE=docker.io/library/debian:12
FROM ${DEBIAN_IMAGE}
ENV DEBIAN_FRONTEND=noninteractive
RUN printf 'Acquire::Retries "5";\n' > /etc/apt/apt.conf.d/80-lab-retries \
 && apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends systemd systemd-sysv dbus ifupdown iproute2 \
      openssh-server sudo python3 dnsmasq iputils-ping procps nftables >/dev/null \
 && rm -rf /var/lib/apt/lists/*
# lanvex: unprivileged account Lanvex logs in with (key auth). ops: password auth test account.
RUN useradd -m -s /bin/bash lanvex && useradd -m -s /bin/bash ops && echo 'ops:ops-lab-password-1' | chpasswd \
 && install -d -m 700 -o lanvex -g lanvex /home/lanvex/.ssh \
 && rm -f /etc/ssh/ssh_host_* \
 && printf 'PasswordAuthentication yes\nPermitRootLogin no\nKbdInteractiveAuthentication no\n' > /etc/ssh/sshd_config.d/lab.conf \
 && printf '[Unit]\nDescription=Generate SSH host keys\nBefore=ssh.service\nConditionPathExists=!/etc/ssh/ssh_host_ed25519_key\n[Service]\nType=oneshot\nExecStart=/usr/bin/ssh-keygen -A\n[Install]\nWantedBy=multi-user.target\n' > /etc/systemd/system/ssh-hostkeys.service
COPY interfaces /etc/network/interfaces
RUN mkdir -p /etc/network/interfaces.d \
 && systemctl enable networking ssh ssh-hostkeys.service dnsmasq \
 && systemctl mask getty@tty1.service console-getty.service serial-getty@console.service
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
