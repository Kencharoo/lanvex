# Lanvex lab target, shared layer. Built FROM an Ubuntu 24.04 rootfs imported as lanvex-target:base
# (see deploy/lab/README.md). Boots systemd as PID 1.
FROM lanvex-target:base
ENV DEBIAN_FRONTEND=noninteractive
ARG MIRROR=http://archive.ubuntu.com/ubuntu
RUN printf 'Acquire::Retries "5";\n' > /etc/apt/apt.conf.d/80-lab-retries \
 && printf 'deb %s noble main universe\ndeb %s noble-updates main universe\n' "$MIRROR" "$MIRROR" > /etc/apt/sources.list \
 && apt-get update -qq
# lanvex: unprivileged account Lanvex logs in with (key auth). ops: password auth test account.
RUN useradd -m -s /bin/bash lanvex && useradd -m -s /bin/bash ops && echo 'ops:ops-lab-password-1' | chpasswd \
 && install -d -m 700 -o lanvex -g lanvex /home/lanvex/.ssh
# Unique host keys per container, generated at first boot.
RUN rm -f /etc/ssh/ssh_host_* \
 && printf '[Unit]\nDescription=Generate SSH host keys\nBefore=ssh.service ssh.socket\nConditionPathExists=!/etc/ssh/ssh_host_ed25519_key\n[Service]\nType=oneshot\nExecStart=/usr/bin/ssh-keygen -A\n[Install]\nWantedBy=sockets.target\n' > /etc/systemd/system/ssh-hostkeys.service \
 && systemctl enable ssh-hostkeys.service ssh.socket \
 && printf 'PasswordAuthentication yes\nPermitRootLogin no\nKbdInteractiveAuthentication no\n' > /etc/ssh/sshd_config.d/lab.conf
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
