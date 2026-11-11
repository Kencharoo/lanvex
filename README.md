# Lanvex

**Network Infrastructure, Simplified.** A self-hosted console for networks, IP addresses, VLANs and
devices, built on [NetBox](https://github.com/netbox-community/netbox) as the source of truth.

Lanvex has its own interface, backend, users and audit trail. It talks to NetBox only through its
REST API and never touches the NetBox database. Every change Lanvex makes today is **documentation in
NetBox** — nothing configures a router, switch, server or DHCP service, and the UI says so on every write.

## What works in 0.2

| Area | Capability |
|---|---|
| Networks | Browse prefixes by VRF and hierarchy, NetBox-accurate utilization, four-step creation wizard with live server-side checks (host bits, duplicates in the VRF, parent/child placement, gateway validity), edit metadata and gateway, delete with typed confirmation and impact summary, free-space map, next free addresses |
| IP addresses | Search and filter, add a specific address or atomically take the next free one, status changes, assign to / release from a device interface (guards primary IPs), delete |
| VLANs | Browse, create, edit, delete; VID/name uniqueness per group; link to prefixes and interfaces |
| Devices | Inventory list and detail with interfaces, 802.1Q modes, addresses and VLANs; edit an interface's documented VLANs |
| Servers | Register Ubuntu Server, Debian and Fedora Server hosts over SSH with a verified, pinned host key and a key Lanvex generates; read interfaces, addresses, routes, DNS and which network stack (netplan/systemd-networkd, NetworkManager, ifupdown) manages what; compare with the addresses NetBox documents for the linked device or VM. Read-only |
| Overview | Inventory counts, most-utilized networks, addresses by status, recent changes (NetBox change log, falling back to the Lanvex audit log) |
| Platform | Local users with admin / read-only roles, server-side sessions, CSRF, rate limiting, append-only audit log, light and dark themes, NetBox connection checks |

Topology and Automation appear in the navigation as **planned** and say what they need before
they ship. They contain no controls. Lanvex does not change servers or devices yet.

## Quick start (Docker)

You need Docker with Compose and a NetBox 4.2+ instance with an API token.

```bash
cp .env.example .env          # set LANVEX_POSTGRES_PASSWORD, LANVEX_NETBOX_URL, LANVEX_NETBOX_TOKEN,
                              # LANVEX_BOOTSTRAP_ADMIN_PASSWORD (12+ characters), and LANVEX_SECRET_KEY
                              # (docker compose run --rm lanvex generate-secret-key)
docker compose up -d --build
open http://localhost:8080    # sign in as the bootstrap admin
```

Session cookies are HTTPS-only in production. Put Lanvex behind a TLS reverse proxy (see
[docs/operations.md](docs/operations.md)), or set `LANVEX_COOKIE_SECURE=false` on a trusted test network.

No NetBox yet? [deploy/netbox/README.md](deploy/netbox/README.md) sets up a disposable local one.

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
| SSH lab | `LANVEX_TEST_LAB=1 uv run pytest -m lab`, `LANVEX_E2E_LAB=1 npx playwright test` | Real SSH servers from `deploy/lab` |

Test doubles are labelled as such in their files. Passing them shows Lanvex behaves as designed
against the NetBox 4.7 API shapes it was written for; only the live suite proves compatibility with
your NetBox.

## Docs

- [ARCHITECTURE.md](ARCHITECTURE.md) — components, data flows, security model
- [docs/configuration.md](docs/configuration.md) — every setting
- [docs/development.md](docs/development.md) — local setup and tests
- [docs/operations.md](docs/operations.md) — production, NetBox token permissions, backups, troubleshooting
- [docs/status.md](docs/status.md) — what has been verified, and what has not
