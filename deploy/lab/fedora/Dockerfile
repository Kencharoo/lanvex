# Fedora Server's network stack, like KlassenServer: NetworkManager, firewalld (zone FedoraServer) and
# dnsmasq, installed but not enabled (Lanvex enables it with the first DHCP pool). Built from Fedora's
# own image, so it needs a container registry; build.sh builds it only with LANVEX_LAB_FEDORA=1 (CI).
ARG FEDORA_IMAGE=docker.io/library/fedora:44
FROM ${FEDORA_IMAGE}
RUN dnf -y install --setopt=install_weak_deps=False systemd NetworkManager firewalld dnsmasq python3-pyyaml \
      openssh-server sudo iproute iputils python3 procps-ng hostname \
 && dnf clean all
# lanvex: unprivileged account Lanvex logs in with (key auth). ops: password auth test account.
RUN useradd -m -s /bin/bash lanvex && useradd -m -s /bin/bash ops && echo 'ops:ops-lab-password-1' | chpasswd \
 && install -d -m 700 -o lanvex -g lanvex /home/lanvex/.ssh \
 && rm -f /etc/ssh/ssh_host_* \
 && printf 'PasswordAuthentication yes\nPermitRootLogin no\nKbdInteractiveAuthentication no\n' > /etc/ssh/sshd_config.d/50-lab.conf
COPY --chown=0:0 eth0.nmconnection /etc/NetworkManager/system-connections/
RUN chown root:root /etc/NetworkManager/system-connections/eth0.nmconnection \
 && chmod 600 /etc/NetworkManager/system-connections/eth0.nmconnection \
 && printf '[main]\nplugins=keyfile\n' > /etc/NetworkManager/conf.d/90-lab.conf \
 && firewall-offline-cmd --set-default-zone=FedoraServer \
 && systemctl enable NetworkManager firewalld sshd \
 && systemctl mask getty@tty1.service console-getty.service serial-getty@console.service \
 && systemctl disable dnsmasq 2>/dev/null; true
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
