# Lanvex Manual

For administrators who install and run Lanvex, and for everyone who works with it. This manual covers
Lanvex 1.4. Each chapter ends with a link to the reference document that has every detail and
setting. [Deutsch](de.md)

1. [What Lanvex does](#1-what-lanvex-does)
2. [Installing](#2-installing)
3. [First steps](#3-first-steps)
4. [Documenting the network](#4-documenting-the-network)
5. [Servers](#5-servers)
6. [Changing a server's network settings](#6-changing-a-servers-network-settings)
7. [Networks on a server: VLANs, DHCP, access, DNS filter](#7-networks-on-a-server-vlans-dhcp-access-dns-filter)
8. [Switches](#8-switches)
9. [Discovery and topology](#9-discovery-and-topology)
10. [Monitoring and alerts](#10-monitoring-and-alerts)
11. [Users and sign-in](#11-users-and-sign-in)
12. [Running Lanvex](#12-running-lanvex)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. What Lanvex does

Lanvex is a console for your network: networks (prefixes), IP addresses, VLANs, devices, servers and
switches. It keeps three things apart:

| | What it is | Where it lives |
|---|---|---|
| **Documented** | How the network should be | Lanvex Core (installed with Lanvex), or a NetBox you already run |
| **Actual** | What servers and switches really have | Lanvex reads it over SSH and SNMP |
| **Pending** | Changes waiting for approval or being applied | Lanvex, under *Automation* |

What you create or edit in Lanvex (a network, an IP address, a VLAN) is written to the documentation at
once, and Lanvex labels it as such: *Recorded in Lanvex Core*, never "deployed". Servers and switches
are changed only through a change that an administrator prepared, reviewed and approved, and only
where changes are turned on. They are off by default.

**Roles.** *Administrators* may change everything. *Read-only* users see everything and change nothing,
in Lanvex and in Lanvex Core.

**Navigation.** *Overview*, *Networks*, *IP Addresses*, *VLANs*, *Devices*, *Servers* and *Switches*
cover the inventory. *Alerts*, *Topology*, *Discovery*, *Automation* (every server and switch change)
and *Activity & Audit Logs* follow. *Settings* holds your account, users, sign-in, monitoring and
webhooks. *Lanvex Core* opens the documentation application with the same sign-in.

---

## 2. Installing

**You need** a Linux server with Docker and Docker Compose v2, or Podman with podman-compose (rootless
works too).

```bash
curl -fsSLO https://raw.githubusercontent.com/Kencharoo/lanvex/main/lanvexctl
sudo bash lanvexctl install          # rootless Podman: bash lanvexctl install
```

`lanvexctl` downloads the latest release, checks it against its published SHA-256 sum and asks:

1. **Where to keep the documentation**: *Lanvex Core, installed now* (recommended), or a NetBox you
   already run (its address and an API token).
2. **How Lanvex is reached**: over HTTPS through a reverse proxy, or over plain HTTP on a trusted
   network.
3. **The port** Lanvex listens on.
4. **The first administrator's name.**

It generates every password and key into `.env`, builds and starts the containers, waits until they
are healthy and prints how to sign in. Lanvex Core's first start sets up its database and takes several
minutes.

**HTTPS.** Put a reverse proxy in front of Lanvex. With Caddy on the same host:

```
lanvex.example.net {
    reverse_proxy 127.0.0.1:8080
}
```

and in `.env`: `LANVEX_HTTP_PORT=127.0.0.1:8080` and `LANVEX_TRUSTED_PROXIES=["172.31.250.1/32"]`.
Without the second line Lanvex does not see that the browser used HTTPS and refuses sign-in. Apply
changes to `.env` with `lanvexctl restart`.

**Your own NetBox** needs a user and token with the permissions listed in
[operations.md](../operations.md#netbox-token-permissions), and three custom fields for server networks
([networks.md](../networks.md#your-own-netbox-instead-of-lanvex-core)). Lanvex Core has all of this set up.

Reference: [README](../../README.md#installation), [operations.md](../operations.md),
[core.md](../core.md).

---

## 3. First steps

1. **Sign in** with the administrator the installer created. Change the password under *Settings,
   Your account*.
2. **Check the documentation connection.** The bottom of the navigation shows *Lanvex Core* or
   *NetBox* with its version and a green dot when Lanvex reaches it.
3. **Add people** under *Settings, Users*: a name, a password and a role. To use your directory or
   single sign-on instead, see [chapter 11](#11-users-and-sign-in).
4. **Open Lanvex Core** from the navigation. You are signed in there as yourself; administrators are
   superusers in Lanvex Core, read-only users can look but not change. *Back to Lanvex* at the top
   returns.

Everything an administrator changes, in Lanvex or through it, is in *Activity & Audit Logs*: who, what,
when, from which address, and whether it worked.

---

## 4. Documenting the network

| Page | What you do there |
|---|---|
| **Networks** | Browse prefixes by VRF and hierarchy with their utilization. *Add network* walks through four steps with live checks (host bits, duplicates in the VRF, where it sits among parents and children, a valid gateway). A network's page shows free ranges and the next free addresses; deleting asks you to type the prefix and shows what goes with it |
| **IP Addresses** | Search and filter, add a specific address or take the next free one, change the status, assign an address to a device's interface or release it. A device's primary IP is protected |
| **VLANs** | Create, edit and delete VLANs; IDs and names are checked per VLAN group |
| **Devices** | The inventory with interfaces, 802.1Q modes, addresses and VLANs. An interface's documented VLANs can be edited here |

A network's **gateway** is an IP address in it tagged `lanvex-gateway`; Lanvex sets and reads it for you.
Sites, racks, cables, circuits, virtual machines and the rest are documented in **Lanvex Core**
directly.

---

## 5. Servers

Lanvex reads Linux servers over SSH: interfaces, addresses, routes, DNS, LLDP neighbours, DHCP leases
and which network stack manages what (netplan with systemd-networkd, NetworkManager, ifupdown). Reading
needs no privileges.

### Adding a server

1. On the server, create an account for Lanvex: `sudo useradd -m -s /bin/sh lanvex`.
2. In Lanvex: *Servers, Add server*. Lanvex fetches the server's SSH host key and shows its
   fingerprint. Compare it with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`, run on the server
   through a console you already trust, then confirm it.
3. Let Lanvex create a key. Add the line it shows (`restrict ssh-ed25519 …`) to
   `~lanvex/.ssh/authorized_keys`. `restrict` allows no forwarding and no terminal.
4. *Test connection*, then read the server.

On appliances and NAS systems with a read-only root file system, use an existing account and put the
key into its home directory ([operations.md](../operations.md#adding-a-server)).

If the server's host key ever changes, Lanvex stops connecting until you verify the new fingerprint
the same way.

### Reading, history

Lanvex reads every server once an hour (per server: 15 minutes to daily, or off) and on *Read now*.
*History* lists each state the server was in and what changed, in words: addresses, links, MTU,
gateway, routes, DNS. Scheduled reads pause by themselves after two failed sign-ins in a row, so the
server's protection against password guessing (OpenSSH, fail2ban) does not block Lanvex's address.
Fix the cause and read once by hand; the schedule resumes.

### Compared with the documentation

Link the server to its device or virtual machine (Lanvex suggests one with the same name). The server
page then compares:

| What | Documented as |
|---|---|
| **Addresses** | IP addresses on the device's interfaces |
| **Default gateway** | The gateway of the network (prefix) the server's gateway is in |
| **DNS servers, search domains** | `dns_servers` and `dns_search` under the key `lanvex` of the device's config context |
| **Static routes** | `routes` under the same key: a list of `{"to": …, "via": …}` |

```json
{
  "lanvex": {
    "dns_servers": ["10.10.0.53", "10.10.0.54"],
    "dns_search": ["school.example"],
    "routes": [{"to": "10.50.0.0/16", "via": "10.10.0.254"}]
  }
}
```

Settings shared by many servers go into one config context in Lanvex Core, assigned by site, role or
tag; a server's own go into its local config context data.

Every difference can be closed in either direction from its menu:

- **Record in Lanvex Core…**: the server is right. Lanvex shows exactly which updates it would make
  (create the address on the interface, correct a prefix length, unassign an address, set the
  prefix's gateway, write DNS and routes into the device's local config context data) and makes those
  and no others. If the documentation or the server changed in between, you get a new preview instead.
- **Change the server to match…**: the documentation is right. This opens a server change with the
  difference already undone ([chapter 6](#6-changing-a-servers-network-settings)).

Reference: [operations.md](../operations.md#adding-a-server),
[changes.md](../changes.md#starting-from-a-difference).

---

## 6. Changing a server's network settings

Lanvex can change one interface at a time: IPv4 addresses or DHCP, the default gateway, DNS servers and
search domains, IPv6 and static routes. It can also create and remove bridges and bonds. Supported:

| Network stack | Typical system | Lanvex edits |
|---|---|---|
| netplan (systemd-networkd or NetworkManager) | Ubuntu Server | The netplan file that defines the interface |
| NetworkManager keyfiles | Fedora, RHEL | The interface's profile |
| ifupdown | Debian | The interface's stanzas in `/etc/network/interfaces` or a file it sources |

### Turning changes on

Changes are off three times over, and each must be turned on:

1. **For the installation**: `LANVEX_AUTOMATION_ENABLED=true` in `.env`, then `lanvexctl restart`.
2. **The helper on the server.** The only thing Lanvex runs as root is a small helper program. On the
   server page, *Configuration changes*, *Install the helper*: *Copy to the server*, then run the
   commands shown on the server as an administrator. The first command checks the file's checksum:
   stop if it does not print `OK`. Then *Check now*.
3. **For the server**: *Turn on* in the same section.

After a Lanvex update that ships a new helper, every server shows *Outdated version*. Copy the new file
and run the commands again; Lanvex cannot replace it by itself.

### Making a change

1. Server page, **Change IP settings**. Choose the interface; the form starts with what it has now.
2. Edit the addresses (`192.0.2.10/24`), the gateway, or choose DHCP. DNS, IPv6 and static routes stay
   as they are unless you tick *Also change …*.
3. Optionally name a host that must still resolve afterwards.
4. **Prepare change.** The change page shows the old and new values, the exact file diff, the commands
   and any warnings. Nothing on the server has changed yet.
5. **Approve and apply**, typing the server's name. With `LANVEX_CHANGES_REQUIRE_SECOND_ADMIN=true`,
   another administrator approves.

### What happens then

1. The helper checks that the file has not changed since the preview, backs it up and **arms a
   rollback timer on the server** (two minutes by default) before writing anything.
2. It writes the new configuration and applies it.
3. Lanvex connects again and checks: it can still sign in, the addresses are there, the default
   route, the gateway answers (ping, or at least ARP), DNS, IPv6 and routes as requested.
4. If every check passes, Lanvex confirms and the timer is cancelled. If one fails, the previous
   configuration comes back at once. If Lanvex cannot reach the server at all, the server's own timer
   restores it.

The change page follows each step. Its status is always one of *Waiting for approval*, *Approved,
queued*, *Applying*, *Verifying*, *Applied and verified*, *Rolled back*, *Not applied*, *Cancelled* or
*Needs attention*. Nothing is called applied before the server confirmed it. *Needs attention* means
Lanvex could not find out what happened; check the server on its console, then *Check again*.

The documentation is not changed by a server change. Afterwards, the change page offers to record the
result.

### Safety rules

- **Lanvex's own connection.** The address Lanvex connects to must stay on the interface as a static
  address. To move a server to a new address: add the new one, change the server's address in Lanvex,
  remove the old one in a second change.
- **Indirect connections** (port forwarding, NAT, Lanvex on the server itself): set a *verification
  address* in *Edit server*, one of the server's own addresses that Lanvex reaches directly. Without it,
  changes are refused.
- **The server Lanvex runs on**: turn on *My browser confirms changes*. After the checks pass, the
  change page asks *Does everything still work?*; only *Keep the change* confirms it. Use Lanvex through
  one of that server's own addresses for this.
- **Protected interfaces** (*Edit server*, names or patterns such as `vlan-*`): interfaces another tool
  manages. Lanvex never changes them.

### Bridges and bonds

*Add bridge or bond* creates one from interfaces without addresses (unused network ports): members,
bond mode (active-backup works with any switch; 802.3ad needs LACP on the switch ports), and IPv4 (none,
static or DHCP). It runs as a change like any other. Bridges and bonds Lanvex created are listed on the
server page and can be removed again.

Reference: [changes.md](../changes.md).

---

## 7. Networks on a server: VLANs, DHCP, access, DNS filter

A Linux server can be the router for several networks: one VLAN interface per network on its trunk to
the switch, an address in each, DHCP for the devices, and rules for what they may reach.

1. **Document the network**: server page, *Networks*, *Add network*: VLAN ID, interface name, trunk,
   the server's address in the network, and the DHCP pool. This goes into Lanvex Core at once.
2. **Compare.** Each network shows whether it is on the server yet, differs, or is to be removed.
3. **Prepare the change.** One change covers every network on the server; the preview shows each file
   and command.
4. **Approve it.** It runs like every other change, with checks that each VLAN interface has its
   address and that DHCP answers.

| Topic | In short |
|---|---|
| **DHCP** | dnsmasq, or Kea where it already runs. The pool is an IP range tagged `lanvex-dhcp`, with an optional lease time. A fixed address is an IP address with a MAC address. *Make fixed* turns a lease into one |
| **What devices may reach** | Closed by default except DHCP and DNS on the server. Per network: the Internet (masqueraded), other Lanvex networks, other networks or single addresses with ports, services on the server. On firewalld, ufw, or Lanvex's own nftables table |
| **DNS filter** | Blocked domains (with every name below them) and exceptions, per network. Needs Lanvex's DHCP pool on the network |
| **Netzverwaltung** | *Import from Netzverwaltung* documents its networks in Lanvex Core; *Prepare takeover* replaces it in one change, and a rollback brings it back exactly as it was |

**The server needs** NetworkManager, netplan or ifupdown on the trunk, the dnsmasq service (or Kea, set
up once), and the switch must carry the VLAN to the server's port. Lanvex writes only its own files and
never touches another tool's interfaces, zones or dnsmasq files.

Reference: [networks.md](../networks.md).

---

## 8. Switches

Lanvex reads managed switches over SNMP: ports, VLANs, link aggregations, the MAC address table and
LLDP neighbours.

**Adding**: *Switches, Add switch*: address, SNMP version and credentials, *Test connection*, *Add and
read*. Use an SNMPv3 user with authentication and privacy and a read view of the whole tree; SNMPv2c
only on a management network. Turn on LLDP on the switches. Link the switch to its device in the
documentation to compare its ports with the cables.

**What you see**: every port with its state, speed and VLANs, and which device is plugged in, named
from the servers Lanvex reads and the documentation (or the maker, from the MAC address). Each port
is compared with the documented cable: *Documented*, *Different cable*, *No cable recorded* (*Record
cable…* adds it after a preview), *Unknown device*, *Several devices*.

**Changing a port's VLANs** (untagged VLAN and tagged VLANs) needs `LANVEX_AUTOMATION_ENABLED=true` and
*Allow VLAN changes from Lanvex* on the switch, plus SNMP write access to the Q-BRIDGE-MIB. Lanvex shows
the SET requests in order, asks you to type the port's name, checks the port has not changed, writes
step by step, reads the port back, and undoes the written steps if the switch refuses one. Uplinks, link
aggregations and the port the switch reaches Lanvex through are never changed. Lanvex does not save
the switch's configuration: save it on the switch if it should survive a restart.

Reference: [switches.md](../switches.md).

---

## 9. Discovery and topology

**Discovery** lists what servers and switches see around them, compared with the documentation:

- *Hosts seen*: hosts in the servers' neighbour tables and DHCP leases, with the switch port they are
  on. Addresses the documentation lacks can be recorded after a preview.
- *Connections seen over LLDP*: the device on the other end of each server interface. A missing cable
  can be recorded.
- *Switch ports*: what is on each port, by default only where it differs from the documentation.

LLDP needs a receiver on the server: systemd-networkd (Ubuntu Server) has one; elsewhere install
`lldpd` and add Lanvex's account to the group that may ask it (`adm` on Debian and Ubuntu).

**Topology** draws devices, sites, circuits and the cables between them as documented: core above
access above servers, servers stacked under their switch, devices Lanvex reads marked. Connections
switches see but no cable records are drawn as dotted lines. Search, a details panel per device and a
cable list are included.

---

## 10. Monitoring and alerts

Lanvex watches every server and switch it reads:

| Alert | When |
|---|---|
| **Unreachable** | The SSH port (servers) or SNMP (switches) did not answer three checks in a row. Checked every two minutes without signing in |
| **Read failed** | A read was refused: sign-in rejected, host key changed, address not allowed |
| **Drift** | Addresses, gateway, DNS, routes or Lanvex networks differ from the documentation. Settings nothing documents are no drift |
| **DHCP pool** | A Lanvex pool is 90 % full (critical when full) |
| **Unknown device** | A device holds a lease or appears on a switch port, and is documented nowhere |
| **Port down**, **Device moved** | On switches: an uplink or a port with an LLDP neighbour went down; a documented device moved ports |
| **Change failed** | A change was rolled back, failed, or needs attention |

*Alerts* lists them; the navigation counts the open ones nobody has acknowledged. Checks, thresholds
and exclusions (interfaces, networks, `vlan:<id>`) are under *Settings, Monitoring*.

**Messages** go out when an alert opens, changes and ends:

- **Email**: an SMTP server in `.env` (`LANVEX_SMTP_HOST`, `_PORT`, `_SECURITY`, `_USERNAME`,
  `_PASSWORD`, `_FROM`, and `LANVEX_PUBLIC_URL` for links), then recipients and the lowest severity
  under *Settings, Monitoring*.
- **Webhooks** (*Settings, Webhooks*): JSON, ntfy, Gotify, Slack or Mattermost, Discord, Microsoft
  Teams, each with its lowest severity. *Test* sends a test message.

Reference: [monitoring.md](../monitoring.md).

---

## 11. Users and sign-in

**Local users** are managed under *Settings, Users*: add, change the role, deactivate, delete once
deactivated. Lanvex keeps at least one active administrator. On the command line:
`lanvexctl user create NAME [--admin]`, `deactivate`, `delete`.

**Directory and single sign-on.** People can also sign in with their account from Active Directory or
another LDAP directory (the same form), or with OpenID Connect (Keycloak, Microsoft Entra ID,
Authentik, …; a button on the sign-in page). Groups decide the role at every sign-in: members of the
administrators' group become administrators, members of the read-only group read-only users, anyone
else cannot sign in. Accounts are created on the first sign-in and can be deactivated in Lanvex. Local
users keep working while the directory is down, so keep the first administrator.

The settings go into `.env` (`LANVEX_LDAP_*`, `LANVEX_OIDC_*`); *Settings, Sign-in* shows what is
configured, the redirect URI to register with the provider, and *Test directory*.

Reference: [sign-in.md](../sign-in.md).

---

## 12. Running Lanvex

| Command | |
|---|---|
| `lanvexctl status` | Version, directory, address, health of every container |
| `lanvexctl logs [SERVICE]` | Logs (`lanvex`, `core`, `core-worker`, …) |
| `lanvexctl backup [FILE]` | Both databases, Lanvex Core's uploads and `.env` in one file |
| `lanvexctl restore FILE` | Into this installation, or a fresh one after a reinstall |
| `lanvexctl update` | Backs up, installs the latest release next to the running one, replaces the containers |
| `lanvexctl start`, `stop`, `restart` | `restart` also applies changes to `.env` |
| `lanvexctl uninstall [--purge]` | Removes the containers; `--purge` also the data (backups stay) |

**Backups** contain `.env`, which holds the keys that decrypt server credentials: keep the file like a
password. `lanvexctl update` makes one before every update.

**After an update**, read the release notes in [CHANGELOG.md](../../CHANGELOG.md). When it ships a new
change helper, reinstall it on each server that takes changes ([chapter 6](#turning-changes-on)).

**Settings** are environment variables in `.env`; every one is listed in
[configuration.md](../configuration.md).

---

## 13. Troubleshooting

| Message or symptom | What to do |
|---|---|
| Sign-in says sessions are HTTPS-only | Use HTTPS through a reverse proxy, and trust its address in `LANVEX_TRUSTED_PROXIES`; or install with plain HTTP on a trusted network |
| "Lanvex Core is not answering" | It is starting (the first start takes minutes) or stopped: `lanvexctl status`, `lanvexctl logs core` |
| "NetBox denied the request" | The token lacks a permission for that action ([operations.md](../operations.md#netbox-token-permissions)) |
| Server: *Host key changed* | Reinstalled server, or interception. Verify the new fingerprint on the server, then *Verify a new host key* |
| Server: *Sign-in failed* | The key is not in `authorized_keys`, the user is wrong, or sshd's `AllowUsers` excludes it |
| Server: *Scheduled reads are paused* | Two failed sign-ins in a row, or the server refused Lanvex's address for now. Fix the cause, read once by hand |
| Helper: *Not allowed through sudo*, *Python 3 missing*, *Outdated version* | Run the install commands from the server page again; install `python3` |
| Change *Rolled back*, "Gateway … answers" failed | Nothing answered for the gateway on that link: a typo, the wrong network, or the gateway is down |
| Change *Needs attention* | Lanvex could not find out what happened. Check `/var/lib/lanvex/changes/` on the server, then *Check again* |
| "The directory did not answer" | Unreachable directory, refused service account or certificate problem; *Settings, Sign-in, Test directory* and the log say which |
| "Stored credentials could not be decrypted" | `LANVEX_SECRET_KEY` changed. Put the old key in `LANVEX_SECRET_KEY_FALLBACKS` |

More: [operations.md](../operations.md#troubleshooting), [core.md](../core.md#troubleshooting),
[changes.md](../changes.md#troubleshooting), [sign-in.md](../sign-in.md#troubleshooting).
What has been tested, and what has not: [status.md](../status.md).
