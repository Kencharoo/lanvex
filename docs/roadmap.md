# Roadmap: Lanvex as the router's console

Last updated 2026-11-18.

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
| **VLANs and DHCP first**, then access rules, then DNS filtering and taking over a running setup: 1.0 is the version that replaces Netzverwaltung | The first two are needed for any new network; access rules build on the zones they create. Kea waits until after 1.0: Netzverwaltung uses dnsmasq, so Kea does not stand between Lanvex and replacing it |
| **Every common server stack, one after another, each tested on a real host in CI.** NetworkManager with firewalld (Fedora, RHEL; 0.7), then netplan (Ubuntu Server) and ifupdown (Debian) with ufw or no firewall (0.8); access rules on firewalld, ufw and nftables alike (0.9); Kea next to dnsmasq after 1.0 | The model is the same everywhere; only the files written and the commands run differ. A stack counts as supported once a real DHCP client gets its address through a new VLAN on it in the lab |
| **Lanvex writes its own files and never edits another tool's.** `/etc/dnsmasq.d/lanvex.conf`, NetworkManager profiles named `lanvex-…`, `/etc/netplan/60-lanvex.yaml`, `/etc/network/interfaces.d/lanvex`, firewalld zones `lx-…`, ufw rules marked `lanvex:` | The same rule Netzverwaltung follows with `nv-`. A conflict (another file serving the same interface) is shown and refused, never overwritten |

## What a server router needs (Netzverwaltung's features as the checklist)

| Netzverwaltung | Lanvex | Stage |
|---|---|---|
| VLAN interfaces on the trunk, with the server's address | VLAN interfaces documented in Lanvex Core (a virtual interface with a parent and a VLAN, its address tagged `lanvex-gateway`), created, changed and removed on the server | 0.7 |
| DHCP per VLAN: pool, lease time, router and DNS options, reservations | Pool = an IP range tagged `lanvex-dhcp`; reservation = an IP address with a MAC address; router and DNS = the server's address in that network | 0.7 |
| Devices: active leases, one click to a fixed address | Leases read from the server, shown per network and in Discovery; one click records a fixed address in Lanvex Core | 0.7 |
| Zone per VLAN: which services on the server it may use | Per network: DHCP and DNS, and the ports allowed on the server (firewalld zone, ufw rules or Lanvex's table) | 0.7 / 0.9 |
| Access matrix: Internet, main LAN, switch interface, VLAN to VLAN | Access rules per network: the Internet, other networks, destinations with ports, services on the server; as firewalld policies, ufw rules or Lanvex's own nftables table | 0.9 |
| DNS filter per VLAN | Blocked domains and exceptions per network, with a dnsmasq of its own | 1.0 |
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

### 0.9: Access rules (released 2026-11-16)

What devices on each Lanvex network may reach, documented in Lanvex Core with the network and brought
to the server in the same approved change as its VLAN and DHCP:

- **The Internet**: forwarded and masqueraded out of the server's uplink; private networks stay closed
  unless allowed below.
- **Other networks and destinations**: another Lanvex network, a documented prefix (the main LAN), or
  one address with ports (a switch's web interface). Traffic leaving towards a network the server does
  not route for is masqueraded, so answers come back through the server.
- **Services on the server** beyond DHCP and DNS (SSH, a web interface, any port).
- Shown as a matrix like Netzverwaltung's, one row per network, editable on the server page.

Stored as a custom field on the network's interface in Lanvex Core (`lanvex_access`), so the
documentation still says everything. On the server, by whatever firewall runs there:

| Firewall | Lanvex writes |
|---|---|
| firewalld (Fedora, RHEL, also elsewhere) | The network's zone lists the services; policies `lx…` from its zone to each zone it may reach, with masquerade towards the uplink |
| ufw (Ubuntu) | Rules marked `lanvex:<interface>`, also `ufw route` rules for forwarding; masquerading in Lanvex's own nftables table, since ufw has no command for it |
| None, or hand-written nftables (Debian) | Lanvex's own table `inet lanvex`: input and forwarding for its networks only, and masquerading; loaded at boot by its own unit. A hand-written ruleset that drops forwarded traffic is pointed out |

IP forwarding is switched on in Lanvex's own sysctl file. A network that carries Lanvex's connection
keeps SSH to the server. **Done when** the lab shows, on Fedora, Ubuntu and Debian, a real client on a
Lanvex network reaching exactly what its rules allow (a stand-in for the Internet, a host on the main
LAN on one port, another Lanvex network, a service on the server) and nothing else, with rollback.
Done: on Fedora (firewalld), Ubuntu (Lanvex's table, and ufw) and Debian, on every push.

### 1.0: Replacing Netzverwaltung (released 2026-11-16)

- **DNS filter** per network: your own blocked domains and exceptions. As in Netzverwaltung, a dnsmasq
  of its own for each filtered network, which DHCP hands to its devices and which passes everything
  it does not block to the main one, so local names keep working.
- **Taking over a running setup**: Netzverwaltung's VLANs, DHCP pools, fixed addresses, access rules
  and DNS filters are imported into Lanvex Core from its `netz.yaml` and policies; then one change
  replaces its files with Lanvex's, stops it, and gives it back exactly as it was on rollback. Files
  other tools keep by hand stay as they are. Each VLAN is down for a moment while its new profile comes
  up; devices keep their addresses.
- **On KlassenServer**: a Lanvex network next to Netzverwaltung first, then the takeover.

**Done when** KlassenServer runs on Lanvex instead of Netzverwaltung, with every VLAN, pool, fixed
address, access rule and DNS filter it had. Built and verified in the lab against a stand-in of
Netzverwaltung on Fedora; the last step is the takeover on KlassenServer itself.

Not taken over: Netzverwaltung's block lists by name (its model has them, its interface does not; the
import report names any it finds, so their domains can be added).

### 1.1: Monitoring and lanvexctl (released 2026-11-17)

Alerts for unreachable servers, refused reads, drift from the documentation, full DHCP pools,
unknown devices and failed changes, by email and webhooks ([monitoring.md](monitoring.md)); lanvexctl to
install, update, back up and restore.

### 1.2: Switches (released 2026-11-17)

Built: switches read over SNMP (v3 or v2c, read-only), with their ports, VLANs, link aggregations,
MAC address table and LLDP neighbours; which device is on which port, compared with the documented
cables in Discovery, on the switch page and in the topology; cables recorded after a preview;
unreachable and failed-read alerts ([switches.md](switches.md)). Release files are built and attached
to each GitHub release by CI.

### 1.3: Switch changes and more server changes (released 2026-11-18)

Built: alerts for switch ports and devices that move or appear, makers from MAC addresses; a switch
port's VLANs set as an approved change with preview, read back and undo ([switches.md](switches.md));
IPv6 and static routes in server changes, bridges and bonds ([changes.md](changes.md)); Kea as the DHCP
server where it runs ([networks.md](networks.md#kea-instead-of-dnsmasq)). Each with lab tests in CI.

### Next

- **Sign-in with LDAP, Active Directory or OpenID Connect.**
- **The manual** (German and English), from the docs as they are now.
- **On real hardware**: port VLANs on real switches, bridges and bonds on physical servers, Kea outside
  the lab.
