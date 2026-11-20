# Lanvex

**Network Infrastructure, Simplified.** A self-hosted console for networks, IP addresses, VLANs and
devices, built on [NetBox](https://github.com/netbox-community/netbox) as the source of truth.

Lanvex has its own interface, backend, users and audit trail. It installs with **Lanvex Core**, a
NetBox-based application for documenting your network that you open from Lanvex with the same
sign-in, or works with a NetBox you already run. Either way it talks to them only through their REST
API and never touches their database. Changes to networks, IP addresses and VLANs are
**documentation in NetBox**, and the UI says so on every write. What Lanvex configures for real is a
Linux server's network (interface addresses, gateway and DNS; VLANs, DHCP, access rules and DNS
filters) and a switch port's VLANs, and only when an administrator has switched that on, previewed the
change and approved it; see below.

## What works

| Area | Capability |
|---|---|
| Networks | Browse prefixes by VRF and hierarchy, NetBox-accurate utilization, four-step creation wizard with live server-side checks (host bits, duplicates in the VRF, parent/child placement, gateway validity), edit metadata and gateway, delete with typed confirmation and impact summary, free-space map, next free addresses |
| IP addresses | Search and filter, add a specific address or atomically take the next free one, status changes, assign to / release from a device interface (guards primary IPs), delete |
| VLANs | Browse, create, edit, delete; VID/name uniqueness per group; link to prefixes and interfaces |
| Devices | Inventory list and detail with interfaces, 802.1Q modes, addresses and VLANs; edit an interface's documented VLANs |
| Servers | Register Ubuntu Server, Debian and Fedora Server hosts over SSH with a verified, pinned host key and a key Lanvex generates; read interfaces, addresses, routes, DNS, LLDP neighbours and which network stack (netplan/systemd-networkd, NetworkManager, ifupdown) manages what, on demand and on a schedule; a history of every state with what changed, in words; compare with the addresses NetBox documents for the linked device or VM, and close each difference either way: record it in NetBox after a preview, or change the server to match |
| Server changes | Change one interface's IPv4 addresses (or DHCP), default gateway and DNS, its IPv6 settings and its static routes, and create or remove bridges and bonds, on netplan and NetworkManager servers. Previewed by a helper on the server, approved by an administrator (optionally a second one), applied behind a rollback timer the server arms itself, verified over a new SSH connection, and confirmed or rolled back automatically; for the server Lanvex runs on, confirmed in the administrator's browser. Off by default: see [docs/changes.md](docs/changes.md) |
| Server networks | A server as the router for VLAN networks: VLAN interfaces, DHCP pools and fixed addresses (dnsmasq, or Kea where it runs), what each network's devices may reach (the Internet, other networks, single addresses and ports, services on the server; on firewalld, ufw or Lanvex's own nftables table), a DNS filter per network, documented in Lanvex Core and brought to the server in one approved change; active leases, one click to a fixed address. Imports and takes over a running Netzverwaltung setup. On NetworkManager (Fedora, RHEL), netplan (Ubuntu Server) and ifupdown (Debian) servers. See [docs/networks.md](docs/networks.md) |
| Switches | Managed switches read over SNMP (v3 or v2c): ports, VLANs, link aggregations, MAC address table, LLDP neighbours. Which device is on which port, named from servers Lanvex reads and the documentation, compared with the documented cables; missing cables recorded after a preview. Standard MIBs and Cisco IOS. A port's VLANs changed as an approved change, read back and undone if the switch does not take it. See [docs/switches.md](docs/switches.md) |
| Lanvex Core | NetBox 4.7.5 with Lanvex's look, installed and set up by the installer: sites, racks, devices, cables, circuits, virtualization and the rest of NetBox's model, at `/core/` with Lanvex's sign-in. Administrators edit, read-only users look. See [docs/core.md](docs/core.md) |
| Topology | Interactive map of devices, sites, provider circuits and the cables between them, as documented: role-ranked layout (core above access above servers), servers stacked under their switch, search, a details panel with every cable, devices read by Lanvex marked, connections switches see but no cable records drawn apart, and a cable list. Stays fast with hundreds of devices |
| Discovery | Hosts in the servers' neighbour tables and DHCP leases with the switch port they are on, the devices on the other end of each server interface as they announce themselves over LLDP, and what switches see on their ports, compared with NetBox: missing addresses and cables can be recorded after a preview |
| Overview | Inventory counts, most-utilized networks, addresses by status, recent changes (NetBox change log, falling back to the Lanvex audit log) |
| Monitoring | Alerts for servers and switches that stop answering or cannot be read, drift from the documentation, DHCP pools running full, unknown devices getting a lease and failed changes; by email and webhooks (ntfy, Gotify, Slack, Discord, Teams, JSON). See [docs/monitoring.md](docs/monitoring.md) |
| Platform | Local users, Active Directory or LDAP, and single sign-on with OpenID Connect, with admin / read-only roles from groups ([docs/sign-in.md](docs/sign-in.md)); server-side sessions, CSRF, rate limiting, append-only audit log, light and dark themes, NetBox connection checks |

**Automation** lists every server and switch change with its status. On switches Lanvex changes port VLANs only; it does not configure dedicated routers. On Linux servers it manages VLAN interfaces, DHCP, access rules and DNS filters, and takes over from Netzverwaltung (see [docs/networks.md](docs/networks.md)).

## Installation

You need a Linux server with Docker and Docker Compose v2, or with Podman and podman-compose
(Fedora Server's default; rootless works too, see [docs/operations.md](docs/operations.md#podman)).
`lanvexctl` downloads the latest release, checks it against its published SHA-256 sum and installs it:

```bash
curl -fsSLO https://raw.githubusercontent.com/Kencharoo/lanvex/main/lanvexctl
sudo bash lanvexctl install          # rootless Podman: bash lanvexctl install
```

The installer asks where to keep your network documentation (Lanvex Core, installed now, or a NetBox
you already run), whether Lanvex will be reached over HTTPS through a reverse proxy or over plain HTTP,
which port it listens on, and the first administrator's name. It generates every password and key into
`.env`, builds and starts the containers, waits until they are healthy and prints how to sign in.
Options after `--` go to the installer (`lanvexctl install -- --help` lists them). From a release
archive instead: `lanvexctl install --file lanvex-1.3.0.zip`, or unzip it and run `./install.sh`.

| Command | |
|---|---|
| `lanvexctl update` | Backs up, installs the latest release next to the running one, replaces the containers |
| `lanvexctl status` | Version, directory, address, health of every container |
| `lanvexctl backup [FILE]` | Both databases, Lanvex Core's uploads and `.env` in one file (mode 600) |
| `lanvexctl restore FILE` | Into this installation, also a fresh one after a reinstall |
| `lanvexctl start`, `stop`, `restart`, `logs [SERVICE]` | |
| `lanvexctl user create NAME [--admin]`, `deactivate`, `delete` | |
| `lanvexctl uninstall [--purge]` | Removes the containers; `--purge` also the data (backups stay) |

[docs/core.md](docs/core.md#updating) covers updates, backups and restores in detail;
[CHANGELOG.md](CHANGELOG.md) lists what changed.

Session cookies are HTTPS-only unless you choose plain HTTP. For HTTPS see
[docs/operations.md](docs/operations.md). Setting up by hand instead: copy `.env.example` to `.env`,
fill it in, `docker compose up -d --build`.

## Development

```bash
# backend (Python 3.12+, PostgreSQL, optional Redis)
cd backend && uv sync && uv run lanvex migrate && uv run uvicorn lanvex.main:app --reload
# frontend (Node 22+)
cd frontend && npm ci && npm run dev    # http://localhost:5173, proxies /api to :8000
```

Full setup, including a NetBox stand-in for UI work without NetBox: [docs/development.md](docs/development.md).

## Tests

| Suite | Command | Talks to |
|---|---|---|
| Backend unit + API | `cd backend && uv run pytest` | Real PostgreSQL; NetBox is a test double (respx) |
| Live NetBox integration | `LANVEX_TEST_NETBOX_URL=… LANVEX_TEST_NETBOX_TOKEN=… uv run pytest -m netbox` | A real, disposable NetBox |
| Frontend unit | `cd frontend && npm test` | — |
| End-to-end | `cd frontend && npx playwright test` | Real API + UI; NetBox stand-in (`backend/tests/fake_netbox.py`) |
| Lanvex Core | `lanvex/scripts/test.sh` in the Lanvex Core repository; `LANVEX_E2E_CORE_URL=… npx playwright test core.live` | Lanvex Core's own tests; sign-in, roles and links through a running Lanvex |
| SSH lab | `LANVEX_TEST_LAB=1 uv run pytest -m lab`, `LANVEX_E2E_LAB=1 npx playwright test` | Real SSH servers from `deploy/lab`, including real configuration changes and rollbacks on them |

Test doubles are labelled as such in their files. Passing them shows Lanvex behaves as designed
against the NetBox 4.7 API shapes it was written for; only the live suite proves compatibility with
your NetBox.

## Docs

- [ARCHITECTURE.md](ARCHITECTURE.md) — components, data flows, security model
- [docs/core.md](docs/core.md) — Lanvex Core: what it is, sign-in and roles, backups, updates, troubleshooting
- [docs/configuration.md](docs/configuration.md) — every setting
- [docs/sign-in.md](docs/sign-in.md) — signing in with Active Directory, LDAP or single sign-on (OpenID Connect)
- [docs/development.md](docs/development.md) — local setup and tests
- [docs/operations.md](docs/operations.md) — production, NetBox token permissions, backups, troubleshooting
- [docs/changes.md](docs/changes.md) — changing server configuration: setup, how a change runs, safety, rollback
- [docs/switches.md](docs/switches.md) — switches over SNMP: setup, what is read, which port a device is on, changing port VLANs
- [docs/status.md](docs/status.md) — what has been verified, and what has not

## Licence

Lanvex is licensed under the [Apache License 2.0](LICENSE); see also [NOTICE](NOTICE). Lanvex Core,
installed with it, is a modified version of [NetBox](https://github.com/netbox-community/netbox),
also under the Apache License 2.0, with its own notices in the lanvex-core repository.
