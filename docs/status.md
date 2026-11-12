# Status of Lanvex 0.3

Last updated 2026-11-12. "Verified" means it was run, not just written.

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

### Server configuration changes (deploy/lab)

All on real Ubuntu 24.04 hosts under systemd, through the Lanvex API, the change runner and real SSH,
with the helper installed through sudo exactly as documented. Only lab containers were changed.

| What | How |
|---|---|
| netplan → systemd-networkd | Add and remove a secondary address; the preview diff touches only the changed key; file stays 0600 root; no timer left behind; Lanvex's fresh read shows the result |
| NetworkManager keyfile | DNS and search domains on the management interface itself; the keyfile is edited in place and the profile reapplied |
| Verification failure → rollback | A gateway that does not exist fails "Gateway answers"; the server restores the profile byte for byte |
| Gateway that drops ping | Accepted when it answers ARP on the link (a host ignoring ICMP echo stands in for a firewall) |
| Lost connection | A change that cuts Lanvex off (a /32 mask on the management interface): Lanvex does not confirm, the server's own timer restores the configuration, Lanvex finds out afterwards and reports *Rolled back* |
| Drift | A file edited after the preview is not overwritten; nothing changes |
| Hosts that cannot take changes | ifupdown host: refused with the reason; missing sudo rule and missing Python reported distinctly; helper upload over SFTP |
| UI end to end | Browser test: install the helper with the commands the dialog shows, enable, prepare, review the diff, approve by typing the name, watch it apply and verify, see it under Automation and on the server page |
| Lanvex restarts, second-admin rule, cancellation, unreachable servers, every runner path | 29 API tests against a scripted server with a controllable clock |

### Everywhere

| What | How |
|---|---|
| Backend | 232 tests: 212 unit and API (real PostgreSQL, migrations up/down/up with no drift), 8 live NetBox, 12 SSH lab (5 reading, 7 changes) |
| Frontend | Type check, 16 unit tests, production build; 7 Playwright tests in Chrome (2 against the lab) |
| Helper | Runs on Python 3.7+ (checked with vermin); stdlib only, plus PyYAML on netplan hosts |

## Not verified yet

| What | Why | How to close it |
|---|---|---|
| Debian and Fedora hosts end to end | Their package mirrors were unreachable from the build environment, so the lab is Ubuntu-only. Detection of both is unit-tested; their network tools are the same upstream projects | Add a Debian 12 and a Fedora 41 host to the lab, or run Lanvex against one |
| Changes on anything but Ubuntu 24.04 | Same reason. Older netplan (Ubuntu 20.04/22.04) and NetworkManager versions are untested | Run a change on a test server of each kind before relying on it |
| Changes on real hardware and real routers | The lab is containers on one bridge; DHCP was not tested (no DHCP server in the lab) | Try on a non-critical server first, with console access at hand |
| More than one Lanvex process running changes | The runner claims changes with row locks and is designed for it, but only one process was tested | — |
| IPv6, VLAN and bond interfaces on servers | The lab kernel has no IPv6, 8021q or bonding | Parsers are covered by constructed samples in iproute2's JSON shape; confirm on a real host |
| Docker image build and Compose stack | No Docker Hub access from the build environment | `docker compose up -d --build` |
| NetBox versions other than 4.7.4 | | Run the live suite against each supported version (4.2+) |

## Not built yet

- **More kinds of server change.** Creating VLAN interfaces, bonds and bridges; IPv6; static routes;
  changes on ifupdown servers. Updating NetBox after a successful change (today a separate step).
- Scheduled reads of servers (today reads happen on request).
- Topology, discovery, drift reconciliation, deployment wizard, configuration history, monitoring.
- Editing prefix tenant/scope and IP tenant/role/NAT; device and interface creation.

## Known limitations

- Utilization counts IP records; NetBox counts distinct hosts. They differ only when the same host
  is recorded twice in one VRF, or an IP sits inside a utilized range.
- Moving a server to another address in the UI means removing and re-adding it (the API supports a
  move with a re-verified host key). Removing a server also removes its change history from the
  Automation list; the audit log keeps every change.
- A change edits one interface at a time; there are no scheduled or batched changes.
- In NetworkManager setups, an interface brought up before NetworkManager saw it is shown as
  "connected (externally)": its intended profile is not active. Lanvex reports this; it does not fix it.
