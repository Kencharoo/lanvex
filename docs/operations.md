# Operations

## Production checklist

- Serve Lanvex over HTTPS through a reverse proxy, and set `LANVEX_TRUSTED_PROXIES` to the address the
  proxy connects from (see below), so Lanvex recognises HTTPS and logs real client IPs.
- Use a dedicated NetBox user and token for Lanvex with only the permissions below.
- Set strong values for `LANVEX_POSTGRES_PASSWORD` and the bootstrap admin password; remove the bootstrap
  variables after the first start.
- Back up the `lanvex-db` volume. It holds Lanvex users and the audit log. Network data lives in NetBox.
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

## NetBox token permissions

Create a NetBox user (for example `lanvex`) and grant object permissions:

| Objects | Actions |
|---|---|
| IPAM: prefixes, IP addresses, VLANs | view, add, change, delete |
| DCIM: interfaces | view, change (Lanvex only edits 802.1Q mode and VLANs) |
| Extras: tags | view, add (Lanvex creates the `lanvex-gateway` tag once) |
| IPAM: VRFs, roles, VLAN groups, IP ranges; DCIM: devices, sites, device roles, manufacturers; Tenancy: tenants | view |
| Core: object changes | view (optional, for the overview's change feed) |
| Virtualization: virtual machines | view (optional, to link servers to VMs) |

A read-only token works too; write actions then fail with NetBox's permission message, and Lanvex
records the failure in its audit log.

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

If a server's host key changes (after a reinstall, or if something intercepts the connection),
Lanvex stops connecting and says so. Verify the new fingerprint the same way before trusting it.

The commands Lanvex runs to read a server are fixed and read-only: `ip -json` (addresses, routes), `cat` of
`/etc/os-release`, `/etc/resolv.conf` and `/etc/network/interfaces[.d]`, `systemctl is-active`,
`ls /etc/netplan`, and `nmcli`/`networkctl`/`resolvectl` status when those services run. The
contents of `/etc/network/interfaces` are parsed but not stored, since they can contain secrets.
For changes, the only other command is `sudo -n /usr/local/libexec/lanvex/lanvex-netapply
<plan|apply|verify|confirm|rollback|status|version>`, with a JSON request on standard input.

## Users

```bash
docker compose run --rm lanvex create-user alice --role admin     # prompts for a password
docker compose run --rm lanvex check-netbox                        # connection diagnostics
```

Administrators can also add, demote and deactivate users under Settings. Lanvex keeps at least one
active administrator.

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
| TLS errors to NetBox | Install the CA in the image, or `LANVEX_NETBOX_VERIFY_TLS=false` for lab instances only |
| Server changes: helper *Not allowed through sudo*, *Python 3 missing*, *Outdated version*; a change *Needs attention* | See the troubleshooting table in [changes.md](changes.md) |
