# Status of Lanvex 0.2

Last updated 2026-11-11. "Verified" means it was run, not just written.

## Verified

### Against a real NetBox 4.7.4

| What | How |
|---|---|
| Every IPAM, VLAN, device and dashboard operation Lanvex performs | 8 live tests (`pytest -m netbox`) drive the Lanvex API against a disposable NetBox 4.7.4 and check results directly in NetBox |
| Token formats and permissions | Passes with a superuser v2 token, a v1 token, and a least-privilege token holding exactly the permissions in docs/operations.md (including creating the `lanvex-gateway` tag). A write-disabled token is refused with a clear message and the refusal is audited |
| Filters Lanvex relies on | Every filter name checked against NetBox's own filtersets |
| UI | Browser walkthrough against real NetBox: create a network through the wizard, verify the result in NetBox, device detail, overview with NetBox's change log |
| Utilization | Batched per-page IP-range lookup gives identical figures to the per-prefix calculation, including VRF separation |

### Against real SSH servers (deploy/lab)

| What | How |
|---|---|
| Host-key verification | The fingerprint Lanvex shows equals `ssh-keygen -lf` on the server; a regenerated host key is refused until re-verified; a confirmed fingerprint must belong to the key being pinned |
| Sign-in | Lanvex-generated ed25519 key (with `restrict`), uploaded keys (incl. passphrase-protected), password; wrong key, wrong password and unreachable hosts are each reported distinctly |
| Reading network state | 5 lab tests (`pytest -m lab`) and a browser test, on Ubuntu 24.04 hosts under systemd running netplan → systemd-networkd + systemd-resolved, NetworkManager, and ifupdown, as an unprivileged user |
| Documented vs actual | Comparison against real NetBox: match, mask differs, only in NetBox, not documented, recorded on another object, link suggestions; works with the least-privilege token |
| Secrets | Credentials never appear in API responses or audit entries; `/etc/network/interfaces` content is not stored |
| Parsers | Fixtures are real output captured from the three lab hosts |

### Everywhere

| What | How |
|---|---|
| Backend | 150 tests: 137 unit and API (real PostgreSQL, migrations up/down/up with no drift), 8 live NetBox, 5 SSH lab |
| Frontend | Type check, unit tests, production build; 6 Playwright tests in Chrome |

## Not verified yet

| What | Why | How to close it |
|---|---|---|
| Debian and Fedora hosts end to end | Their package mirrors were unreachable from the build environment, so the lab is Ubuntu-only. Detection of both is unit-tested; their network tools are the same upstream projects | Add a Debian 12 and a Fedora 41 host to the lab, or run Lanvex against one |
| IPv6, VLAN and bond interfaces on servers | The lab kernel has no IPv6, 8021q or bonding | Parsers are covered by constructed samples in iproute2's JSON shape; confirm on a real host |
| Docker image build and Compose stack | No Docker Hub access from the build environment | `docker compose up -d --build` |
| NetBox versions other than 4.7.4 | | Run the live suite against each supported version (4.2+) |

## Not built yet

- **Changing server configuration (phase 2b).** Static addresses, gateways, DNS, secondary
  addresses and VLAN interfaces through NetworkManager and netplan, each with validation, preview,
  approval, pre-change checkpoint, execution, connectivity verification and automatic rollback.
  Needs a background job runner and an explicit opt-in (`LANVEX_AUTOMATION_ENABLED`).
- Scheduled reads of servers (today reads happen on request).
- Topology, discovery, drift reconciliation, deployment wizard, configuration history, monitoring.
- Editing prefix tenant/scope and IP tenant/role/NAT; device and interface creation.

## Known limitations

- Utilization counts IP records; NetBox counts distinct hosts. They differ only when the same host
  is recorded twice in one VRF, or an IP sits inside a utilized range.
- Moving a server to another address in the UI means removing and re-adding it (the API supports a
  move with a re-verified host key).
- In NetworkManager setups, an interface brought up before NetworkManager saw it is shown as
  "connected (externally)": its intended profile is not active. Lanvex reports this; it does not fix it.
