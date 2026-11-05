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
| `backend/lanvex/integrations` (phase 2+) | Pluggable adapters (Linux/SSH, OPNsense, …) declaring explicit capabilities | Run arbitrary commands |

## Three states, kept separate

1. **Documented (desired)** — objects in NetBox. Every write in the MVP lands here only.
2. **Actual (running)** — what devices really run. Read via adapters in phase 2.
3. **Pending** — deployment jobs awaiting approval/execution (phase 2, `jobs` table).

The UI labels MVP writes as "Recorded in NetBox", never "deployed". No adapter exists yet,
so no deployment state can be shown as succeeded.

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
`audit_events` (actor, action, target type/id/repr, outcome, detail JSON, IP, request id, time).
Migrations: Alembic in `backend/alembic`.

## Security model

- Roles: `admin` (read + write NetBox objects, manage users) and `readonly`. Enforced server-side
  per endpoint via dependencies; the UI only hides what the server already refuses.
- Sessions: random 256-bit token in an `HttpOnly`, `SameSite=Strict` cookie (`Secure` in production);
  only its SHA-256 hash is stored. Idle and absolute expiry.
- CSRF: per-session token, required in `X-CSRF-Token` for every non-GET request.
- Rate limits (Redis, in-memory fallback in dev/tests): login attempts and write operations.
- Secrets only from environment; never in responses, logs (structlog redaction), or the bundle.
- No shell execution in the MVP. Phase 2 automation uses narrowly scoped, typed operations
  behind adapters, disabled by default (`LANVEX_AUTOMATION_ENABLED=false`).

## Directory layout

```
backend/   FastAPI app (lanvex/), Alembic migrations, tests/
frontend/  Vite + React + TS + Tailwind, shadcn-style components (src/components/ui)
deploy/    Compose overlays (local NetBox), nginx config
docs/      Setup, configuration, troubleshooting
```
