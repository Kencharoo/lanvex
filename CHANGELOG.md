# Changelog

## 0.4.1 — 2026-11-14

Fixes from the first installation on a real server (Ubuntu 24.04, Docker Compose v2).

- **First start no longer crashes.** Both web workers created the first administrator at the same
  moment; the second failed and stopped Lanvex until Docker restarted it. A database lock now makes
  the others wait.
- **`core-worker` reports its real health.** It inherited the web application's health check and
  turned *unhealthy* after ten minutes although it worked. The check now looks for the worker's
  registration in Redis.
- **Installer: choose the port.** `--port [ADDRESS:]PORT` and a question for it; the installer checks
  that the port is free before writing anything. If starting fails, it says where the first
  administrator's password is and how to continue.
- **No more alarming lines during installation and start:** the pull error for `lanvex-core:local`,
  gunicorn's "Control server error: Permission denied", and the `useradd` warning about uid 1000.
- **Consistent log times:** Lanvex Core's containers log in the configured time zone, with the UTC
  offset on every line.

Updating from 0.4.0: see [docs/core.md](docs/core.md#updating). Nothing in `.env` changes.

## 0.4.0 — 2026-11-13

- **Lanvex Core**: NetBox 4.7.5 with Lanvex's branding and single sign-on, installed and set up
  together with Lanvex, reachable only through Lanvex at `/core/`.
- **Installer** (`install.sh`): Lanvex Core or an existing NetBox, HTTPS proxy or plain HTTP, every
  secret generated.
- Fix: a network's detail page failed on a NetBox that had no `lanvex-gateway` tag yet.

Earlier versions: 0.3 changed server network configuration with rollback on the server; 0.2 read
servers over SSH; 0.1 was the console for networks, addresses, VLANs and devices.
