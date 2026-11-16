# Changelog

## 0.8.0 — 2026-11-16

Networks on the other common server systems, not only NetworkManager ([docs/networks.md](docs/networks.md)).

- **Ubuntu Server and Debian.** VLAN networks with DHCP on servers whose network netplan (with
  systemd-networkd) or ifupdown configures: Lanvex writes `/etc/netplan/60-lanvex.yaml` or
  `/etc/network/interfaces.d/lanvex`, linked to the trunk's own definition, and removes the VLAN
  devices those tools leave behind. NetworkManager servers work as before. Each kind is tested on a
  real host in CI with a real DHCP client on the new VLAN (Fedora 44, Ubuntu 24.04, Debian 12).
- **dnsmasq next to systemd-resolved.** Where dnsmasq does not run yet and nothing else chooses its
  interfaces, Lanvex's file binds only the interfaces it serves, so dnsmasq starts on Ubuntu, where
  systemd-resolved holds port 53. Where dnsmasq already answers on every interface (Debian's default),
  Lanvex adds its pools without restricting that. When the last pool is removed, dnsmasq goes back
  to how it ran before Lanvex's first pool.
- **ufw.** Where ufw is active, DHCP and DNS are allowed on each network Lanvex serves, with rules
  marked `lanvex:<interface>` that leave with the network; a rollback restores ufw's rules. A
  hand-written nftables firewall that drops incoming traffic is pointed out in the preview.
- **Helper version 3.** Lists the VLANs it manages in `/etc/lanvex/networks.json`, which every read
  of a server now includes (with Lanvex's DHCP configuration and leases, on every system). Interface
  settings are not offered for Lanvex's own VLANs, which change as networks.
- ifupdown servers can be prepared for changes (networks only); changing interface settings stays
  limited to netplan and NetworkManager.
- Lab: a Debian 12 host (CI); the Ubuntu host has dnsmasq and ufw installed as Ubuntu leaves them.

**Updating:** every server's change helper shows as outdated (version 3); install it again from the
server page.

## 0.7.0 — 2026-11-16

The first step towards doing what Netzverwaltung does on KlassenServer, in general form
([docs/roadmap.md](docs/roadmap.md)): VLAN networks with DHCP on a Linux server, from the
documentation.

- **Networks on a server** ([docs/networks.md](docs/networks.md)). A server page's new *Networks*
  section lists the VLAN networks Lanvex Core documents for the server (a VLAN interface of its device
  with an address, a DHCP pool as an IP range tagged `lanvex-dhcp`, fixed addresses as IP addresses
  with a MAC address) next to what the server has, and prepares the one change that brings the server
  in line. Add a network, edit its DHCP pool, add fixed addresses (also from a lease with one click),
  or remove it; Lanvex Core is updated at once, the server only through the approved change.
- **Helper version 2** creates, changes and removes VLAN interfaces as NetworkManager profiles
  (`lanvex-*`), gives each a firewalld zone (`lx-*`) that allows DHCP and DNS on the server, and
  serves DHCP with dnsmasq (`/etc/dnsmasq.d/lanvex.conf`, enabling the service with the first pool).
  It writes only its own files, refuses another tool's VLAN or dnsmasq setting for the same
  interface, serves DHCP only on VLANs it created, checks dnsmasq's syntax first, and restores files,
  profiles, zones and the dnsmasq service on rollback. Protected interfaces are never touched.
- **Changes confirmed in your browser.** For the server Lanvex runs on, which it reaches only through
  loopback: with *My browser confirms changes*, a change that passed every check is kept only when an
  administrator chooses *Keep* in time, from a browser that reaches Lanvex (ideally over one of the
  server's own addresses, which Lanvex tells). Without a decision the server undoes the change. Every
  page shows a change waiting for a decision.
- **Leases** of Lanvex's DHCP pools appear on the server page and in Discovery.
- Reads of NetworkManager servers include Lanvex's DHCP configuration and dnsmasq's leases; leases do
  not count as a new state in the history.
- Lanvex Core 0.7.0: custom fields `mac_address` (IP addresses) and `dhcp_lease_time` (IP ranges); the
  service account may manage IP ranges and remove interfaces.
- Lab: a Fedora 44 host like KlassenServer (NetworkManager, firewalld, dnsmasq) in CI, with a real VLAN
  and a real DHCP client; the lab builds from the runners' Ubuntu mirror and retries a failed
  download on another.

**Updating:** after the update, every server's change helper shows as outdated (version 2). Install it
again from the server page, as the first time. Devices on new networks reach the server's DHCP and DNS;
access to other networks and the Internet comes with access rules (0.9).

## 0.6.1 — 2026-11-16

Fixes from the first installation on Fedora Server with rootless Podman (KlassenServer).

- **Changes are safe when Lanvex reaches a server indirectly.** When the address Lanvex connects to
  is not one of the server's own (port forwarding, NAT, or Lanvex running on that server through
  `host.containers.internal`), signing in again after a change proved nothing: a change that cut the
  server off its network could be confirmed. Lanvex now refuses changes for such a server until it
  has a verification address, one of its own that Lanvex reaches directly; that address is
  protected, checked before a change is prepared, and must be reachable after the change, or the
  change is rolled back. The server page shows which interface Lanvex relies on.
- **Protected interfaces.** Interfaces another tool manages (names or patterns like `vlan-*`) can be
  excluded from changes per server.
- **Podman.** The installer works with Podman and podman-compose (rootless too): containers are found
  by their Compose labels, health checks are in docker-compose.yml (Podman's OCI images carry none)
  and run by the installer where no systemd timer does, every image name is fully qualified (Fedora's
  short-name policy), and a systemd unit starts Lanvex at boot. `scripts/check-install.sh` works with
  both. CI now installs Lanvex under rootless Podman configured like Fedora; that found one more
  problem, fixed here: with Podman 4, Lanvex Core could not write its static files, because the
  shared volume took the owner of the `lanvex` container, which mounted it first.
- **Failed sign-ins.** Scheduled reads pause after two failed sign-ins in a row, so OpenSSH's
  PerSourcePenalties (or fail2ban) does not block Lanvex's address, and behind NAT everyone else's.
  OpenSSH's "Not allowed at this time" is recognised and shown as *Refused for now*. A successful read
  resumes the schedule.
- **Prefixes.** The list includes each prefix's gateway (it showed none before). Prefixes can be
  given a site when created or edited. Utilization counts addresses inside a range marked utilized
  only once, as NetBox does. A DHCP range counts only when it is marked utilized, in both.
- **Scripting against Lanvex Core.** API responses to a signed-in session set the CSRF cookie
  (`lanvex_core_csrftoken`); docs/core.md shows how to script writes with a Lanvex session.
- A sign-in refused for missing HTTPS logs the address it came from, to find a reverse proxy missing
  from `LANVEX_TRUSTED_PROXIES`.
- Docs: Podman, appliances and NAS systems as servers, paused reads; stale version numbers removed.
- Lanvex Core 0.6.1 (core.lock).

## 0.6.0 — 2026-11-15

Keeping documentation and servers in step: what changed on a server, where it differs from Lanvex
Core, closing each difference in either direction, and what servers see around them.

- **Server history.** Lanvex reads each server every hour (configurable, per server too) and keeps
  each state the server was in. The server page lists what changed between them in words: addresses
  added or removed, links lost, MTU, gateway, routes, DNS. Reads that find nothing new are counted
  instead of stored, so a year of hourly reads of a quiet server is one entry.
- **Closing differences with NetBox.** Each address where the server and NetBox differ can be
  - recorded in NetBox: a preview of exactly which NetBox updates are needed (create the address on
    the right interface, add the interface if the device lacks it, correct a prefix length, unassign
    an address the server no longer has), then those updates and no others; or
  - changed on the server: the change form opens with that difference undone, followed by the usual
    preview, approval, verification and automatic rollback.
- After a successful change, its page says whether NetBox documents the result and offers to record it.
- **Discovery.** A new page lists what servers see around them, compared with NetBox:
  - hosts in their neighbour tables (kept for 30 days after last seen), and which of them NetBox lacks;
    those can be recorded as unassigned addresses after a preview;
  - devices announcing themselves over LLDP on each server interface (from systemd-networkd or lldpd),
    compared with the documented cable, through patch panels. A missing cable can be recorded.
  The server page shows each interface's LLDP neighbour, and the history notes when it changes.
  docs/operations.md explains how to let Lanvex read LLDP.
- `LANVEX_SERVER_SNAPSHOT_RETENTION` now counts different states and defaults to 200 (was 20 reads).
- New settings `LANVEX_SERVER_READ_INTERVAL_MINUTES` (default 60, `0` = off) and
  `LANVEX_READ_SCHEDULER_ENABLED`, `LANVEX_DISCOVERY_RETENTION_DAYS` (default 30). Recording an interface the device lacks needs *add* on interfaces
  for your own NetBox's token, recording a cable *add* on cables (docs/operations.md). Lanvex Core
  0.6.0 gives its service account exactly that.
- The SSH lab (three real Ubuntu servers with netplan, NetworkManager and ifupdown) now runs in CI.

## 0.5.0 — 2026-11-15

- **Topology.** The map of your network that was a placeholder until now: devices, sites, provider
  circuits and the cables between them, as documented in Lanvex Core or NetBox.
  - Layout by role: circuits on top, then core, distribution, access, patch panels, servers and other
    end devices. Cables between devices of the same rank are drawn side by side; servers and other
    devices on a single cable are stacked under their switch, so large sites stay compact.
  - One site or all sites (each in its own frame); devices at the far end of a cable from another site
    are shown and marked.
  - Search, a details panel with every cable of a device, links to the device, its server in Lanvex
    and its page in Lanvex Core, and a cable list for reading everything as a table.
  - Planned cables are dashed; devices Lanvex reads over SSH are marked.
  - Fast with hundreds of devices: the layout runs in the background, and the map's code loads only when
    the page is opened.
- With your own NetBox, the token needs *view* on cables for the map (docs/operations.md).
- **Fix:** two quick changes to a page's filters (switching a view and then choosing a site, say)
  kept only the second. Affected every filtered page.
- CI keeps Playwright traces of failed browser tests for 14 days.

## 0.4.2 — 2026-11-14

- **Fix:** 0.4.1's new health check reported Lanvex Core *healthy* while it was still setting up its
  database, so the installer finished before Lanvex Core answered. Found by the first automatic full
  installation; 0.4.0 was not affected.
- Lanvex is licensed under the Apache License 2.0 (`LICENSE`, `NOTICE`).
- `scripts/check-install.sh` checks a running installation: containers, logs, sign-in, Lanvex Core.
- Continuous integration for both repositories, including a full installation with `install.sh`
  ([docs/development.md](docs/development.md#continuous-integration)).

## 0.4.1 — 2026-11-14

Fixes from the first installation on a real server (Ubuntu 24.04, Docker Compose v2).

- **First start no longer crashes.** Both web workers created the first administrator at the same
  moment; the second failed and stopped Lanvex until Docker restarted it. A database lock now makes
  the others wait.
- **`core-worker` reports its real health.** It inherited the web application's health check and
  turned *unhealthy* after ten minutes although it worked. The check now looks for the worker's
  registration in Redis.
- **Installer: choose the port.** `--port [ADDRESS:]PORT` and a question for it; the installer checks
  that the port is free before writing anything. If starting fails, it says where the first
  administrator's password is and how to continue.
- **No more alarming lines during installation and start:** the pull error for `lanvex-core:local`,
  gunicorn's "Control server error: Permission denied", and the `useradd` warning about uid 1000.
- **Consistent log times:** Lanvex Core's containers log in the configured time zone, with the UTC
  offset on every line.

Updating from 0.4.0: see [docs/core.md](docs/core.md#updating). Nothing in `.env` changes.

## 0.4.0 — 2026-11-13

- **Lanvex Core**: NetBox 4.7.5 with Lanvex's branding and single sign-on, installed and set up
  together with Lanvex, reachable only through Lanvex at `/core/`.
- **Installer** (`install.sh`): Lanvex Core or an existing NetBox, HTTPS proxy or plain HTTP, every
  secret generated.
- Fix: a network's detail page failed on a NetBox that had no `lanvex-gateway` tag yet.

Earlier versions: 0.3 changed server network configuration with rollback on the server; 0.2 read
servers over SSH; 0.1 was the console for networks, addresses, VLANs and devices.
