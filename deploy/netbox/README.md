# A disposable local NetBox

For development and the live integration tests, use NetBox's community Docker deployment,
[netbox-docker](https://github.com/netbox-community/netbox-docker). It runs entirely separately from
Lanvex; Lanvex only needs its URL and a token.

```bash
git clone -b release https://github.com/netbox-community/netbox-docker.git
cd netbox-docker
cat > docker-compose.override.yml <<'EOF'
services:
  netbox:
    ports:
      - "8000:8080"
EOF
docker compose pull && docker compose up -d
docker compose exec netbox /opt/netbox/netbox/manage.py createsuperuser
```

Open http://localhost:8000, sign in, and create an API token under your user profile.

## Connect Lanvex

- **Lanvex running locally (not in Docker):** `LANVEX_NETBOX_URL=http://localhost:8000`
- **Lanvex in Docker Compose:** `localhost` inside the container is the container itself. Either use
  `LANVEX_NETBOX_URL=http://host.docker.internal:8000` (add `extra_hosts: ["host.docker.internal:host-gateway"]`
  to the `lanvex` service on Linux), or attach the NetBox container to the `lanvex` network and use its
  service name:

  ```bash
  docker network connect lanvex netbox-docker-netbox-1
  # LANVEX_NETBOX_URL=http://netbox-docker-netbox-1:8080
  ```

Then run `docker compose run --rm lanvex check-netbox`.

> Not verified in the environment where Lanvex 0.1 was built (no Docker daemon was available there).
> The steps follow netbox-docker's documented usage.
