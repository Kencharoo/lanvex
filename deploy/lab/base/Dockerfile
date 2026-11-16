# The lab's Ubuntu 24.04 base from Ubuntu's own image, where a registry is reachable (CI): the same
# packages debootstrap includes in build.sh, with apt retrying downloads a mirror drops.
ARG BASE=docker.io/library/ubuntu:24.04
FROM ${BASE}
ARG MIRROR=http://archive.ubuntu.com/ubuntu
ENV DEBIAN_FRONTEND=noninteractive
RUN printf 'Acquire::Retries "5";\n' > /etc/apt/apt.conf.d/80-lab-retries \
 && printf 'Types: deb\nURIs: %s\nSuites: noble noble-updates\nComponents: main universe\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n' "$MIRROR" \
      > /etc/apt/sources.list.d/ubuntu.sources \
 && apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends systemd systemd-sysv dbus openssh-server iproute2 \
      ca-certificates sudo less iputils-ping >/dev/null \
 && rm -f /etc/apt/sources.list.d/ubuntu.sources && rm -rf /var/lib/apt/lists/*
