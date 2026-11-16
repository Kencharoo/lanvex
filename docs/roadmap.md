# Roadmap: Lanvex as the router's console

Last updated 2026-11-16.

**Goal:** Lanvex does everything Netzverwaltung does on KlassenServer (VLANs, DHCP, who may reach what,
DNS filtering) in a general form: for any server Lanvex manages, on more Linux systems than one
Fedora machine, from the network documentation in Lanvex Core, and with the same safety as every other
Lanvex change (preview, approval, automatic rollback). Netzverwaltung stays in charge on KlassenServer
until Lanvex can take over its whole setup.

## Decisions

| Decision | Why |
|---|---|
| **The documentation drives the configuration.** A network is defined in Lanvex Core (VLAN, prefix, the server's interface and address, DHCP pool, fixed addresses); Lanvex compares that with the server and prepares the change that makes the server match | Lanvex's rule from the start: Lanvex Core says what should be, the server says what is, and a change is what is pending. Nothing is configured that is not documented |
| **Changes to the server Lanvex runs on are confirmed by the administrator's browser.** The server keeps a change only if the browser still reaches Lanvex over that server's own network before the deadline; otherwise its timer restores the previous configuration | Lanvex in a container on the same machine reaches it only through loopback, which proves nothing. This is how Netzverwaltung confirms its changes too |
| **VLANs and DHCP first**, then access rules, then DNS filtering | The first two are needed for any new network; access rules build on the zones they create |
| **Fedora first** (NetworkManager, firewalld, dnsmasq), then Debian/Ubuntu with nftables, Ubuntu with ufw, and Kea instead of dnsmasq | KlassenServer runs Fedora. The others share the model; only the files written differ |
| **Lanvex writes its own files and never edits another tool's.** `/etc/dnsmasq.d/lanvex.conf`, NetworkManager profiles named `lanvex-…`, firewalld zones and policies named `lanvex-…` | The same rule Netzverwaltung follows with `nv-`. A conflict (another file serving the same interface) is shown and refused, never overwritten |

## How Netzverwaltung's features map

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

### 0.7: VLANs and DHCP (in progress)

Built: browser confirmation; helper version 2 with VLAN interfaces, firewalld zones and dnsmasq; the
custom fields and permissions in Lanvex Core; the server page's *Networks* section with fixed addresses
and leases; leases in Discovery ([networks.md](networks.md)).

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

**Done when** a new VLAN network with DHCP can be documented in Lanvex and brought to a Fedora server
in one approved change, a client on that VLAN gets an address, and a broken change is rolled back.

### 0.8: netplan and Debian/Ubuntu

- VLAN interfaces with netplan (systemd-networkd and NetworkManager renderers).
- dnsmasq on Debian/Ubuntu. Kea as an alternative DHCP server.

### 0.9: Access rules

- Per network: the Internet, other networks, named destinations (a switch's web interface), and the
  services on the server itself. Shown as a matrix like Netzverwaltung's.
- firewalld policies first, then nftables (Debian/Ubuntu) and ufw.
- Lanvex's own connection and the administrator's network can never be locked out: a rule that would
  do so is refused before it is prepared.

### 1.0: DNS filtering and the switch-over

- Block lists per network.
- Import of an existing setup, including Netzverwaltung's `netz.yaml` and its `nv-` files.
- KlassenServer moves from Netzverwaltung to Lanvex; Netzverwaltung is retired.

## Alongside

- **Monitoring and alerts**: unknown devices getting a lease, a network's DHCP pool running full, a
  server's network stopping to match the documentation, a server becoming unreachable.
- The manual (German and English) catches up with each stage.
