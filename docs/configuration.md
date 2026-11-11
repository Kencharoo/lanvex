# Configuration

All settings are environment variables prefixed `LANVEX_`. In Docker they come from `.env`; locally,
`backend/.env` is also read. Secrets are never logged or returned by the API.

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
| `LANVEX_HTTP_PORT` | `8080` | Docker Compose only. Host port; prefix an address to limit it, e.g. `127.0.0.1:8080` behind a proxy on the same host |
| `LANVEX_DOCKER_SUBNET` | `172.31.250.0/24` | Docker Compose only. Subnet of the `lanvex` network; its `.1` is the address a proxy on the host connects from. Change it if it overlaps a network you use |

## NetBox

| Variable | Default | Notes |
|---|---|---|
| `LANVEX_NETBOX_URL` | — | Base URL without `/api`, e.g. `https://netbox.example.net` |
| `LANVEX_NETBOX_TOKEN` | — | v2 (`nbt_<key>.<secret>`, sent as `Bearer`) or v1 (sent as `Token`) |
| `LANVEX_NETBOX_VERIFY_TLS` | `true` | Only disable for lab instances with self-signed certificates |
| `LANVEX_NETBOX_TIMEOUT_SECONDS` | `10` | Per request |
| `LANVEX_NETBOX_MAX_RETRIES` | `2` | Read requests only. Writes are never retried automatically |

Without URL and token Lanvex still starts; NetBox-backed pages show how to connect.

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

## Server management

| Variable | Default | Notes |
|---|---|---|
| `LANVEX_SECRET_KEY` | — | Required to add servers. Encrypts stored SSH keys and passwords (32+ characters; `lanvex generate-secret-key`) |
| `LANVEX_SECRET_KEY_FALLBACKS` | `[]` | Previous keys, as a JSON list, while rotating. Run `lanvex rotate-credentials`, then remove them |
| `LANVEX_SERVER_ALLOWED_NETWORKS` | `[]` | JSON list of CIDRs. When set, Lanvex refuses to scan or connect to addresses outside them (names are resolved first) |
| `LANVEX_SSH_CONNECT_TIMEOUT_SECONDS` | `10` | Connection and login |
| `LANVEX_SSH_COMMAND_TIMEOUT_SECONDS` | `15` | Per command |
| `LANVEX_SERVER_SNAPSHOT_RETENTION` | `20` | Reads kept per server |

Back up `LANVEX_SECRET_KEY` with the database. Without it, stored credentials cannot be decrypted
and every server has to be given new credentials.

## Automation (phase 2)

`LANVEX_AUTOMATION_ENABLED` defaults to `false`. No feature yet reads it as permission to change
infrastructure; it exists so that the future switch is explicit and off by default.
