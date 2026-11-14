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

## Lanvex Core

Lanvex Core is a separate repository (a NetBox fork; see its `FORK.md`), expected next to this one
as `../lanvex-core`. Its tests: `lanvex/scripts/test.sh` there (PostgreSQL and Redis on localhost).

To run Lanvex against a local Lanvex Core without Docker, start Lanvex Core like NetBox, from its
`netbox/` directory, with its configuration module and the variables the installer would write:

```bash
export NETBOX_CONFIGURATION=lanvex_configuration
export LANVEX_CORE_DB_HOST=localhost LANVEX_CORE_DB_PASSWORD=… LANVEX_CORE_REDIS_HOST=localhost
export LANVEX_CORE_SECRET_KEY=… LANVEX_CORE_API_TOKEN_PEPPER=… LANVEX_CORE_PROXY_SECRET=…   # 32+ characters
export LANVEX_CORE_SERVICE_TOKEN=nbt_aaaaaaaaaaaa.bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
python manage.py migrate && python manage.py collectstatic --no-input && python manage.py lanvex_init
gunicorn --bind 127.0.0.1:8200 netbox.wsgi
```

and point Lanvex at it:

```bash
export LANVEX_NETBOX_URL=http://127.0.0.1:8200/core LANVEX_NETBOX_TOKEN=$LANVEX_CORE_SERVICE_TOKEN
export LANVEX_NETBOX_PUBLIC_URL=/core LANVEX_CORE_UPSTREAM_URL=http://127.0.0.1:8200
export LANVEX_CORE_STATIC_DIR=/path/to/lanvex-core/netbox/static   # plus the same LANVEX_CORE_PROXY_SECRET
```

Then, with Lanvex running on port 8000 (the Vite dev server also forwards `/core`):

```bash
LANVEX_TEST_NETBOX_URL=http://127.0.0.1:8200/core LANVEX_TEST_NETBOX_TOKEN=$LANVEX_CORE_SERVICE_TOKEN uv run pytest -m netbox
LANVEX_E2E_CORE_URL=http://127.0.0.1:8000 LANVEX_E2E_CORE_PASSWORD=… npx playwright test core.live
```

`core.lock` pins the Lanvex Core commit a Lanvex release ships with. `scripts/fetch-core.sh` puts it
into `./core` for `docker compose`; `scripts/release.sh` builds the release archive (Lanvex at HEAD
plus `./core`). Steps for a new Lanvex Core version are in [core.md](core.md#for-developers).

## Continuous integration

Both repositories check every push and pull request on GitHub Actions.

| Repository | Workflow | What it does |
|---|---|---|
| lanvex | `.github/workflows/ci.yml` | Backend (ruff, pytest with PostgreSQL), frontend (type check, unit tests, build), browser tests against the NetBox stand-in, shellcheck and an installer run in both modes, the image build, and a **full installation**: `install.sh --core` on a fresh runner with the Lanvex Core version from `core.lock`, then `scripts/check-install.sh` |
| lanvex-core | `.github/workflows/lanvex-core.yml` | Lint, fork check, no migrations of our own, Lanvex Core's tests, NetBox's complete suite on `lanvex-core-*` tags (or on request: *Run workflow*), and the image built and started with `lanvex/scripts/smoke-image.sh` |

The full installation fetches lanvex-core at the commit in `core.lock`. Both repositories are public,
so that needs nothing. Should lanvex-core ever become private, give the job a read-only deploy key:

```bash
ssh-keygen -t ed25519 -N '' -C lanvex-ci -f lanvex-core-ci
```

Add `lanvex-core-ci.pub` to lanvex-core under *Settings → Deploy keys* (leave *Allow write access*
off), and the content of `lanvex-core-ci` to lanvex under *Settings → Secrets and variables → Actions*
as `LANVEX_CORE_DEPLOY_KEY`. Then delete both files.

`scripts/check-install.sh [URL] [USERNAME] [PASSWORD]` is the check the full installation ends with,
and works on any installation: run it in the directory you installed from.

## Layout

```
backend/lanvex/
  core/        settings, logging, errors, DB, rate limiting, request context
  netbox/      the only HTTP client for NetBox
  auth/ audit/ users, sessions, roles; audit trail
  ipam/        prefixes, IP addresses, address arithmetic (pure, heavily tested)
  vlans/ devices/ lookups/ dashboard/ system/
  integrations/ adapters; linux/ = SSH transport, fixed commands, parsers, the change helper
  servers/     server registry, credentials, host keys, snapshots, NetBox comparison
  changes/     server changes: preparation, approval, the runner
  core_proxy.py  /core/ proxy to Lanvex Core
frontend/src/
  app/         session, theme, layout
  components/  ui primitives, data table, address-space strip and map
  pages/       one module per section
  lib/         API client, types, CIDR parsing, hooks
```

## Conventions

- The browser talks only to `/api/v1` (and to Lanvex Core's own pages under `/core/`). Components consume
  Lanvex models (`lib/types.ts`), never raw NetBox JSON.
- Say "NetBox" in the UI through `useNetBoxLabel()`, which gives "Lanvex Core" when Lanvex Core is installed.
- Every write goes through `audit.track(...)` and returns `state: "documented"`.
- Do not invent NetBox endpoints or fields. Check the serializers and filtersets of the target NetBox version.
- `integrations/linux/lanvex_netapply.py` runs **on servers, as root**. Keep it one file, standard
  library plus PyYAML, Python 3.7+ (`uvx vermin -t=3.7- lanvex/integrations/linux/lanvex_netapply.py`),
  with fixed argument lists and no shell. Raise `HELPER_VERSION` whenever its requests or answers
  change, so servers with the old file show *Outdated version*.
