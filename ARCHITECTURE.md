# Lanvex Architecture

Lanvex is a self-hosted network infrastructure console. NetBox is the source of truth for
inventory and IPAM; Lanvex adds an independent UI, workflows, access control, audit, and
controlled changes to servers.

## Components

```
 Browser (React SPA)
     │  same-origin /api/*  (session cookie + CSRF header)
     ▼
 Lanvex API (FastAPI)  ── PostgreSQL (Lanvex-only data: users, sessions, audit, servers, changes)
     │                 ── Redis (rate limiting)
     │  REST, server-side token
     ▼
 NetBox (separate service, unmodified, its own database)
```

| Component | Responsibility | Never does |
|---|---|---|
| `frontend/` | UI, client-side validation for fast feedback | Talk to NetBox, hold NetBox credentials |
| `backend/lanvex/netbox` | Only module that speaks HTTP to NetBox: auth, timeouts, retries, pagination, error mapping | Business rules |
| `backend/lanvex/ipam`, `vlans`, `devices`, `dashboard` | Domain services: validation, overlap checks, utilization, mapping NetBox payloads to Lanvex models | Raw HTTP |
| `backend/lanvex/auth` | Local users, Argon2 hashing, server-side sessions, roles, CSRF | — |
| `backend/lanvex/audit` | Append-only audit trail of every write Lanvex performs | — |
| `backend/lanvex/integrations` | Adapters declaring explicit capabilities. `linux` (SSH) reads servers and talks to the change helper; others plug in the same way | Run arbitrary commands |
| `backend/lanvex/servers` | Server registry, encrypted credentials, pinned host keys, snapshots of actual state, comparison with NetBox | Write to servers |
| `backend/lanvex/changes` | Server changes: preparation and safety rules, approval, the runner that applies, verifies, confirms or rolls back | Change anything without an approved, previewed change |

## Three states, kept separate

1. **Documented (desired)** — objects in NetBox. Every IPAM, VLAN and device write lands here only.
2. **Actual (running)** — what servers really run: snapshots read over SSH (`server_snapshots`).
3. **Pending** — server changes awaiting approval or being applied (`server_changes`).

The UI labels NetBox writes as "Recorded in NetBox", never "deployed". A server change is shown as
applied only after the server confirmed it and Lanvex verified it; it never updates NetBox by itself,
so the comparison on the server page shows any difference that remains.

## Server management: reading

```
 Lanvex API ──SSH (pinned host key, one stored credential, fixed commands)──▶ Linux server
     │                                                                      (unprivileged user)
     ├── servers            registry; credentials encrypted with LANVEX_SECRET_KEY (Fernet via HKDF)
     ├── server_snapshots   immutable reads of *actual* state (facts JSON + raw output, pruned)
     └── NetBox             *documented* state for the linked device/VM, compared address by address
```

- **Adapters and capabilities.** `integrations/base.py` defines capabilities (`read.interfaces`,
  `read.routes`, …, `change.interface_ipv4`). The UI and API offer only what the server's adapter
  declares.
- **Commands.** `integrations/linux/commands.py` is the entire set Lanvex can run: constants, with
  a fixed `LC_ALL=C` and `PATH`. Callers pass an enum, never a string. Stack-specific tools run only
  when that stack's service is active.
- **Trust.** Host keys are fetched without logging in, shown as SHA-256 fingerprints, and pinned only
  after an administrator confirms them; the server re-checks that the confirmed fingerprint matches
  the key. Connections use only the pinned key and the server's own credential (no agent, no
  `~/.ssh`). A changed key stops all access until re-verified.
- **Parsing.** Pure functions over command output (`parsers.py`, `assemble.py`), tested against
  output captured from real hosts running netplan/networkd, NetworkManager and ifupdown.
- **Execution.** Reads run in the request (typically 150–300 ms) with per-server locking and
  timeouts.

## Server management: changes

```
 UI ── prepare ──▶ API: fresh read, safety rules ──SSH──▶ helper plan   (files before/after, commands)
 UI ── approve ──▶ server_changes: approved (typed name; optionally a second admin)
                     │
 ChangeRunner ◀──────┘  (in-process loop; SELECT … FOR UPDATE SKIP LOCKED; heartbeat)
   ├─SSH─▶ helper apply   drift check → backup → arm systemd timer → write → netplan/nmcli apply
   ├─SSH─▶ helper verify  (new connection) addresses, default route, gateway, DNS, name lookup
   ├─SSH─▶ helper confirm (cancel timer)  or  helper rollback (restore backup now)
   └─SSH─▶ read the server again → snapshot
```

- **The helper.** `integrations/linux/lanvex_netapply.py` is installed by an administrator at
  `/usr/local/libexec/lanvex/lanvex-netapply`; a sudoers rule lets the Lanvex user run exactly that
  file as root. It takes typed JSON on stdin, validates it independently of Lanvex, and supports
  netplan (networkd or NetworkManager renderer) and NetworkManager keyfiles. netplan files are edited
  key by key, so unrelated lines stay byte-identical; NetworkManager keyfiles are edited directly
  rather than through `nmcli modify`, which on netplan-integrated systems migrates profiles.
- **The rollback lives on the server.** Before writing anything the helper arms a transient systemd
  timer (`lanvex-rollback-<id>`) that restores the backup unless confirmed. Losing Lanvex, the network
  or the SSH session therefore cannot leave a half-applied change: the server undoes it alone.
- **Lanvex's own rules** on top of the helper's: the address Lanvex connects to must stay on the
  interface as a static address; warnings for gateway changes and the management interface; one
  open change per server (a partial unique index); the helper's plan is the preview, and the helper
  refuses to apply if the file changed since (sha256).
- **Status is what Lanvex knows.** If verification cannot reach the server, Lanvex waits until after
  the server's deadline and asks it what happened. If it still cannot find out (or Lanvex restarted
  mid-change; stale heartbeats are picked up after 120 s), the change is `needs_attention`, never
  "succeeded". Every outcome is audited (`server.change.execute`).

## Data flow: create a prefix

1. UI wizard validates CIDR (Zod) → `POST /api/v1/prefixes/validate` for server-side checks
   (strict network, duplicates in the same VRF, parents/children for context).
2. User confirms the summary → `POST /api/v1/prefixes` (admin role, CSRF, rate-limited).
3. Backend re-validates, creates the prefix via `POST /api/ipam/prefixes/`, optionally the
   gateway IP (`POST /api/ipam/ip-addresses/`, tagged `lanvex-gateway`), writes an audit event,
   then reads the object back from NetBox and returns it.

## NetBox integration rules

- Targets NetBox **4.2+** (prefix `scope_type`/`scope_id` model); developed against the 4.7.4 API.
- Token: v2 tokens (`nbt_…`) use `Authorization: Bearer`, v1 use `Authorization: Token`.
- Retries only for idempotent methods (GET/HEAD) on connect errors and 502/503/504.
- NetBox has no prefix "name" or "gateway" field. Lanvex maps the network name to
  `description`, and stores the gateway as a real IP address object inside the prefix
  tagged `lanvex-gateway`. No custom fields are assumed.
- Utilization is computed by Lanvex using NetBox's own method (child prefixes for containers,
  IP count over usable size otherwise), because the REST API does not expose it.

## Lanvex database (PostgreSQL)

`users` (id, username, password_hash, role, is_active, timestamps) ·
`sessions` (token hash, user, csrf token, expiry, IP, user agent) ·
`audit_events` (actor, action, target type/id/repr, outcome, detail JSON, IP, request id, time) ·
`servers` (address, user, auth method, encrypted credential, public key, pinned host key, NetBox link, last status, summary) ·
`server_snapshots` (server, time, status, facts JSON, raw output, duration, requested by) ·
`server_changes` (number, server, interface, request, state before, helper preview, status, approvals,
deadline, checks, log, outcome; one open change per server).
Migrations: Alembic in `backend/alembic`.

## Security model

- Roles: `admin` (read + write NetBox objects, manage users) and `readonly`. Enforced server-side
  per endpoint via dependencies; the UI only hides what the server already refuses.
- Sessions: random 256-bit token in an `HttpOnly`, `SameSite=Strict` cookie (`Secure` in production);
  only its SHA-256 hash is stored. Idle and absolute expiry.
- CSRF: per-session token, required in `X-CSRF-Token` for every non-GET request.
- Rate limits (Redis, in-memory fallback in dev/tests): login attempts and write operations.
- Secrets only from environment; never in responses, logs (structlog redaction), or the bundle.
- No user input reaches a shell. Server reads use a fixed command set. The only privileged command is
  the change helper, given typed JSON on stdin; it runs its own tools with fixed argument lists.
  Changes are disabled by default (`LANVEX_AUTOMATION_ENABLED=false`) and per server, need an
  administrator's typed approval, and are rolled back by the server itself unless verified.
- Credentials at rest: Fernet with a key derived from `LANVEX_SECRET_KEY`; never returned by the API,
  logged, or audited. Optional `LANVEX_SERVER_ALLOWED_NETWORKS` limits where Lanvex may connect.

## Directory layout

```
backend/   FastAPI app (lanvex/), Alembic migrations, tests/
frontend/  Vite + React + TS + Tailwind, shadcn-style components (src/components/ui)
deploy/    Compose overlays (local NetBox), nginx config
docs/      Setup, configuration, troubleshooting
```
