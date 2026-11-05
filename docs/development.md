# Development

## Prerequisites

Python 3.12+ with [uv](https://docs.astral.sh/uv/), Node 22+, PostgreSQL 14+, Redis (optional).

```bash
createuser lanvex --pwprompt          # password used below: lanvex-dev-only
createdb -O lanvex lanvex
createdb -O lanvex lanvex_test        # backend tests
createdb -O lanvex lanvex_e2e         # Playwright tests
```

## Run

```bash
cd backend
uv sync
export LANVEX_DATABASE_URL=postgresql+asyncpg://lanvex:lanvex-dev-only@localhost:5432/lanvex
export LANVEX_NETBOX_URL=https://your-netbox LANVEX_NETBOX_TOKEN=...
export LANVEX_BOOTSTRAP_ADMIN_USERNAME=admin LANVEX_BOOTSTRAP_ADMIN_PASSWORD=change-me-please
uv run lanvex migrate
uv run uvicorn lanvex.main:app --reload --reload-dir lanvex

cd ../frontend
npm ci
npm run dev        # http://localhost:5173
```

### Without a NetBox: the stand-in

`backend/tests/fake_netbox.py` is an in-memory **test double** for the parts of the NetBox 4.7 API
Lanvex uses, seeded with a small campus. It is for UI work and E2E tests only. It is not NetBox and
does not enforce NetBox's validation.

```bash
cd backend && uv run uvicorn tests.fake_netbox:app --port 8001
export LANVEX_NETBOX_URL=http://127.0.0.1:8001 LANVEX_NETBOX_TOKEN=nbt_fake.token
curl -X POST http://127.0.0.1:8001/__reset__   # restore the seed data
```

## Tests

```bash
cd backend
uv run pytest                      # unit + API; migrations run down and up against lanvex_test
uv run ruff check . && uv run ruff format --check .

cd ../frontend
npm run typecheck && npm test
npx playwright install chromium    # once; or LANVEX_E2E_CHROME=/path/to/chrome
npx playwright test                # starts the stand-in, API and UI itself
```

Integration tests against a real NetBox are opt-in and destructive to their own test objects only
(198.51.100.0/24):

```bash
LANVEX_TEST_NETBOX_URL=http://localhost:8000 LANVEX_TEST_NETBOX_TOKEN=nbt_... uv run pytest -m netbox
```

## Layout

```
backend/lanvex/
  core/        settings, logging, errors, DB, rate limiting, request context
  netbox/      the only HTTP client for NetBox
  auth/ audit/ users, sessions, roles; audit trail
  ipam/        prefixes, IP addresses, address arithmetic (pure, heavily tested)
  vlans/ devices/ lookups/ dashboard/ system/
frontend/src/
  app/         session, theme, layout
  components/  ui primitives, data table, address-space strip and map
  pages/       one module per section
  lib/         API client, types, CIDR parsing, hooks
```

## Conventions

- The browser talks only to `/api/v1`. Components consume Lanvex models (`lib/types.ts`), never raw NetBox JSON.
- Every write goes through `audit.track(...)` and returns `state: "documented"`.
- Do not invent NetBox endpoints or fields. Check the serializers and filtersets of the target NetBox version.
