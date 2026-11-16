# Lanvex Core

Lanvex Core is the application where your network is documented: sites, racks, devices, cables,
IP addresses, VLANs and everything else NetBox models. It is built on
[NetBox](https://github.com/netbox-community/netbox) 4.7.5 (Apache License 2.0) and installed with
Lanvex, so a new installation needs no separate NetBox. Lanvex shows this data in its own pages,
compares it with what your servers actually run, and links into Lanvex Core for everything else.

Lanvex Core is NetBox with Lanvex's name, colours and typeface, single sign-on from Lanvex, and the
settings Lanvex needs. It is not NetBox and is not supported by NetBox Labs. NetBox's documentation
applies to everything inside it.

Using your own NetBox instead remains fully supported; the installer asks which one you want.

## Installing

```bash
sudo ./install.sh          # choose "1) In Lanvex Core, installed now"
```

or without questions: `sudo ./install.sh --core --behind-proxy --admin admin`. The installer
generates every password and key, writes them into `.env`, builds both applications and waits until
both are healthy. The first start of Lanvex Core sets up its database, which takes several minutes;
the installer allows 20.

Release archives contain the Lanvex Core source in `./core`. In a git checkout, run
`scripts/fetch-core.sh` first; it puts exactly the version named in `core.lock` there.

### What runs

| Container | Purpose | Data |
|---|---|---|
| `core` | Lanvex Core itself (port 8080 inside Docker only). Every start applies database migrations, collects static files and sets up Lanvex's service account | `core-media` (uploaded images and files), `core-static` |
| `core-worker` | Background jobs (reports, scripts, bulk operations) | — |
| `core-db` | PostgreSQL for Lanvex Core | `core-db` |
| `core-redis` | Job queue and cache | `core-redis` |

They sit on their own Docker network, `lanvex-core`, which is internal: none of them publishes a port
or can reach the internet or your network. Only the `lanvex` container joins it. Lanvex Core's own
secrets (database password, secret key, token pepper) are given to the Lanvex Core containers only.

## Signing in and roles

There is one sign-in: Lanvex's. Choose **Lanvex Core** in Lanvex's menu, or open `/core/` on the
Lanvex address. Without a Lanvex session you are sent to Lanvex's sign-in page and then on to the
page you wanted. Inside Lanvex Core, **Back to Lanvex** at the top returns, and **Log out** signs you
out of Lanvex.

| Lanvex role | In Lanvex Core |
|---|---|
| Administrator | Superuser: may view and change everything, including Lanvex Core's own administration pages |
| Read-only | May view all network data; cannot change anything and cannot see users, groups or permissions |

A user's Lanvex Core account is created on their first visit, with their Lanvex display name. A role
changed in Lanvex applies on the user's next page in Lanvex Core. A deactivated Lanvex user can no
longer open Lanvex Core, because every request needs a valid Lanvex session.

Lanvex Core's own login page is switched off. Its REST API and GraphQL are reachable through Lanvex
with a Lanvex session (for example the API browser at `/core/api/`), but not with API tokens:
Lanvex removes `Authorization` headers. Scripts sign in like a browser; see *Scripting* below.

### Scripting

A script signs in to Lanvex, keeps the cookies, and sends the CSRF token with every write:

```bash
url=https://lanvex.example.net
curl -sS -c jar -H 'Content-Type: application/json' \
  -d '{"username": "admin", "password": "…"}' "$url/api/v1/auth/login" >/dev/null
curl -sS -b jar -c jar "$url/core/api/status/" >/dev/null        # sets lanvex_core_csrftoken
csrf=$(awk '$6 == "lanvex_core_csrftoken" {print $7}' jar)
curl -sS -b jar -H "X-CSRFToken: $csrf" -H "Referer: $url/core/" -H 'Content-Type: application/json' \
  -d '{"name": "Lab", "slug": "lab"}' "$url/core/api/dcim/sites/"
```

- The cookie is `lanvex_core_csrftoken`, not Django's usual `csrftoken`; every API response to a
  signed-in session sets it (Lanvex Core 0.6.1 and newer; before that, only pages with a form).
- Writes need the header `X-CSRFToken`, and over HTTPS a `Referer` on the same site.
- The script acts as the Lanvex user it signed in as, with that user's role.
- Lanvex's own API (`/api/v1/…`, documented at `/api/docs`) works with the same session and needs
  the `X-CSRF-Token` header with the `csrf_token` the sign-in returned.

## How it fits together

```
 Browser ──/core/*──► Lanvex ──► Lanvex Core (core:8080)       pages and forms, as the signed-in user
                        │
                        └──────► Lanvex Core /core/api/          Lanvex's own pages, as lanvex-service
```

- **People** reach Lanvex Core only through Lanvex. For each request Lanvex checks the session, then
  adds the user's name, display name and role (`X-Lanvex-User`, `X-Lanvex-Display-Name`,
  `X-Lanvex-Groups`) and a shared secret (`X-Lanvex-Proxy-Secret`). Lanvex Core trusts those headers
  only with the right secret. Anything a browser sends under these names is removed first, as are
  Lanvex's session cookie, `Authorization` and forwarding headers. Static files (CSS, JavaScript)
  are served by Lanvex from the shared `core-static` volume.
- **Lanvex itself** uses Lanvex Core's REST API like any NetBox, as the service account
  `lanvex-service` with a token from `.env`. That account holds only the permissions Lanvex needs
  (listed in [operations.md](operations.md#netbox-token-permissions)), cannot sign in to the web
  interface, and cannot be impersonated through the headers above. Every start of the `core`
  container sets the account, its permissions and its token back to exactly that.
- **Links** from Lanvex into Lanvex Core (*Open in Lanvex Core*) point to `/core/…` on the Lanvex
  address (`LANVEX_NETBOX_PUBLIC_URL`), never to the internal `core:8080`.

As with any NetBox, what is in Lanvex Core is documentation: the desired state. Recording an address
there configures nothing on a device or server.

## Settings

The installer writes these into `.env`; [configuration.md](configuration.md#lanvex-core) describes
each one. Change them only together with the matching value:

| Change | How |
|---|---|
| Service token | Set the same new value in `LANVEX_NETBOX_TOKEN` and `LANVEX_CORE_SERVICE_TOKEN` (`nbt_` + 12 + `.` + 40 letters or digits), then `docker compose up -d`. The old token is removed |
| Proxy secret | New value (32+ characters) in `LANVEX_CORE_PROXY_SECRET`, then `docker compose up -d`. Everyone signs in to Lanvex Core again automatically |
| Lanvex Core secret key or token pepper | New value, then `docker compose up -d`. Lanvex Core sessions end (people are signed in again automatically); the service token is recreated |
| Database password | Not by editing `.env` alone: PostgreSQL keeps the password it was created with. Change it in the database first (`ALTER ROLE lanvex_core PASSWORD '…'`), then in `.env` |
| Time zone | `LANVEX_CORE_TIME_ZONE`, e.g. `Europe/Berlin` |

## Operations

### Logs and status

`scripts/check-install.sh` checks a whole installation at once: every container healthy and never
restarted, no errors in the logs, sign-in, the connection to Lanvex Core, and that Lanvex Core is
closed to anyone not signed in. Run it in the directory you installed from; with your username it
asks for your password and includes the sign-in checks: `scripts/check-install.sh "" admin`.

```bash
docker compose ps                          # core and core-worker should be "healthy"
docker compose logs -f core core-worker    # Lanvex Core and its background jobs
```

Lanvex Core logs in `LANVEX_CORE_TIME_ZONE` with the UTC offset on every line; Lanvex logs in UTC.

### Backups

Back up three things together, and keep `.env` with them:

```bash
docker compose exec -T core-db pg_dump -U lanvex_core -Fc lanvex_core > lanvex-core-$(date +%F).dump
docker run --rm -v lanvex_core-media:/media:ro -v "$PWD":/backup alpine \
  tar -czf /backup/lanvex-core-media-$(date +%F).tar.gz -C /media .
```

and Lanvex's own database as described in [operations.md](operations.md). Restoring the Lanvex Core
database into a fresh installation:

```bash
docker compose stop core core-worker
docker compose exec -T core-db pg_restore -U lanvex_core -d lanvex_core --clean --if-exists < lanvex-core-….dump
docker compose up -d
```

### Management commands

Any NetBox `manage.py` command runs through the image's `manage` role:

```bash
docker compose run --rm core manage showmigrations
docker compose run --rm core manage nbshell
```

### Updating

Lanvex and Lanvex Core are released together: each Lanvex release names the Lanvex Core version it
was tested with in `core.lock` and contains it in `./core`. To update:

```bash
# back up first (above), then:
unzip lanvex-0.6.1.zip && cp lanvex-0.6.0/.env lanvex-0.6.1/
cd lanvex-0.6.1 && docker compose up -d --build   # Podman: podman-compose up -d --build
```

The Compose project is always called `lanvex`, so the new directory uses the same containers,
networks and volumes; the old directory can be deleted once everything runs. Under Podman, the
systemd unit that starts Lanvex at boot names the directory; point it at the new one first:

```bash
sed -i "s|^WorkingDirectory=.*|WorkingDirectory=$PWD|" ~/.config/systemd/user/lanvex.service
systemctl --user daemon-reload   # as root: /etc/systemd/system/lanvex.service and systemctl daemon-reload
```

An installation from before 0.6.1 has no such unit; [operations.md](operations.md#podman) shows how to
add it. Lanvex Core applies its
database migrations when it starts; that can take a few minutes after a NetBox feature release.

### Switching between Lanvex Core and your own NetBox

The choice is the NetBox section of `.env` plus `COMPOSE_PROFILES=core`. Switching does not move
data between them. Lanvex keeps no copy of network data, so after a switch it shows what the new
source contains.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `core-worker` is *unhealthy* | Its background worker is not registered in Redis: it stopped, hangs, or cannot reach `core-redis`. `docker compose logs core-worker`. (Lanvex 0.4.0 reported it unhealthy even when it worked; update) |
| "Lanvex Core is not answering" | The `core` container is starting (the first start takes minutes) or stopped. `docker compose ps`, `docker compose logs core` |
| "Lanvex Core is not part of this installation" | `LANVEX_CORE_UPSTREAM_URL` and `LANVEX_CORE_PROXY_SECRET` are not set for Lanvex. The installer sets both when you choose Lanvex Core |
| "Lanvex Core could not sign you in" | `docker compose logs core` names the reason. *The proxy secret does not match*: the `lanvex` and `core` containers run with different `LANVEX_CORE_PROXY_SECRET` values; run `docker compose up -d` after any change to `.env`. *The user name is reserved*: a Lanvex user is called `lanvex-service`; rename it |
| "Lanvex Core can only be used through Lanvex" | A Lanvex Core session was used without going through Lanvex. Open Lanvex Core from Lanvex |
| Lanvex Core pages without styling | The `core` container has not yet copied its static files into `core-static`, or that volume is not mounted into `lanvex`. Wait for `core` to be healthy |
| Lanvex says Lanvex Core "denied the request" | The service token differs between `LANVEX_NETBOX_TOKEN` and `LANVEX_CORE_SERVICE_TOKEN`, or `core` has not been restarted since it changed |
| `core` stops with "… must be set" | A Lanvex Core variable is missing in `.env`. Compare with `.env.example` |
| `core` restarts while migrating after an update | Look for the first error in `docker compose logs core`. Restore the backup if a migration failed |

## Known limitations

- **No API tokens for other tools.** Scripts use a Lanvex session (*Scripting* above). Tools that
  only take a NetBox API token (Ansible's NetBox collection, Terraform) cannot use Lanvex Core yet.
- **No outbound connections.** The internal network means webhooks to outside systems, remote data
  sources (Git, S3) and NetBox plugins installed from the internet do not work.
- **Plugins.** Only Lanvex's own package is installed. Adding NetBox plugins means building your own
  image.
- **Moving data from an existing NetBox** into Lanvex Core is not automated. Because Lanvex Core's
  database is NetBox's, NetBox's own backup-and-restore procedure applies to NetBox 4.7.5 and older,
  but this has not been tested.
- **Lanvex Core's own users, groups and permissions** follow Lanvex's two roles. Finer permissions
  inside Lanvex Core (for example, a user who may edit only cables) are not mapped from Lanvex.

## For developers

Lanvex Core lives in its own repository, a fork of NetBox on branch `lanvex`. Its `FORK.md` lists
every change to NetBox's files and how the fork is kept cheap to maintain:

- Lanvex's behaviour is a separate package, `netbox/lanvex_core` (single sign-on, theme, Back to
  Lanvex, service account), loaded through NetBox's plugin mechanism. Configuration is
  `netbox/lanvex_configuration.py`. Five NetBox files are changed (three templates, README, NOTICE),
  each marked "Modified for Lanvex Core". No database migrations of its own.
- `lanvex/scripts/merge-upstream.sh v4.7.6` merges a NetBox release; `lanvex/scripts/test.sh` runs
  lint, the fork check and Lanvex Core's tests (`--full` adds NetBox's whole suite);
  `lanvex/scripts/check-fork.sh` fails when a changed NetBox file is missing from `FORK.md` or lacks
  its notice.

In this repository:

| File | Purpose |
|---|---|
| `core.lock` | The Lanvex Core commit this Lanvex release is built and tested with |
| `scripts/fetch-core.sh` | Puts that commit into `./core` (from `../lanvex-core` or a git URL) |
| `scripts/release.sh` | Builds `lanvex-<version>.zip` from HEAD plus `./core` |
| `backend/lanvex/core_proxy.py` | The `/core/` proxy: sessions, identity headers, static files |
| `frontend/e2e/core.live.spec.ts` | Browser test of sign-in, roles, links and sign-out (opt-in) |

To release a new Lanvex Core: commit and tag it in the fork, put the commit into `core.lock`, run
`scripts/fetch-core.sh`, run the tests below, then `scripts/release.sh`.

Testing against a running Lanvex Core (see [development.md](development.md#lanvex-core)):

```bash
LANVEX_TEST_NETBOX_URL=http://127.0.0.1:8200/core LANVEX_TEST_NETBOX_TOKEN=nbt_… uv run pytest -m netbox
LANVEX_E2E_CORE_URL=http://127.0.0.1:8000 LANVEX_E2E_CORE_PASSWORD=… npx playwright test core.live
```
