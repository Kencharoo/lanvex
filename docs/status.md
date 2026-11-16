# Status of Lanvex 0.8.0

Last updated 2026-11-16. "Verified" means it was run, not just written.

## Verified

### Second installation on a real server (0.6.0)

On Fedora Server 44 with rootless Podman 5.8.9 and podman-compose 1.6.0 (no Docker), Lanvex Core
mode, plain HTTP. The network was documented through the Lanvex API and Lanvex Core's API, and a
Fedora 44 server running NetworkManager was registered over SSH:

| What | Result |
|---|---|
| Reading a Fedora server | Recognised as supported, NetworkManager detected, 14 interfaces and 43 neighbours read; 9 of 10 addresses matched the documentation (the tenth was undocumented) |
| Host key and helper | The fingerprint shown matched the server's; the helper's checksum was identical in the API, the download, on the server and in the release source |
| Logs | No errors in `lanvex` and `core` during the whole test |
| Problems found | Fourteen, fixed in 0.6.1 or explained ([CHANGELOG.md](../CHANGELOG.md)). The serious one: with Lanvex running on the server it changes, the connection went through the server's loopback, so a change could have cut off the server's real network and still passed verification |

### Networks on Ubuntu Server and Debian (new in 0.8)

On every push, in the CI lab, the same five tests run on three real hosts, each with a real DHCP
client (`lab-nm`) on the new VLAN:

| Host | What it is | Result |
|---|---|---|
| Fedora 44 | NetworkManager, firewalld, dnsmasq installed but off | VLAN as a NetworkManager profile with its firewalld zone; dnsmasq enabled and started; the client gets its fixed address |
| Ubuntu 24.04 | netplan with systemd-networkd, systemd-resolved on port 53, dnsmasq enabled but failing next to it, ufw | VLAN in `/etc/netplan/60-lanvex.yaml`; dnsmasq starts with `bind-dynamic` and answers DNS on the VLAN address next to systemd-resolved; the client gets its fixed address. With ufw active (SSH allowed, everything else denied) the client still gets its address through Lanvex's marked rules; removing the network removes exactly those rules, and a rollback brings them back |
| Debian 12 | ifupdown sourcing `interfaces.d`, dnsmasq running on every interface | VLAN in `/etc/network/interfaces.d/lanvex`, created by its own stanza; Lanvex's pool added without restricting dnsmasq; the client gets its fixed address |

On each: the same request again is not a change; removing the network and rolling that back restores
the interface, its address and dnsmasq (enabled and running); an unconfirmed change is undone by the
server's own timer, with dnsmasq exactly as before; another tool's VLAN (a NetworkManager profile, a
netplan file, an ifupdown file) and another dnsmasq file for the same interface are refused (13 lab
tests). Also in the lab, by hand on the Ubuntu and ifupdown hosts: a change whose VLAN cannot be
created is undone completely (ifupdown), and removing the last pool stops dnsmasq again where it did
not run before (netplan). Unit tests cover the files written for each system, choosing the system,
the dnsmasq modes and the ufw rules (6 tests); API tests cover networks on netplan and ifupdown servers,
ownership through the helper's list, and interface changes refused on ifupdown and on Lanvex's own VLANs.

### Networks: VLANs and DHCP on a server (new in 0.7)

| What | How |
|---|---|
| On a real Fedora 44 host like KlassenServer (CI) | NetworkManager, firewalld (zone FedoraServer) and dnsmasq, in the lab on every push. The helper plans, applies, verifies and confirms a new VLAN interface on eth0 with its firewalld zone and a DHCP pool with a fixed address; a real DHCP client on another host, on VLAN 50, gets its fixed address through the zone; dnsmasq is enabled. Removing the network and rolling that back restores profile, zone and dnsmasq; an unconfirmed change is undone by the server's own timer, including dnsmasq's service state; another tool's VLAN profile and dnsmasq file are refused (4 lab tests) |
| Helper rules | Validation of VLANs, pools, leases, fixed addresses; the files it renders; dnsmasq's config directory on Fedora and Debian; conflicts with other dnsmasq files (18 unit tests) |
| Lanvex's side | Documenting a network in Lanvex Core (VLAN, prefix, virtual interface, gateway address, tagged pool with lease time), fixed addresses, statuses against the server's read, leases and Discovery, protected Lanvex VLANs kept as they are, removal, the prepared change; safety rules (protected names, another tool's interface, missing parent, the VLAN carrying Lanvex's connection) (6 API tests against a Lanvex Core stand-in and a scripted server) |
| Against Lanvex Core 4.7.5 (locally) | A network documented through the Lanvex API, with its fixed address in the custom field; the change prepared from it by the real helper on the NetworkManager lab host, shown in the browser. The service account's new permissions and both custom fields are tested in Lanvex Core |
| Ubuntu with NetworkManager | Found by trying it: Ubuntu installs only the dnsmasq program with NetworkManager (dnsmasq-base); the helper now asks for the dnsmasq service |

### Changes confirmed in the browser (new in 0.7)

| What | How |
|---|---|
| On a real host | A server reached through a forwarded port (like Lanvex on KlassenServer): the change applied on the netplan lab host, the page asked to keep it with the seconds left and the browser's address (not the server's, so an extra tick), Keep confirmed it on the server. The banner on other pages |
| Every path | Keep over the server's address, keep from another address only with acknowledgement, undo, no decision (never confirmed, the server restores), a failed check never asks, a decision after the deadline refused, audit (6 API tests) |

### Change safety when Lanvex reaches a server indirectly (new in 0.6.1)

| What | How |
|---|---|
| Indirect connection | A server reached through an address it does not have (port forwarding, NAT, Lanvex on the server itself) cannot be changed until a verification address is set; an address that is not the server's is refused (API tests; in the browser through a TCP forwarder to the netplan lab host) |
| Verification | Before a change Lanvex must reach the verification address; after the change it must reach it again from Lanvex itself, not only through the helper. A change that cuts the server off its network while the forwarded connection stays up is rolled back (API tests against a scripted server) |
| Protected interfaces | Names and patterns (`vlan-*`) another tool manages are never changed, and are not offered (API test, frontend unit test) |
| Paused reads | Scheduled reads pause after two failed sign-ins in a row, or at once when OpenSSH refuses the connection (`PerSourcePenalties`); a successful read resumes them. The refusal is recognised from a real asyncssh connection to a server that answers like OpenSSH 9.8 (4 tests) |

### Topology (new in 0.5)

| What | How |
|---|---|
| Real NetBox data | The API is tested against responses captured from NetBox 4.7.5 (Lanvex Core) with a sample network: two sites, core and access switches, a patch panel with front and rear ports, a server, an unconnected switch, a planned cable and a provider circuit between the sites (8 tests). The map was checked in the browser against that Lanvex Core, light and dark, and on a phone |
| Layout rules | Role ranking, cable direction, side-by-side cables between peers, which end devices are stacked, and that nothing stacks under a circuit or across sites (7 unit tests) |
| In the browser | Devices, site frames, the circuit and all cables drawn; details panel and following a cable to its other end; search; the cable list; one site with the device at the far end of a cable shown as another site (2 browser tests against the stand-in) |
| Size | A synthetic campus of 296 devices and 349 cables is arranged and drawn in about 1.5 seconds and stays at 60 frames per second |
| Permissions | With Lanvex Core, the service account reads cables; with your own NetBox, the token needs *view* on cables (docs/operations.md) |

### Full installation, automatically on every push (GitHub Actions)

`install.sh --core` on a fresh Ubuntu 24.04 runner builds both images and starts all seven
containers; `scripts/check-install.sh` then checks them. Passed for 0.4.2: every container healthy
and never restarted, no errors in the logs, sign-in, Lanvex Core (NetBox 4.7.5) reachable and
signed in, Lanvex Core closed to visitors without a session, its styles served, and its pages open
as the signed-in user. The installer waited about 5½ minutes for Lanvex Core's first start.

Since 0.6.1 a second job does the same under **rootless Podman** with podman-compose 1.6.0 and
Fedora's registry settings (short image names refused), with Docker stopped: the installer must
pick Podman by itself, finish, remove the first administrator's password from `.env`, and every
container must report healthy through `podman healthcheck run`.

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

Since 0.5, the lab (three Ubuntu 24.04 containers) is built and its backend and browser tests run on
every push in GitHub Actions.

| What | How |
|---|---|
| Host-key verification | The fingerprint Lanvex shows equals `ssh-keygen -lf` on the server; a regenerated host key is refused until re-verified; a confirmed fingerprint must belong to the key being pinned |
| Sign-in | Lanvex-generated ed25519 key (with `restrict`), uploaded keys (incl. passphrase-protected), password; wrong key, wrong password and unreachable hosts are each reported distinctly |
| Reading network state | 5 lab tests (`pytest -m lab`) and a browser test, on Ubuntu 24.04 hosts under systemd running netplan → systemd-networkd + systemd-resolved, NetworkManager, and ifupdown, as an unprivileged user |
| Documented vs actual | Comparison against real NetBox: match, mask differs, only in NetBox, not documented, recorded on another object, link suggestions; works with the least-privilege token |
| Recording differences in NetBox | Against Lanvex Core (NetBox 4.7.5) and the netplan host: an interface the device lacks is added, a server-only address recorded on it and on eth0, a prefix length corrected, an address the server lacks unassigned; the preview writes nothing; a plan that changed since the preview is refused (live test). Refusals (address on another device, primary IP), DHCP addresses recorded as DHCP, a write NetBox refuses midway (4 API tests). In the browser: linking, recording, and preparing a server change from a difference |
| Discovery | Neighbour tables and LLDP read from the three lab hosts: through systemd-networkd (lab-netplan), through lldpd (lab-ifup), and lldpd that the account may not ask (lab-nm, reported as such); every host's address listed with the others as observers and the right prefix length (lab test, in CI). LLDP compared with documented cables: documented through patch panels, different, missing, shared segment, missing device, port or interface, port already cabled, interface types NetBox cannot cable (found by trying it: Lanvex Core refused a cable on *Virtual* interfaces, so the preview now says so); recording hosts and cables, refusals, stale previews (4 API tests). A cable recorded from the Discovery page in Lanvex Core 4.7.5 (by hand). In the browser: the page and an interface's LLDP neighbour |
| History and scheduled reads | Repeated reads of an unchanged real host are recognised as the same state; changes made on the host are kept up to the retention limit and described in words (lab test). Two Lanvex processes never read the same server twice for one due time (API test). The history through a host key change (browser test) |
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
| Hosts that cannot take changes | ifupdown host: interface settings refused with the reason (networks only); missing sudo rule and missing Python reported distinctly; helper upload over SFTP |
| UI end to end | Browser test: install the helper with the commands the dialog shows, enable, prepare, review the diff, approve by typing the name, watch it apply and verify, see it under Automation and on the server page |
| Lanvex restarts, second-admin rule, cancellation, unreachable servers, indirect connections, protected interfaces, browser confirmation, networks changes, every runner path | 42 API tests against a scripted server with a controllable clock |

### Everywhere

| What | How |
|---|---|
| Backend | 326 tests: 301 unit and API (real PostgreSQL, migrations up/down/up with no drift), 8 live NetBox, 17 SSH lab (6 reading, discovery and recording; 7 changes; 4 networks on Fedora). The lab tests run in CI on every push; the one that also needs a live NetBox ran locally against Lanvex Core 4.7.5, as its own service account |
| Frontend | Type check, 30 unit tests, production build; 10 Playwright tests in Chrome (2 against the lab, in CI; 1 against Lanvex Core) |
| Lanvex Core | 0.7.0: its own tests (permissions of the service account, the DHCP custom fields, and its CSRF cookie for scripts included) |
| Helper | Runs on Python 3.7+ (checked with vermin); stdlib only, plus PyYAML on netplan hosts |

## Not verified yet

| What | Why | How to close it |
|---|---|---|
| Reading and interface changes on Debian | The Debian 12 lab host (0.8) runs the networks tests through the helper; Lanvex's SSH reads and interface changes run on the Ubuntu hosts | Read a Debian server with Lanvex before relying on it |
| Interface changes on Fedora; networks on other versions | Networks are tested on Fedora 44, Ubuntu 24.04 and Debian 12; interface changes on the Ubuntu hosts. Older netplan (Ubuntu 20.04/22.04), ifupdown and NetworkManager versions, RHEL, and Ubuntu with NetworkManager for networks are untested | Run a change on a test server of each kind before relying on it |
| Changes on real hardware and real routers | The lab is containers on one bridge | Try on a non-critical server first, with console access at hand |
| A networks change through Lanvex against a live Lanvex Core, end to end | The CI lab has no Lanvex Core; the pieces were verified separately (helper on Fedora in CI, Lanvex Core locally, the flow with stand-ins) | A network on KlassenServer or a test server, from Add network to a device with a lease |
| Netzverwaltung and Lanvex side by side on KlassenServer | Lanvex leaves `nv-` files, Netzverwaltung's profiles and zones alone (tested with stand-ins on Fedora), but not with Netzverwaltung itself | Add a Lanvex network on a free VLAN there, then run netzverwaltung-apply and check both still serve DHCP |
| More than one Lanvex process running changes | The runner claims changes with row locks and is designed for it, but only one process was tested | — |
| IPv6 and bond interfaces on servers | The lab kernel has no IPv6 or bonding (CI loads 8021q for VLANs) | Parsers are covered by constructed samples in iproute2's JSON shape; confirm on a real host |
| Updating an existing installation (0.4.0 → 0.4.2) | Every automatic run installs from scratch | Update the test server as in docs/core.md (*Updating*), then `scripts/check-install.sh "" admin` |
| In the browser on a real installation: creating and editing data, read-only users, servers over SSH, server changes | Not part of the first installation test | A walkthrough on the test server |
| Backup and restore of a Docker installation | Not tried yet | The commands in docs/core.md (*Backups*) on the test server, restored into a fresh installation |
| Upgrading an installation from 0.3 | No 0.3 installation exists outside the build environment | 0.3 installations keep their NetBox; Lanvex Core is only added by a new installation or by hand (`.env.example`) |
| NetBox versions other than 4.7.4 and 4.7.5 | | Run the live suite against each supported version (4.2+) |
| Moving data from an existing NetBox into Lanvex Core | Not built | NetBox's own backup and restore, by hand; see docs/core.md |

## Not built yet

- **Access rules** for networks (firewalld policies, nftables, ufw), DNS filtering, and importing an
  existing setup such as Netzverwaltung's ([roadmap.md](roadmap.md)).
- **More kinds of server change.** Bonds and bridges; IPv6; static routes; interface settings on
  ifupdown servers; Kea instead of dnsmasq; nftables rules for DHCP (pointed out today).
- Discovery from the network side (SNMP, switch MAC tables) and of devices Lanvex does not read; a
  deployment wizard; monitoring and alerts.
- Comparing gateways, routes and DNS with NetBox (today the comparison covers addresses).
- Editing prefix tenant and scopes other than a site, and IP tenant/role/NAT; device creation.

## Known limitations

- Lanvex Core's API can be used only from a signed-in Lanvex browser session and by Lanvex itself; other
  tools cannot use tokens against it. It has no outbound network access (webhooks, remote data
  sources). Permissions inside it follow Lanvex's two roles. Details in docs/core.md.

- Utilization counts IP records; NetBox counts distinct hosts. They differ only when the same host
  is recorded twice in one VRF. Like NetBox, Lanvex counts an IP range only when it is marked
  *utilized*: a DHCP range that is not marked adds nothing.
- Under rootless Podman, a container cannot reach its own host's LAN address. Lanvex reads the server
  it runs on through `host.containers.internal`; to change it, turn on browser confirmation
  (docs/changes.md): it proves the network your browser is on, not every network the change touched.
- Devices on a Lanvex network reach only the server's DHCP and DNS until access rules exist (0.9).
- Moving a server to another address in the UI means removing and re-adding it (the API supports a
  move with a re-verified host key). Removing a server also removes its change history from the
  Automation list; the audit log keeps every change.
- A change edits one interface at a time; there are no scheduled or batched changes.
- In NetworkManager setups, an interface brought up before NetworkManager saw it is shown as
  "connected (externally)": its intended profile is not active. Lanvex reports this; it does not fix it.
