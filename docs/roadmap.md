# Roadmap: Lanvex as the router's console

Last updated 2026-11-16.

**Goal:** any Linux server Lanvex manages can be the router for its networks: VLANs, DHCP, who may
reach what, DNS filtering. On the server systems people actually run (Ubuntu Server, Debian, Fedora,
RHEL, whatever manages their network), from the network documentation in Lanvex Core, and with the same
safety as every other Lanvex change (preview, approval, automatic rollback).

Netzverwaltung, a tool written for one classroom server (KlassenServer), does this for exactly that
machine and network. It is the yardstick for what a server needs, and KlassenServer is one test case,
not the target: nothing in Lanvex is specific to it.

## Decisions

| Decision | Why |
|---|---|
| **The documentation drives the configuration.** A network is defined in Lanvex Core (VLAN, prefix, the server's interface and address, DHCP pool, fixed addresses); Lanvex compares that with the server and prepares the change that makes the server match | Lanvex's rule from the start: Lanvex Core says what should be, the server says what is, and a change is what is pending. Nothing is configured that is not documented |
| **Changes to the server Lanvex runs on are confirmed by the administrator's browser.** The server keeps a change only if the browser still reaches Lanvex over that server's own network before the deadline; otherwise its timer restores the previous configuration | Lanvex in a container on the same machine reaches it only through loopback, which proves nothing. This is how Netzverwaltung confirms its changes too |
| **VLANs and DHCP first**, then access rules, then DNS filtering | The first two are needed for any new network; access rules build on the zones they create |
| **Every common server stack, one after another, each tested on a real host in CI.** NetworkManager with firewalld (Fedora, RHEL; 0.7), then netplan (Ubuntu Server) and ifupdown (Debian) with ufw or no firewall (0.8), then Kea next to dnsmasq | The model is the same everywhere; only the files written and the commands run differ. A stack counts as supported once a real DHCP client gets its address through a new VLAN on it in the lab |
| **Lanvex writes its own files and never edits another tool's.** `/etc/dnsmasq.d/lanvex.conf`, NetworkManager profiles named `lanvex-…`, `/etc/netplan/60-lanvex.yaml`, `/etc/network/interfaces.d/lanvex`, firewalld zones `lx-…`, ufw rules marked `lanvex:` | The same rule Netzverwaltung follows with `nv-`. A conflict (another file serving the same interface) is shown and refused, never overwritten |

## What a server router needs (Netzverwaltung's features as the checklist)

| Netzverwaltung | Lanvex | Stage |
|---|---|---|
| VLAN interfaces on the trunk, with the server's address | VLAN interfaces documented in Lanvex Core (a virtual interface with a parent and a VLAN, its address tagged `lanvex-gateway`), created, changed and removed on the server | 0.7 |
| DHCP per VLAN: pool, lease time, router and DNS options, reservations | Pool = an IP range tagged `lanvex-dhcp`; reservation = an IP address with a MAC address; router and DNS = the server's address in that network | 0.7 |
| Devices: active leases, one click to a fixed address | Leases read from the server, shown per network and in Discovery; one click records a fixed address in Lanvex Core | 0.7 |
| Zone per VLAN: which services on the server it may use | A firewalld zone per network; in 0.7 only what DHCP and DNS need, the rest in 0.9 | 0.7 / 0.9 |
| Access matrix: Internet, main LAN, switch interface, VLAN to VLAN | Access rules between networks, the Internet and named destinations, as firewalld policies (later nftables) | 0.9 |
| DNS filter per VLAN | Block lists per network | 1.0 |
| "Take over existing setup" | Import a server's existing VLANs, DHCP and firewall setup into Lanvex Core, including Netzverwaltung's `netz.yaml` | 1.0 |
| Diff, 120 s confirmation, rollback unit, backups | Lanvex's change pipeline; one change can now touch several files and services | 0.7 |

## Stages

### 0.7: VLANs and DHCP (released 2026-11-16)

Built: browser confirmation; helper version 2 with VLAN interfaces, firewalld zones and dnsmasq; the
custom fields and permissions in Lanvex Core; the server page's *Networks* section with fixed addresses
and leases; leases in Discovery ([networks.md](networks.md)). On a Fedora 44 host in CI, a real DHCP
client gets its fixed address through a new VLAN, and removals, rollbacks and the server's own timer
work; what is not verified yet is in [status.md](status.md).

- Browser confirmation for changes to the server Lanvex runs on (or reaches only indirectly).
- The helper (version 2) creates, changes and removes VLAN interfaces as NetworkManager profiles,
  writes `/etc/dnsmasq.d/lanvex.conf`, and gives each Lanvex network a firewalld zone that allows
  DHCP and DNS. One change brings the server in line with Lanvex Core for all of its networks.
- Lanvex Core: a MAC address field on IP addresses, the `lanvex-dhcp` tag, and the permissions
  the service account needs for interfaces, VLANs and IP ranges.
- The server page shows its networks and DHCP pools next to what Lanvex Core documents, and prepares
  the change. The network page edits the pool and fixed addresses.
- Leases on the server page, in Discovery, and one click to a fixed address.
- Tests: a Fedora host in the CI lab (NetworkManager, firewalld, dnsmasq) with a real VLAN and real
  DHCP clients.

**Done when** a new VLAN network with DHCP can be documented in Lanvex and brought to a NetworkManager
server in one approved change, a client on that VLAN gets an address, and a broken change is rolled back.

### 0.8: Ubuntu Server and Debian (released 2026-11-16)

Built: helper version 3 creates VLAN interfaces with netplan (`/etc/netplan/60-lanvex.yaml`, Ubuntu
Server) and ifupdown (`/etc/network/interfaces.d/lanvex`, Debian) as well as NetworkManager, and lists
them in `/etc/lanvex/networks.json`; dnsmasq runs next to systemd-resolved and goes back to how it
ran once the last pool is removed; ufw rules for DHCP and DNS, marked `lanvex:<interface>`. In CI the
0.7 test (a real DHCP client on a new VLAN, removal, rollback, the server's own timer, another tool's
configuration refused) runs on Fedora 44, Ubuntu 24.04 and Debian 12, and through an active ufw.

**Next in this line: Kea** as an alternative DHCP server, for servers that already run it.

### 0.9: Access rules

- Per network: the Internet, other networks, named destinations (a switch's web interface), and the
  services on the server itself. Shown as a matrix like Netzverwaltung's.
- firewalld policies first, then nftables (Debian/Ubuntu) and ufw.
- Lanvex's own connection and the administrator's network can never be locked out: a rule that would
  do so is refused before it is prepared.

### 1.0: DNS filtering and taking over existing setups

- Block lists per network.
- Import of an existing setup into Lanvex Core: VLAN interfaces, DHCP configuration and firewall rules
  a server already has, written by hand or by another tool (Netzverwaltung's `netz.yaml` and `nv-` files
  among them), so Lanvex can take over a running router without rebuilding it.

## Alongside

- **Monitoring and alerts**: unknown devices getting a lease, a network's DHCP pool running full, a
  server's network stopping to match the documentation, a server becoming unreachable.
- The manual (German and English) catches up with each stage.
