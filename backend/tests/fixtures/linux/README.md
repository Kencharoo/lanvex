# Captured command output

Real output of every command in `lanvex/integrations/linux/commands.py`, captured by running them as
the unprivileged `lanvex` user on the lab targets in `deploy/lab` (Ubuntu 24.04, systemd as PID 1):

| Directory | Network stack |
|---|---|
| `netplan/` | netplan → systemd-networkd, systemd-resolved |
| `nm/` | NetworkManager, two NICs (`eth1` was brought up before NetworkManager saw it) |
| `ifup/` | ifupdown with `/etc/network/interfaces` |

`<command>.stdout` is the output; `_meta.json` has exit statuses and stderr. Regenerate with the lab
running: see `deploy/lab/README.md`.
