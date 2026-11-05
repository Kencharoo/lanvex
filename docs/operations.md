# Operations

## Production checklist

- Serve Lanvex over HTTPS through a reverse proxy, and set `LANVEX_TRUSTED_PROXIES` to the proxy's address
  so audit entries and rate limits see real client IPs.
- Use a dedicated NetBox user and token for Lanvex with only the permissions below.
- Set strong values for `LANVEX_POSTGRES_PASSWORD` and the bootstrap admin password; remove the bootstrap
  variables after the first start.
- Back up the `lanvex-db` volume. It holds Lanvex users and the audit log. Network data lives in NetBox.
- Keep `LANVEX_AUTOMATION_ENABLED=false`.

### Reverse proxy (Caddy example)

```
lanvex.example.net {
    reverse_proxy 127.0.0.1:8080
}
```

With `LANVEX_TRUSTED_PROXIES=["127.0.0.1/32"]` (or your proxy's network) Lanvex honours
`X-Forwarded-For` and `X-Forwarded-Proto`.

## NetBox token permissions

Create a NetBox user (for example `lanvex`) and grant object permissions:

| Objects | Actions |
|---|---|
| IPAM: prefixes, IP addresses, VLANs | view, add, change, delete |
| DCIM: interfaces | view, change (Lanvex only edits 802.1Q mode and VLANs) |
| Extras: tags | view, add (Lanvex creates the `lanvex-gateway` tag once) |
| IPAM: VRFs, roles, VLAN groups, IP ranges; DCIM: devices, sites, device roles, manufacturers; Tenancy: tenants | view |
| Core: object changes | view (optional, for the overview's change feed) |

A read-only token works too; write actions then fail with NetBox's permission message, and Lanvex
records the failure in its audit log.

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
| TLS errors to NetBox | Install the CA in the image, or `LANVEX_NETBOX_VERIFY_TLS=false` for lab instances only |
