# Operations

## Production checklist

`install.sh` takes care of the passwords, keys and port settings below; the rest is up to you.

- Serve Lanvex over HTTPS through a reverse proxy, and set `LANVEX_TRUSTED_PROXIES` to the address the
  proxy connects from (see below), so Lanvex recognises HTTPS and logs real client IPs.
- Use a dedicated NetBox user and token for Lanvex with only the permissions below.
- Set strong values for `LANVEX_POSTGRES_PASSWORD` and the bootstrap admin password; remove the bootstrap
  variables after the first start.
- Back up the `lanvex-db` volume. It holds Lanvex users and the audit log. Network data lives in NetBox
  or, with Lanvex Core, in the `core-db` and `core-media` volumes ([core.md](core.md#backups)).
  Keep `.env` with the backups.
- Keep `LANVEX_AUTOMATION_ENABLED=false` unless you change servers through Lanvex. If you do, read
  [changes.md](changes.md) first and turn changes on only for the servers that need them.
- Set `LANVEX_SECRET_KEY` and store it with your database backups. Consider `LANVEX_SERVER_ALLOWED_NETWORKS`.

### Reverse proxy (Caddy on the same host)

```
lanvex.example.net {
    reverse_proxy 127.0.0.1:8080
}
```

In `.env`:

```
LANVEX_HTTP_PORT=127.0.0.1:8080
LANVEX_TRUSTED_PROXIES=["172.31.250.1/32"]
```

The first line keeps port 8080 off the network, so only the proxy reaches Lanvex. The second is
needed because a proxy on the host reaches the container from the Docker network's gateway, not from
`127.0.0.1`; without it Lanvex does not see that the browser used HTTPS and refuses sign-in. The
gateway is the `.1` of `LANVEX_DOCKER_SUBNET` (default `172.31.250.0/24`). For a proxy on another
machine, trust that machine's address instead and leave the port reachable from it.

For a name only used inside your network, add `tls internal` to the site block; Caddy then signs the
certificate with its own CA, which browsers need to trust (its root certificate is in Caddy's data
directory under `pki/authorities/local/root.crt`).

## Podman

The installer uses Podman when Docker is not there (or not running), with `podman-compose` or
`podman compose`. Rootless works: run `lanvexctl install` (or `./install.sh`) as the user who should own the containers, without
sudo. Notes:

- **Start at boot.** Podman has no daemon that restarts containers after a reboot. The installer
  writes a systemd unit, `lanvex.service`: a system unit when run as root, otherwise a user unit and
  `loginctl enable-linger`, so it starts without anyone signing in. If lingering could not be enabled,
  the installer says so; run `sudo loginctl enable-linger <user>` once. To add the unit by hand (an
  installation from before 0.6.1, or after the installer could not), as the user who owns the
  containers, in the installation directory:

  ```bash
  mkdir -p ~/.config/systemd/user
  cat > ~/.config/systemd/user/lanvex.service <<EOF
  [Unit]
  Description=Lanvex
  Wants=network-online.target
  After=network-online.target

  [Service]
  Type=oneshot
  RemainAfterExit=yes
  WorkingDirectory=$PWD
  ExecStart=$(command -v podman-compose) up -d
  ExecStop=$(command -v podman-compose) stop
  TimeoutStartSec=0

  [Install]
  WantedBy=default.target
  EOF
  systemctl --user daemon-reload && systemctl --user enable lanvex.service
  sudo loginctl enable-linger "$USER"
  ```

  A unit you wrote yourself before can stay; it should not run next to this one.
- **Commands.** Where these docs say `docker compose`, use `podman-compose` (or `podman compose`).
- **Health checks** run from systemd timers. Without a systemd session (some containers and CI
  machines) `podman healthcheck run <container>` runs one by hand; the installer does this itself.
- **Reverse proxy.** With rootless Podman, a proxy on the same host may not reach Lanvex from
  `172.31.250.1`. Try to sign in once through the proxy: when the address is not trusted, Lanvex
  refuses and logs `sign_in_refused_without_https` with the `peer` it came from
  (`podman-compose logs lanvex | grep sign_in_refused`). Put that address into
  `LANVEX_TRUSTED_PROXIES`.
- **Lanvex Core restarts again and again**, with `PermissionError` on `netbox/static` in
  `podman-compose logs core`: the volume for its static files belongs to the wrong user (installations
  of 0.6.0 under Podman 4). Lanvex Core rebuilds these files at every start, so the volume can go:
  `podman-compose down`, `podman volume rm lanvex_core-static`, `podman-compose up -d`.
- **Addresses in the audit log.** With rootless Podman, every request reaches Lanvex from one internal
  address (the container network's, such as `10.89.5.4`), not the browser's, so the audit log shows
  that address for everyone. Browser confirmation of changes is not affected (it uses the address the
  browser opened). To record real addresses, publish Lanvex only on `127.0.0.1`
  (`LANVEX_HTTP_PORT=127.0.0.1:8100`), put a reverse proxy on the host in front of it that sets
  `X-Forwarded-For`, and trust that internal address in `LANVEX_TRUSTED_PROXIES`. Never trust it while
  Lanvex's port is open to the network: anyone could then claim any address.
- **Changing the server Lanvex runs on.** A rootless container cannot reach its own host's LAN
  address. Lanvex can read that host through `host.containers.internal`, but not change it
  ([changes.md](changes.md#when-lanvex-reaches-the-server-indirectly)).

## NetBox token permissions

With Lanvex Core nothing needs doing: its `lanvex-service` account gets exactly these permissions on
every start. For your own NetBox, create a user (for example `lanvex`) and grant object permissions:

| Objects | Actions |
|---|---|
| IPAM: prefixes, IP addresses, VLANs, IP ranges | view, add, change, delete (IP ranges: only to change for [networks](networks.md), otherwise view) |
| DCIM: interfaces | view, change (802.1Q mode and VLANs), add and delete (optional: recording a server interface the device lacks, adding and removing [networks](networks.md)) |
| Extras: tags | view, add (Lanvex creates the `lanvex-gateway` and `lanvex-dhcp` tags once) |
| IPAM: VRFs, roles, VLAN groups; DCIM: sites, device roles, manufacturers; Tenancy: tenants | view |
| DCIM: devices; Virtualization: virtual machines | view; change (optional: recording a server's DNS servers, search domains and static routes in its local config context data) |
| Core: object changes | view (optional, for the overview's change feed) |
| DCIM: cables | view (optional, for the topology map); add (optional, recording a cable seen over LLDP) |
| Virtualization: VM interfaces | view (optional, to link servers to VMs); add (optional, as for devices) |

A read-only token works too; write actions then fail with NetBox's permission message, and Lanvex
records the failure in its audit log. [Networks](networks.md#your-own-netbox-instead-of-lanvex-core)
also need two custom fields; Lanvex Core creates them itself.

## Adding a server

Reading a server needs no privileges. Changing its network configuration (optional, off by default)
additionally needs a small helper installed as root; see [changes.md](changes.md). On each server:

1. Create an unprivileged account for Lanvex (no sudo needed): `sudo useradd -m -s /bin/sh lanvex`.
2. In Lanvex, choose **Servers → Add server**. Lanvex fetches the server's SSH host key and shows its
   fingerprint. Compare it with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` run on the server
   through a console you already trust, then confirm.
3. Let Lanvex create a key (recommended). It shows a `restrict ssh-ed25519 …` line; add it to
   `~lanvex/.ssh/authorized_keys`. `restrict` disables forwarding and terminals for that key.
4. Test the connection, then read the network state.

**Appliances and NAS systems** (ZimaOS, TrueNAS, Synology and the like) often have a read-only root
file system, so `useradd` fails. Use an existing administrator account there, and put the key into
that account's home directory, whatever it is (`echo ~` shows it; on ZimaOS it is `/DATA`). If
`~/.ssh` does not exist yet, create it as that user, not with sudo, or it belongs to root and SSH
ignores it: `mkdir -m 700 ~/.ssh`. Paste the key line in one piece; a terminal that wraps long lines
can break it when copied. `cat >> ~/.ssh/authorized_keys`, paste, Enter, Ctrl+D is safer than a long
`echo`. Then `chmod 600 ~/.ssh/authorized_keys`.

If a server's host key changes (after a reinstall, or if something intercepts the connection),
Lanvex stops connecting and says so. Verify the new fingerprint the same way before trusting it.

Lanvex reads each server every hour by default (`LANVEX_SERVER_READ_INTERVAL_MINUTES`; each server
can be set to 15 minutes to daily, or off, on its page). The server page's **History** lists each
state the server was found in and what changed, in words: addresses, links, MTU, gateway, routes,
DNS. A read that finds the same state is counted, not stored again; failed reads are listed too.

Scheduled reads stop by themselves after two failed sign-ins in a row, or at once when the server
refuses Lanvex's address ("Refused for now"). Every failed attempt counts against the server's
protection against password guessing: OpenSSH 9.8 and newer (`PerSourcePenalties`) and fail2ban then
block Lanvex's address, and behind NAT every other machine's with it. Fix the cause and read the
server once by hand; scheduled reads resume after a successful read. An OpenSSH block ends by itself
after a while; `sudo systemctl restart ssh` (or `sshd`) on the server ends it at once.

The commands Lanvex runs to read a server are fixed and read-only: `ip -json` (addresses, routes,
neighbours), `cat` of `/etc/os-release`, `/etc/resolv.conf` and `/etc/network/interfaces[.d]`,
`systemctl is-active`, `ls /etc/netplan`, and `nmcli`/`networkctl`/`resolvectl`/`lldpcli` status when
those services run. The
contents of `/etc/network/interfaces` are parsed but not stored, since they can contain secrets.
For changes, the only other command is `sudo -n /usr/local/libexec/lanvex/lanvex-netapply
<plan|apply|verify|confirm|rollback|status|version>`, with a JSON request on standard input.

## Discovery

Every read also notes what the server sees around it. **Discovery** lists it, compared with NetBox:

- **Hosts seen**: the server's neighbour table (`ip neigh`: hosts it exchanged packets with recently).
  Kept for `LANVEX_DISCOVERY_RETENTION_DAYS` after they were last seen. Hosts NetBox lacks can be
  recorded as unassigned IP addresses, after a preview. A Lanvex server's own address is recorded from
  its server page instead, where it is assigned to the right interface.
- **Connections seen over LLDP**: what the switch (or other device) on each interface announces, compared
  with the cable NetBox documents there (through patch panels, the far end). A missing cable can be
  recorded when both interfaces exist in NetBox and neither has a cable. An interface that hears more
  than one device is a shared segment and gets no cable.

LLDP needs a receiver on the server. Either:

- **systemd-networkd** (Ubuntu Server with netplan): receives LLDP from switches and routers by default,
  and anyone may read it. Nothing to do.
- **lldpd**: `apt install lldpd` (or `dnf install lldpd`). Lanvex asks it with `lldpcli`, which only
  members of one group may run (`stat -c %G /usr/sbin/lldpcli`; `adm` on Debian and Ubuntu):
  `sudo usermod -aG adm lanvex`. Note that `adm` also lets the account read system logs. Without it,
  the server page says that lldpd runs but Lanvex may not ask it.

## Users

```bash
docker compose run --rm lanvex create-user alice --role admin     # prompts for a password
docker compose run --rm lanvex check-netbox                        # connection diagnostics
```

Administrators can also add, demote, deactivate and (once deactivated) delete users under Settings.
Lanvex keeps at least one active administrator, and nobody can deactivate or delete their own account
there. For a temporary account, finish on the command line (the audit log keeps its name):

```bash
docker compose run --rm lanvex deactivate-user claude-tmp   # or: delete-user claude-tmp
```

## Health

- `GET /api/v1/health/live` — process is up (used by the container health check)
- `GET /api/v1/health/ready` — database (and Redis, if configured) reachable; 503 when the database is not

NetBox reachability is reported in the UI and by `lanvex check-netbox`, not as a readiness gate, so a
NetBox outage does not take Lanvex down.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Sign-in says sessions are HTTPS-only | Production mode over plain HTTP. Use HTTPS, or `LANVEX_COOKIE_SECURE=false` on a trusted network |
| "NetBox redirected the request" | `LANVEX_NETBOX_URL` has the wrong scheme or is missing a path prefix (e.g. `/netbox`) |
| "NetBox denied the request" | Token invalid, expired, IP-restricted, or missing the object permission for that action |
| NetBox unreachable from the container | Use a URL that resolves inside Docker (not `localhost`); see deploy/netbox |
| "The change may or may not have been applied" | NetBox did not answer a write in time. Refresh and check before retrying; Lanvex never retries writes |
| Utilization differs slightly from NetBox | Duplicate IP records for the same host are counted once by NetBox and per record by Lanvex |
| "Set LANVEX_SECRET_KEY to store server credentials" | Generate one with `lanvex generate-secret-key`, set it, restart |
| Server shows "Host key changed" | Reinstalled server, or interception. Verify the new fingerprint on the server, then *Verify a new host key* |
| Server shows "Sign-in failed" | Key not in `authorized_keys`, wrong user, or `AllowUsers`/`PubkeyAuthentication` in sshd_config |
| "Stored credentials could not be decrypted" | `LANVEX_SECRET_KEY` changed. Put the old key in `LANVEX_SECRET_KEY_FALLBACKS` |
| Lanvex Core: not answering, could not sign you in, pages without styling | See the troubleshooting table in [core.md](core.md#troubleshooting) |
| TLS errors to NetBox | Install the CA in the image, or `LANVEX_NETBOX_VERIFY_TLS=false` for lab instances only |
| Server changes: helper *Not allowed through sudo*, *Python 3 missing*, *Outdated version*; a change *Needs attention* | See the troubleshooting table in [changes.md](changes.md) |
