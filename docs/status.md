# Status of Lanvex 0.1

Last updated 2026-11-05. "Verified" means it was run, not just written.

## Verified

| What | How |
|---|---|
| Database schema and migrations | Alembic upgrade → downgrade → upgrade on PostgreSQL 16 at the start of every backend test run; `alembic check` reports no drift |
| Auth, roles, CSRF, sessions, rate limits, audit | 97 backend tests against real PostgreSQL (login, enumeration-safe errors, rate limiting, CSRF, read-only enforcement, idle expiry, last-admin guard, HTTPS-only cookie guard) |
| Address arithmetic | Exhaustive unit tests, IPv4 and IPv6, matching NetBox 4.7's own utilization and usable-range rules |
| NetBox client behaviour | Error mapping, retries (reads only), timeouts on writes, pagination, token header formats, version check — against a test double |
| API contract per module | Prefixes, IP addresses, VLANs, devices, interfaces, dashboard — request payloads and response mapping checked against NetBox 4.7.4 serializer field names (from the v4.7.4 source) using a test double |
| UI, end to end | 5 Playwright tests in Chrome: sign-in, listing with hierarchy and utilization, the full creation wizard (host-bit correction, nesting, broadcast-gateway rejection, persistence checked on the NetBox side, audit entry), duplicate blocking, typed-confirmation delete. Runs the real API and UI against the NetBox stand-in |
| Production serving | Single process serves the built UI (deep links, static assets) and API; unknown API paths return JSON 404; security headers present; JSON logs |
| Frontend types and units | `tsc --noEmit` clean; Vitest covers CIDR parsing and the utilization strip |

## Not yet verified

| What | Why | How to close it |
|---|---|---|
| Against a real NetBox | Installing NetBox in the build environment was not permitted | `uv run pytest -m netbox` with `LANVEX_TEST_NETBOX_URL` / `_TOKEN` pointed at a disposable NetBox (see deploy/netbox) |
| Docker image build and Compose stack | No Docker daemon in the build environment. `docker compose config` validates the file | `docker compose up -d --build` |
| NetBox versions other than 4.7 | Developed against 4.7.4 API definitions; 4.2+ is the declared minimum | Run the live suite against each supported version |

## Not built yet

- Server management (read-only SSH adapter), then controlled network configuration with preview,
  approval, checkpoint, verification and rollback (phase 2)
- Topology view, discovery, drift detection, deployment wizard, configuration history, monitoring
- A background worker. Redis is used for rate limiting only; jobs arrive with phase 2
- Editing prefix tenant/scope and IP tenant/role/NAT (readable, not editable yet)
- Device and interface creation (devices are managed in NetBox)

## Known limitations

- Utilization counts IP records; NetBox counts distinct hosts. They differ only when the same host is
  recorded twice in one VRF.
- List utilization is computed per row (one or two NetBox calls each, bounded concurrency). Fine for
  pages of 50; large installations will want caching.
- The device picker filters to the first 50 matches.
