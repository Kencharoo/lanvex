# Lanvex

**Network Infrastructure, Simplified.** A self-hosted console for networks, IP addresses, VLANs and
devices, built on [NetBox](https://github.com/netbox-community/netbox) as the source of truth.

Lanvex has its own interface, backend, users and audit trail. It installs with **Lanvex Core**, a
NetBox-based application for documenting your network that you open from Lanvex with the same
sign-in, or works with a NetBox you already run. Either way it talks to them only through their REST
API and never touches their database. Changes to networks, IP addresses and VLANs are
**documentation in NetBox**, and the UI says so on every write. The one thing Lanvex configures for
real is a Linux server's interface addresses, gateway and DNS, and only when an administrator has
switched that on, previewed the change and approved it; see below.

## What works in 0.4

| Area | Capability |
|---|---|
| Networks | Browse prefixes by VRF and hierarchy, NetBox-accurate utilization, four-step creation wizard with live server-side checks (host bits, duplicates in the VRF, parent/child placement, gateway validity), edit metadata and gateway, delete with typed confirmation and impact summary, free-space map, next free addresses |
| IP addresses | Search and filter, add a specific address or atomically take the next free one, status changes, assign to / release from a device interface (guards primary IPs), delete |
| VLANs | Browse, create, edit, delete; VID/name uniqueness per group; link to prefixes and interfaces |
| Devices | Inventory list and detail with interfaces, 802.1Q modes, addresses and VLANs; edit an interface's documented VLANs |
| Servers | Register Ubuntu Server, Debian and Fedora Server hosts over SSH with a verified, pinned host key and a key Lanvex generates; read interfaces, addresses, routes, DNS and which network stack (netplan/systemd-networkd, NetworkManager, ifupdown) manages what; compare with the addresses NetBox documents for the linked device or VM |
| Server changes | Change one interface's IPv4 addresses (or DHCP), default gateway and DNS on netplan and NetworkManager servers. Previewed by a helper on the server, approved by an administrator (optionally a second one), applied behind a rollback timer the server arms itself, verified over a new SSH connection, and confirmed or rolled back automatically. Off by default: see [docs/changes.md](docs/changes.md) |
| Lanvex Core | NetBox 4.7.5 with Lanvex's look, installed and set up by the installer: sites, racks, devices, cables, circuits, virtualization and the rest of NetBox's model, at `/core/` with Lanvex's sign-in. Administrators edit, read-only users look. See [docs/core.md](docs/core.md) |
| Overview | Inventory counts, most-utilized networks, addresses by status, recent changes (NetBox change log, falling back to the Lanvex audit log) |
| Platform | Local users with admin / read-only roles, server-side sessions, CSRF, rate limiting, append-only audit log, light and dark themes, NetBox connection checks |

**Automation** lists every server change with its status. **Topology** appears as planned and says
what it needs before it ships. Lanvex does not configure routers, switches or DHCP services.

## Installation

You need a Linux server with Docker and Docker Compose v2.

```bash
unzip lanvex-0.4.1.zip && cd lanvex-0.4.1
sudo ./install.sh
```

The installer asks where to keep your network documentation (Lanvex Core, installed now, or a NetBox
you already run), whether Lanvex will be reached over HTTPS through a reverse proxy or over plain HTTP,
which port it listens on (it checks that the port is free), and the first administrator's name. It
generates every password and key into `.env`, builds and starts the containers, waits until they are
healthy and prints how to sign in. `./install.sh --help` lists options for installing without
questions, such as `--port 100.64.0.5:8081`.

To update, unpack the new release next to the old one, copy `.env` into it and run
`docker compose up -d --build` there; the data stays in Docker's volumes. Back up first
([docs/core.md](docs/core.md#updating)). [CHANGELOG.md](CHANGELOG.md) lists what changed.

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
- [docs/development.md](docs/development.md) — local setup and tests
- [docs/operations.md](docs/operations.md) — production, NetBox token permissions, backups, troubleshooting
- [docs/changes.md](docs/changes.md) — changing server configuration: setup, how a change runs, safety, rollback
- [docs/status.md](docs/status.md) — what has been verified, and what has not

## Licence

Lanvex is licensed under the [Apache License 2.0](LICENSE); see also [NOTICE](NOTICE). Lanvex Core,
installed with it, is a modified version of [NetBox](https://github.com/netbox-community/netbox),
also under the Apache License 2.0, with its own notices in the lanvex-core repository.
