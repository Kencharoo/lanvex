# Configuration

All settings are environment variables prefixed `LANVEX_`. In Docker they come from `.env`, which
`install.sh` writes; locally, `backend/.env` is also read. Secrets are never logged or returned by
the API.

## Core

| Variable | Default | Notes |
|---|---|---|
| `LANVEX_ENV` | `development` | `production` turns on Secure cookies and is set by the Docker image |
| `LANVEX_DATABASE_URL` | `postgresql+asyncpg://lanvex:lanvex@localhost:5432/lanvex` | Lanvex's own database. Compose builds it from `LANVEX_POSTGRES_PASSWORD` |
| `LANVEX_REDIS_URL` | `redis://localhost:6379/0` | Used for rate limits. Empty disables Redis (per-process limits) |
| `LANVEX_LOG_LEVEL` | `INFO` | |
| `LANVEX_LOG_JSON` | `false` | `true` in the Docker image |
| `LANVEX_MIGRATE_ON_START` | `true` | Docker entrypoint only |
| `LANVEX_WORKERS` | `2` | Docker entrypoint only |
| `LANVEX_HTTP_PORT` | `8080` | Docker Compose only. Host port; prefix an address to limit it, e.g. `127.0.0.1:8080` behind a proxy on the same host. The installer sets it (`--port`) after checking the port is free |
| `LANVEX_DOCKER_SUBNET` | `172.31.250.0/24` | Docker Compose only. Subnet of the `lanvex` network; its `.1` is the address a proxy on the host connects from. Change it if it overlaps a network you use |

## NetBox

| Variable | Default | Notes |
|---|---|---|
| `LANVEX_NETBOX_URL` | — | Base URL without `/api`, e.g. `https://netbox.example.net` |
| `LANVEX_NETBOX_TOKEN` | — | v2 (`nbt_<key>.<secret>`, sent as `Bearer`) or v1 (sent as `Token`) |
| `LANVEX_NETBOX_VERIFY_TLS` | `true` | Only disable for lab instances with self-signed certificates |
| `LANVEX_NETBOX_TIMEOUT_SECONDS` | `10` | Per request |
| `LANVEX_NETBOX_MAX_RETRIES` | `2` | Read requests only. Writes are never retried automatically |

| `LANVEX_NETBOX_PUBLIC_URL` | — | Where browsers reach this NetBox, when it differs from `LANVEX_NETBOX_URL`. Links in Lanvex (*Open in NetBox*) use it. A path (`/core`) or an `http(s)://` URL. With Lanvex Core: `/core` |

Without URL and token Lanvex still starts; NetBox-backed pages show how to connect.

## Lanvex Core

Set by the installer when you choose Lanvex Core; see [core.md](core.md). `COMPOSE_PROFILES=core`
starts its containers. With Lanvex Core, `LANVEX_NETBOX_URL` is `http://core:8080/core` and
`LANVEX_NETBOX_TOKEN` equals `LANVEX_CORE_SERVICE_TOKEN`.

| Variable | Used by | Notes |
|---|---|---|
| `LANVEX_CORE_UPSTREAM_URL` | Lanvex | Lanvex Core inside Docker, `http://core:8080`. Together with the proxy secret it switches on `/core/` and the *Lanvex Core* menu item |
| `LANVEX_CORE_PROXY_SECRET` | both | Shared secret proving a request came through Lanvex. 32+ characters |
| `LANVEX_CORE_STATIC_DIR` | Lanvex | Lanvex Core's static files, `/srv/core-static` (the shared `core-static` volume) |
| `LANVEX_CORE_SERVICE_TOKEN` | Lanvex Core | Token of the `lanvex-service` account: `nbt_` + 12 + `.` + 40 letters or digits. Applied on every start |
| `LANVEX_CORE_DB_PASSWORD` | Lanvex Core | Password of its PostgreSQL. Set when the database is first created; see core.md to change it |
| `LANVEX_CORE_SECRET_KEY` | Lanvex Core | Django secret key (sessions, signing) |
| `LANVEX_CORE_API_TOKEN_PEPPER` | Lanvex Core | Hashes API tokens. Changing it invalidates tokens; the service token is recreated on start |
| `LANVEX_CORE_TIME_ZONE` | Lanvex Core | e.g. `Europe/Berlin`; default `UTC` |

Compose passes only these to Lanvex Core's containers, and its own secrets only to them. Lanvex Core
reads further optional `LANVEX_CORE_*` variables (database and Redis hosts, log level, number of web
workers), listed in `netbox/lanvex_configuration.py` and `lanvex/docker-entrypoint.sh` in Lanvex
Core; to use one, add it to the `core` service's `environment` in `docker-compose.yml`.

## Sessions and access

| Variable | Default | Notes |
|---|---|---|
| `LANVEX_BOOTSTRAP_ADMIN_USERNAME` / `_PASSWORD` | — | Creates the first admin when no users exist. Remove after first start |
| `LANVEX_SESSION_IDLE_MINUTES` | `60` | |
| `LANVEX_SESSION_ABSOLUTE_HOURS` | `12` | |
| `LANVEX_COOKIE_SECURE` | `true` in production | When true, sign-in over plain HTTP is refused with an explanation |
| `LANVEX_TRUSTED_PROXIES` | `[]` | JSON list of CIDRs whose `X-Forwarded-For` / `X-Forwarded-Proto` are believed. A proxy on the Docker host connects from the network gateway: `["172.31.250.1/32"]` with the default subnet |
| `LANVEX_LOGIN_RATE_LIMIT` / `_WINDOW_SECONDS` | `10` / `300` | Per client IP and username |
| `LANVEX_WRITE_RATE_LIMIT` / `_WINDOW_SECONDS` | `120` / `60` | Per user, all writes |
| `LANVEX_LDAP_*` | — | Sign-in with Active Directory or LDAP: [sign-in.md](sign-in.md) |
| `LANVEX_OIDC_*` | — | Single sign-on with OpenID Connect: [sign-in.md](sign-in.md) |

## Server management

| Variable | Default | Notes |
|---|---|---|
| `LANVEX_SECRET_KEY` | — | Required to add servers and switches. Encrypts stored SSH keys, passwords and SNMP credentials (32+ characters; `lanvex generate-secret-key`) |
| `LANVEX_SECRET_KEY_FALLBACKS` | `[]` | Previous keys, as a JSON list, while rotating. Run `lanvex rotate-credentials`, then remove them |
| `LANVEX_SERVER_ALLOWED_NETWORKS` | `[]` | JSON list of CIDRs. When set, Lanvex refuses to scan or connect to servers and switches outside them (names are resolved first) |
| `LANVEX_SSH_CONNECT_TIMEOUT_SECONDS` | `10` | Connection and login |
| `LANVEX_SSH_COMMAND_TIMEOUT_SECONDS` | `15` | Per command |
| `LANVEX_SERVER_READ_INTERVAL_MINUTES` | `60` | How often servers are read automatically; `0` turns scheduled reads off. Each server can override it on its page (15 minutes to daily, or off) |
| `LANVEX_READ_SCHEDULER_ENABLED` | `true` | Run the scheduler in this process. Safe with several processes: each due server is read once |
| `LANVEX_DISCOVERY_RETENTION_DAYS` | `30` | How long a host seen in a server's neighbour table, or a MAC address on a switch port, stays listed after it was last seen |
| `LANVEX_SWITCH_READ_INTERVAL_MINUTES` | `15` | How often switches are read over SNMP; `0` turns scheduled switch reads off. Each switch can override it ([switches.md](switches.md)) |
| `LANVEX_SNMP_TIMEOUT_SECONDS` | `2` | Per SNMP request |
| `LANVEX_SNMP_RETRIES` | `1` | Per SNMP request |
| `LANVEX_SERVER_SNAPSHOT_RETENTION` | `200` | Different states kept per server (its history). Reads that find nothing new do not add one |

Back up `LANVEX_SECRET_KEY` with the database. Without it, stored credentials cannot be decrypted
and every server has to be given new credentials.

## Server configuration changes

See [changes.md](changes.md) for how changes work and how to set up a server for them.

| Variable | Default | Meaning |
|---|---|---|
| `LANVEX_AUTOMATION_ENABLED` | `false` | Master switch. While `false`, Lanvex only reads servers and NetBox: no change can be prepared, approved or run, and approved changes that have not started end as *Not applied* |
| `LANVEX_CHANGE_ROLLBACK_SECONDS` | `120` | 30–900. How long the server waits for Lanvex to confirm a change before restoring its previous configuration on its own. Verification must finish within this time |
| `LANVEX_CHANGES_REQUIRE_SECOND_ADMIN` | `false` | When `true`, the administrator who prepared a change cannot approve it |
| `LANVEX_CHANGE_RUNNER_ENABLED` | `true` | Run approved changes in this Lanvex process. Leave on; it exists so tests can drive the runner themselves |

Each server must also have changes turned on (on its page) and the change helper installed.
