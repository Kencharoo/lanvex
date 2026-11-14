# Status of Lanvex 0.4.2

Last updated 2026-11-14. "Verified" means it was run, not just written.

## Verified

### First installation on a real server (0.4.0)

On Ubuntu 24.04 with Docker Compose v2, from the release archive, Lanvex Core mode, plain HTTP on a
Tailscale address, automation off:

| What | Result |
|---|---|
| Images | `lanvex:local` and `lanvex-core:local` build from the archive |
| Containers | All seven start; `lanvex`, `lanvex-db`, `lanvex-redis`, `core`, `core-db`, `core-redis` turn healthy. Lanvex Core's first start (migrations, service account) took about 3½ minutes |
| Lanvex | Sign-in over plain HTTP; Lanvex Core reachable and authenticated as NetBox 4.7.5; the prefix, IP address, VLAN, device and overview APIs answer |
| Lanvex Core through Lanvex | `/core/` opens as the signed-in user, with its styles |
| Problems found | Seven, fixed in 0.4.1 ([CHANGELOG.md](../CHANGELOG.md)). The first automatic full installation then found that 0.4.1's health check reported Lanvex Core healthy too early; fixed in 0.4.2 |

### Lanvex Core (new in 0.4)

Lanvex Core ran from its source (gunicorn, PostgreSQL 16, Redis), with Lanvex in front of it exactly
as the containers are wired, but outside Docker (see *Not verified yet*).

| What | How |
|---|---|
| Lanvex on Lanvex Core | The 8 live tests (`pytest -m netbox`) pass against Lanvex Core with the `lanvex-service` token and only the permissions Lanvex Core gives it |
| Single sign-on | Browser test (`core.live.spec.ts`): Lanvex Core while signed out → Lanvex's sign-in → back to the page asked for, as the same user; a prefix created through Lanvex Core's own form appears in Lanvex with a working *Open in Lanvex Core* link; logging out of Lanvex Core signs out of Lanvex; a read-only user sees the data and is refused (403) on add |
| Who gets in | Requests without the proxy secret, with a wrong one, or as the service account are not signed in, and a failed sign-in through Lanvex says so; a session replayed without Lanvex is ended; a role changed in Lanvex applies on the next page; read-only users cannot see users or permissions (17 Lanvex Core tests and manual checks with curl) |
| The proxy | 14 tests: signed-out browsers and API calls, header stripping and replacement, display names outside ASCII, form posts with their length, cookies both ways, static files and path traversal, Lanvex Core not answering |
| Service account | `lanvex_init` creates the account, its four permissions and its token; repeated runs change nothing; a changed token or pepper replaces the old token; the account cannot sign in |
| Container roles | The image's entrypoint (`core`, `worker`, `manage`) run outside Docker against the same services |
| Installer | Both modes with `--no-start`: `.env` contents and permissions, `docker compose config` for both profiles; Lanvex Core's own secrets reach only its containers; shellcheck clean |
| NetBox's own test suite | All 14,725 NetBox tests on the fork (`lanvex/scripts/test.sh --full`): 14,723 pass, 22 skipped as upstream intends. Two did not pass in the full parallel run, and both pass on their own: one checks that the settings file sits in NetBox's folder (the test run used one from elsewhere), the other uses a database ID as an IP address octet and breaks once earlier tests push IDs past 255 (NetBox's test, unchanged in the fork) |
| Fork hygiene | `check-fork.sh`: every changed NetBox file listed in FORK.md and marked; ruff clean |
| Look | Light and dark themes checked in the browser: Lanvex colours, typeface and logo, no NetBox Labs links or news feed |

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
| Backend | 249 tests: 229 unit and API (real PostgreSQL, migrations up/down/up with no drift), 8 live NetBox, 12 SSH lab (5 reading, 7 changes). The lab tests were last run for 0.3; 0.4 changes server code only where links to NetBox are built, which unit tests cover |
| Frontend | Type check, 16 unit tests, production build; 8 Playwright tests in Chrome (2 against the lab, 1 against Lanvex Core) |
| Helper | Runs on Python 3.7+ (checked with vermin); stdlib only, plus PyYAML on netplan hosts |

## Not verified yet

| What | Why | How to close it |
|---|---|---|
| Debian and Fedora hosts end to end | Their package mirrors were unreachable from the build environment, so the lab is Ubuntu-only. Detection of both is unit-tested; their network tools are the same upstream projects | Add a Debian 12 and a Fedora 41 host to the lab, or run Lanvex against one |
| Changes on anything but Ubuntu 24.04 | Same reason. Older netplan (Ubuntu 20.04/22.04) and NetworkManager versions are untested | Run a change on a test server of each kind before relying on it |
| Changes on real hardware and real routers | The lab is containers on one bridge; DHCP was not tested (no DHCP server in the lab) | Try on a non-critical server first, with console access at hand |
| More than one Lanvex process running changes | The runner claims changes with row locks and is designed for it, but only one process was tested | — |
| IPv6, VLAN and bond interfaces on servers | The lab kernel has no IPv6, 8021q or bonding | Parsers are covered by constructed samples in iproute2's JSON shape; confirm on a real host |
| 0.4.1 as a whole in Docker, and updating 0.4.0 → 0.4.1 | The build environment cannot reach an image registry, so the images cannot be built here | Update the test server as in docs/core.md (*Updating*); all seven containers must turn *healthy* and stay so after ten minutes |
| In the browser on a real installation: creating and editing data, read-only users, servers over SSH, server changes | Not part of the first installation test | A walkthrough on the test server |
| Backup and restore of a Docker installation | Not tried yet | The commands in docs/core.md (*Backups*) on the test server, restored into a fresh installation |
| Upgrading an installation from 0.3 | No 0.3 installation exists outside the build environment | 0.3 installations keep their NetBox; Lanvex Core is only added by a new installation or by hand (`.env.example`) |
| NetBox versions other than 4.7.4 and 4.7.5 | | Run the live suite against each supported version (4.2+) |
| Moving data from an existing NetBox into Lanvex Core | Not built | NetBox's own backup and restore, by hand; see docs/core.md |

## Not built yet

- **More kinds of server change.** Creating VLAN interfaces, bonds and bridges; IPv6; static routes;
  changes on ifupdown servers. Updating NetBox after a successful change (today a separate step).
- Scheduled reads of servers (today reads happen on request).
- Topology, discovery, drift reconciliation, deployment wizard, configuration history, monitoring.
- Editing prefix tenant/scope and IP tenant/role/NAT; device and interface creation.

## Known limitations

- Lanvex Core's API can be used only from a signed-in Lanvex browser session and by Lanvex itself; other
  tools cannot use tokens against it. It has no outbound network access (webhooks, remote data
  sources). Permissions inside it follow Lanvex's two roles. Details in docs/core.md.

- Utilization counts IP records; NetBox counts distinct hosts. They differ only when the same host
  is recorded twice in one VRF, or an IP sits inside a utilized range.
- Moving a server to another address in the UI means removing and re-adding it (the API supports a
  move with a re-verified host key). Removing a server also removes its change history from the
  Automation list; the audit log keeps every change.
- A change edits one interface at a time; there are no scheduled or batched changes.
- In NetworkManager setups, an interface brought up before NetworkManager saw it is shown as
  "connected (externally)": its intended profile is not active. Lanvex reports this; it does not fix it.
