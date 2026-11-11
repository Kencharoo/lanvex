# Lanvex Architecture

Lanvex is a self-hosted network infrastructure console. NetBox is the source of truth for
inventory and IPAM; Lanvex adds an independent UI, workflows, access control, audit, and
(later) controlled automation.

## Components

```
 Browser (React SPA)
     │  same-origin /api/*  (session cookie + CSRF header)
     ▼
 Lanvex API (FastAPI)  ── PostgreSQL (Lanvex-only data: users, sessions, audit, jobs)
     │                 ── Redis (rate limiting; job queue in phase 2)
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
| `backend/lanvex/integrations` | Adapters declaring explicit capabilities. `linux` (SSH, read-only) exists; others plug in the same way | Run arbitrary commands |
| `backend/lanvex/servers` | Server registry, encrypted credentials, pinned host keys, snapshots of actual state, comparison with NetBox | Write to servers |

## Three states, kept separate

1. **Documented (desired)** — objects in NetBox. Every write in the MVP lands here only.
2. **Actual (running)** — what devices really run. Read via adapters in phase 2.
3. **Pending** — deployment jobs awaiting approval/execution (phase 2, `jobs` table).

The UI labels MVP writes as "Recorded in NetBox", never "deployed". No adapter exists yet,
so no deployment state can be shown as succeeded.

## Server management (phase 2a, read-only)

```
 Lanvex API ──SSH (pinned host key, one stored credential, fixed commands)──▶ Linux server
     │                                                                      (unprivileged user)
     ├── servers            registry; credentials encrypted with LANVEX_SECRET_KEY (Fernet via HKDF)
     ├── server_snapshots   immutable reads of *actual* state (facts JSON + raw output, pruned)
     └── NetBox             *documented* state for the linked device/VM, compared address by address
```

- **Adapters and capabilities.** `integrations/base.py` defines capabilities (`read.interfaces`,
  `read.routes`, …). The `linux-ssh` adapter declares only read capabilities; the UI and API offer
  only what the server's adapter declares. Write capabilities will be added per adapter, each behind
  validation → preview → approval → checkpoint → execution → verification → rollback.
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
  timeouts. A background job runner arrives with write operations, which need it for
  checkpoint/rollback windows.

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
`server_snapshots` (server, time, status, facts JSON, raw output, duration, requested by).
Migrations: Alembic in `backend/alembic`.

## Security model

- Roles: `admin` (read + write NetBox objects, manage users) and `readonly`. Enforced server-side
  per endpoint via dependencies; the UI only hides what the server already refuses.
- Sessions: random 256-bit token in an `HttpOnly`, `SameSite=Strict` cookie (`Secure` in production);
  only its SHA-256 hash is stored. Idle and absolute expiry.
- CSRF: per-session token, required in `X-CSRF-Token` for every non-GET request.
- Rate limits (Redis, in-memory fallback in dev/tests): login attempts and write operations.
- Secrets only from environment; never in responses, logs (structlog redaction), or the bundle.
- No user input reaches a shell. Server reads use a fixed command set; future write operations will
  be narrowly scoped, typed operations behind adapters, disabled by default (`LANVEX_AUTOMATION_ENABLED=false`).
- Credentials at rest: Fernet with a key derived from `LANVEX_SECRET_KEY`; never returned by the API,
  logged, or audited. Optional `LANVEX_SERVER_ALLOWED_NETWORKS` limits where Lanvex may connect.

## Directory layout

```
backend/   FastAPI app (lanvex/), Alembic migrations, tests/
frontend/  Vite + React + TS + Tailwind, shadcn-style components (src/components/ui)
deploy/    Compose overlays (local NetBox), nginx config
docs/      Setup, configuration, troubleshooting
```
