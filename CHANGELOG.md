# Changelog

## 0.6.0 — 2026-11-15

Keeping documentation and servers in step: what changed on a server, where it differs from Lanvex
Core, closing each difference in either direction, and what servers see around them.

- **Server history.** Lanvex reads each server every hour (configurable, per server too) and keeps
  each state the server was in. The server page lists what changed between them in words: addresses
  added or removed, links lost, MTU, gateway, routes, DNS. Reads that find nothing new are counted
  instead of stored, so a year of hourly reads of a quiet server is one entry.
- **Closing differences with NetBox.** Each address where the server and NetBox differ can be
  - recorded in NetBox: a preview of exactly which NetBox updates are needed (create the address on
    the right interface, add the interface if the device lacks it, correct a prefix length, unassign
    an address the server no longer has), then those updates and no others; or
  - changed on the server: the change form opens with that difference undone, followed by the usual
    preview, approval, verification and automatic rollback.
- After a successful change, its page says whether NetBox documents the result and offers to record it.
- **Discovery.** A new page lists what servers see around them, compared with NetBox:
  - hosts in their neighbour tables (kept for 30 days after last seen), and which of them NetBox lacks;
    those can be recorded as unassigned addresses after a preview;
  - devices announcing themselves over LLDP on each server interface (from systemd-networkd or lldpd),
    compared with the documented cable, through patch panels. A missing cable can be recorded.
  The server page shows each interface's LLDP neighbour, and the history notes when it changes.
  docs/operations.md explains how to let Lanvex read LLDP.
- `LANVEX_SERVER_SNAPSHOT_RETENTION` now counts different states and defaults to 200 (was 20 reads).
- New settings `LANVEX_SERVER_READ_INTERVAL_MINUTES` (default 60, `0` = off) and
  `LANVEX_READ_SCHEDULER_ENABLED`, `LANVEX_DISCOVERY_RETENTION_DAYS` (default 30). Recording an interface the device lacks needs *add* on interfaces
  for your own NetBox's token, recording a cable *add* on cables (docs/operations.md). Lanvex Core
  0.6.0 gives its service account exactly that.
- The SSH lab (three real Ubuntu servers with netplan, NetworkManager and ifupdown) now runs in CI.

## 0.5.0 — 2026-11-15

- **Topology.** The map of your network that was a placeholder until now: devices, sites, provider
  circuits and the cables between them, as documented in Lanvex Core or NetBox.
  - Layout by role: circuits on top, then core, distribution, access, patch panels, servers and other
    end devices. Cables between devices of the same rank are drawn side by side; servers and other
    devices on a single cable are stacked under their switch, so large sites stay compact.
  - One site or all sites (each in its own frame); devices at the far end of a cable from another site
    are shown and marked.
  - Search, a details panel with every cable of a device, links to the device, its server in Lanvex
    and its page in Lanvex Core, and a cable list for reading everything as a table.
  - Planned cables are dashed; devices Lanvex reads over SSH are marked.
  - Fast with hundreds of devices: the layout runs in the background, and the map's code loads only when
    the page is opened.
- With your own NetBox, the token needs *view* on cables for the map (docs/operations.md).
- **Fix:** two quick changes to a page's filters (switching a view and then choosing a site, say)
  kept only the second. Affected every filtered page.
- CI keeps Playwright traces of failed browser tests for 14 days.

## 0.4.2 — 2026-11-14

- **Fix:** 0.4.1's new health check reported Lanvex Core *healthy* while it was still setting up its
  database, so the installer finished before Lanvex Core answered. Found by the first automatic full
  installation; 0.4.0 was not affected.
- Lanvex is licensed under the Apache License 2.0 (`LICENSE`, `NOTICE`).
- `scripts/check-install.sh` checks a running installation: containers, logs, sign-in, Lanvex Core.
- Continuous integration for both repositories, including a full installation with `install.sh`
  ([docs/development.md](docs/development.md#continuous-integration)).

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
