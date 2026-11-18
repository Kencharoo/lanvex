# Networks: VLANs and DHCP on a server

A Linux server can be the router for several networks: one VLAN interface per network on its trunk to
the switch, an address in each, and DHCP handing out addresses to the devices there. Lanvex sets this
up from the network documentation, with the same safety as every other server change.

New in Lanvex 0.7 on servers whose network NetworkManager manages (Fedora, RHEL); since 0.8 also on
Ubuntu Server (netplan with systemd-networkd) and Debian (ifupdown); since 0.9 with
[access rules](#what-devices-may-reach): the Internet, other networks, destinations with ports and
services on the server, per network; since 1.0 with a [DNS filter](#dns-filter) per network and
[taking over from Netzverwaltung](#taking-over-from-netzverwaltung).

## How it works

1. **You document the network** in Lanvex (server page, *Networks*, *Add network*): VLAN ID, the
   interface name on the server, the trunk it sits on, the server's address in the network, and the
   DHCP pool. Lanvex writes this to Lanvex Core at once. Nothing on the server changes.
2. **Lanvex compares** what Lanvex Core documents with the server's last read: each network is *Not on
   the server yet*, *Differs on the server*, *On the server*, or *To be removed*, and so is its DHCP.
3. **You prepare the change.** One change covers every network on the server: the helper shows exactly
   which files it writes and which commands it runs.
4. **You approve it**, and the change runs like every other: backup, rollback timer, apply, checks
   (each VLAN interface exists with its address, is in its firewalld zone or allowed by ufw where those
   run, and dnsmasq answers DHCP), then confirmation, or the previous configuration comes back by itself.

| In Lanvex Core | What it means on the server |
|---|---|
| A virtual interface of the server's device, with a parent interface and one VLAN (802.1Q mode *Access*) | A VLAN interface, configured the way the server's network is (see below) |
| An IPv4 address on that interface | The server's address in the network; devices get it as gateway and DNS server |
| An IP range in that network, tagged `lanvex-dhcp`; custom field *DHCP lease time* (empty: 12h) | The DHCP pool (`/etc/dnsmasq.d/lanvex.conf`). A pool given exactly the bounds of an IP range Lanvex Core already has tags that range |
| An IP address in that network with the custom field *MAC address* | A fixed address: that device always gets this address. The address may be free or assigned to the device's interface in Lanvex Core (not the server's own); then the MAC address must be the one that interface has there, if it has one, and no other device's |
| The custom field *Access (Lanvex)* on the interface | What devices on the network may reach (below) |

The server page shows the leases devices hold (as of the last read), and *Make fixed* turns one into a
fixed address. Devices with a lease also appear in *Discovery*.

## Which servers

The VLAN interfaces are configured by whatever configures the trunk they sit on:

| The trunk is managed by | Typical system | Lanvex writes |
|---|---|---|
| NetworkManager | Fedora, RHEL, Ubuntu or Debian with NetworkManager | A profile per network, `/etc/NetworkManager/system-connections/lanvex-<name>.nmconnection` |
| netplan with systemd-networkd | Ubuntu Server | `/etc/netplan/60-lanvex.yaml` with all of Lanvex's VLANs, linked to the trunk's netplan definition |
| ifupdown | Debian | `/etc/network/interfaces.d/lanvex`, one stanza per network; the stanza creates and deletes the VLAN device itself, so Debian's `vlan` package is not needed |

Once Lanvex has VLANs on a server, it keeps configuring them the same way. Each tested on a real host
of that kind in CI, with a real DHCP client on the new VLAN ([status.md](status.md)).

Changing an interface's own addresses (*Change IP settings*) works on netplan and NetworkManager
servers; on ifupdown servers Lanvex manages networks only.

## What Lanvex writes on the server, and what it leaves alone

- Only its own files: the VLAN configuration above, `/etc/dnsmasq.d/lanvex.conf`, firewalld zones
  `lx-*` (one per network, allowing DHCP and DNS on the server) where firewalld runs, and
  `/etc/lanvex/networks.json`, the list of the VLAN interfaces it manages there (readable without
  root, so Lanvex's reads can tell its interfaces from others).
- With **ufw** active, three rules per network that serves DHCP (DHCP, and DNS over UDP and TCP, in on
  that interface), marked with the comment `lanvex:<interface>`. Lanvex removes exactly those rules
  with the network; a rollback restores ufw's rule files.
- It never changes another tool's interface, profile, netplan or ifupdown definition, zone or dnsmasq
  file. A network whose name is already in use on the server, or whose interface another dnsmasq file
  serves, is refused with the file that serves it.
- It serves DHCP only on VLAN interfaces it created: a second DHCP server on an existing network would
  disturb every device on it.
- [Protected interfaces](changes.md#protected-interfaces) are never touched, also when Lanvex created
  them: a network whose name is protected stays exactly as the server has it.
- The VLAN interface that carries Lanvex's own connection is never removed or moved.

## dnsmasq

- The first DHCP pool enables and starts the dnsmasq service. When the last pool is removed, dnsmasq
  goes back to how it ran before that first pool (stopped and disabled, or running for something else).
- If dnsmasq is not running yet and nothing else in its configuration chooses interfaces, Lanvex's
  file lists the interfaces it serves and binds only those (`bind-dynamic`). That is what lets dnsmasq
  run next to systemd-resolved on Ubuntu, which holds port 53 on 127.0.0.53.
- If dnsmasq already answers on every interface for another configuration (Debian's default once the
  package is installed), Lanvex adds its pools without restricting that, and says so in the preview.
- If dnsmasq answers on loopback alone (`local-service=host`, Fedora's and RHEL's default), Lanvex's
  interface lines end that: dnsmasq then answers on loopback and Lanvex's interfaces, bound to their
  addresses. After every change, Lanvex checks that dnsmasq answers DHCP, and DNS on the server's
  address in each of its networks.
- Taking over from Netzverwaltung, dnsmasq keeps answering where Netzverwaltung had it answer (its
  networks' interfaces, now listed in Lanvex's file).
- The choice is kept as long as Lanvex's file exists, so later changes do not flip it.

## Kea instead of dnsmasq

On a server that already runs Kea (`kea-dhcp4`), Lanvex can put its DHCP pools there instead. Kea's
configuration is one file, `kea-dhcp4.conf`, so Lanvex writes its networks to a file of its own and
Kea includes it. An administrator sets that up once:

1. In `/etc/kea/kea-dhcp4.conf`, inside `"Dhcp4"`, add
   `"shared-networks": <?include "/etc/kea/lanvex-shared-networks.json"?>,`
   (Lanvex's networks; your own subnets stay where they are).
2. Make Kea listen on Lanvex's VLAN interfaces: `"interfaces-config": { "interfaces": [ "*" ] }`, or
   list them.
3. `echo '[]' | sudo tee /etc/kea/lanvex-shared-networks.json`, then restart Kea.

From then on, networks changes on that server write Kea's file (one shared network per Lanvex
network, with its pool, lease time, router, DNS servers and fixed addresses), check it with
`kea-dhcp4 -t` before applying, and reload Kea. After the change Lanvex checks that Kea runs and,
through its control socket where it has one, that it serves each network; a rollback puts the file
back and reloads Kea again.

- Kea serves DHCP only. Devices get the server's address as their DNS server unless the network
  says otherwise, so something on the server must answer DNS there (dnsmasq for DNS alone, Unbound,
  BIND); the preview says so.
- DNS filters and taking over from Netzverwaltung need dnsmasq, and are refused on a Kea server.
- Pools Lanvex already serves with dnsmasq stay with dnsmasq: a network is never served twice. Remove
  them, set up Kea, then add them again.
- Leases appear on the server page when Kea's lease file (`/var/lib/kea/kea-leases4.csv`) is readable
  by Lanvex's account (Debian's default; on Fedora and RHEL, `/var/lib/kea` is closed to others).

## What the server needs

- NetworkManager, netplan or ifupdown manages the trunk. On ifupdown, `/etc/network/interfaces`
  reads `/etc/network/interfaces.d` (the line `source /etc/network/interfaces.d/*`, Debian's default).
- The **dnsmasq service**: `dnf install dnsmasq` or `apt install dnsmasq`. NetworkManager brings only
  the dnsmasq program, which serves nothing by itself; Lanvex says so if that is all there is. On
  Ubuntu, installing dnsmasq next to systemd-resolved leaves it failed until Lanvex's first pool; that
  is expected.
- dnsmasq reads `/etc/dnsmasq.d` (the default on Fedora, RHEL, Debian and Ubuntu).
- The change helper in version 5 (Lanvex 1.0), with Python 3. After updating Lanvex, every
  server's helper shows as outdated: copy the new file to the server from the server page
  (*Configuration changes*) and run the commands shown there as an administrator of the server, as
  the first time ([changes.md](changes.md#2-on-each-server-install-the-helper)).
- The switch carries the VLAN to the server: the server's port is a trunk with that VLAN tagged.
  Lanvex does not configure switches.
- If dnsmasq already serves other networks on the server (Netzverwaltung's, for example), Lanvex's
  first pool restarts it: their DHCP and DNS pause for a moment, and the preview says so.
- A firewall Lanvex can use: firewalld, ufw, or the `nft` command (package nftables) for its own
  table. Without any of them Lanvex still creates networks, but cannot limit what their devices reach
  and refuses access rules that forward traffic. A hand-written nftables ruleset that drops incoming
  or forwarded traffic is pointed out in the preview: Lanvex's table cannot override it.

## What devices may reach

Devices on a Lanvex network get an address, the server as gateway and DNS server, and reach DHCP and
DNS on the server. Everything else is closed until you allow it: on the server page, *Networks*, the
table *What devices on each network may reach* has a row per network (like Netzverwaltung's matrix),
and *Edit* sets:

| Setting | Means |
|---|---|
| **The Internet** | Forwarded out of the server's uplink (the interface of its default route) and masqueraded. Private networks (10/8, 172.16/12, 192.168/16, 100.64/10, 169.254/16) stay closed, unless allowed below |
| **Another Lanvex network** on the server | Everything there, without masquerading (the server routes for both) |
| **Other networks and addresses** | A network (`192.168.178.0/24`, the main LAN) or one address (`192.168.178.3`, a switch's web interface), on every port or only on the ones listed (`443/tcp, 80/tcp`). Masqueraded, so answers come back through the server even where that network does not route to Lanvex's networks |
| **On the server itself** | Ports besides DHCP and DNS: SSH, a web interface, `8100/tcp` for Lanvex on KlassenServer |

Answers to allowed connections always come back; a rule is about who may start a connection. Rules
are one way: allowing VLAN 50 to reach VLAN 60 does not let VLAN 60 reach VLAN 50. Saved rules are
documented in Lanvex Core at once and reach the server with the next change you prepare and approve,
like everything else here. A network that carries Lanvex's own SSH connection must keep SSH to the
server; Lanvex refuses rules that would cut it off.

On the server, by the firewall it runs:

| Firewall | Lanvex writes |
|---|---|
| firewalld | The network's zone `lx-…` lists the ports; a policy `lxf-<network>-<code>` from that zone to each zone the network may reach, with masquerade towards zones that are not Lanvex's. The configuration is checked (`firewall-cmd --check-config`) before firewalld reloads it |
| ufw (active) | Rules marked `lanvex:<interface>`: `ufw allow in on …` for the server, `ufw route …` for forwarding, in the order ufw needs (it takes the first match). Masquerading in Lanvex's own nftables table, since ufw has no command for it |
| Neither | Lanvex's own nftables table `inet lanvex` (`/etc/lanvex/firewall.nft`, loaded at boot by `lanvex-firewall.service`): input and forwarding for Lanvex's networks only, masquerading. Other traffic is not touched |

IP forwarding is switched on in `/etc/sysctl.d/60-lanvex.conf` while a network may reach beyond the
server, and goes back to how it was once none does. On ufw, ufw's own defaults also let ping (ICMP
echo) through between networks; Lanvex does not change ufw's files.

## DNS filter

In the same editor, **Block names for devices on this network** takes a list of domains (each blocks
the domain and every name below it: `tiktok.com` also blocks `www.tiktok.com`) and exceptions that are
answered anyway (`edu.tiktok.com`). Devices asking for a blocked name get "no such name".

As in Netzverwaltung, dnsmasq cannot tell networks apart, so each filtered network gets a dnsmasq of its
own (`lanvex-dns@<network>.service`, configured in `/etc/lanvex/dns/<network>.conf`). It answers on an
address of Lanvex's dummy link `lxdns` (`10.99.53.1`, `.2`, … in the order of the networks' names),
which DHCP hands to the network's devices as their DNS server, and passes every name it does not block
to the main dnsmasq, so local device names keep working. The main dnsmasq is told to leave `lxdns`
alone (and, where it would otherwise bind every address, to bind each interface's own addresses).

- A DNS filter needs Lanvex's DHCP pool on the network: that is how devices learn to use it.
- `10.99.53.0/24` must be free on the server; Lanvex refuses the filter otherwise and says what uses it.
- Devices notice a new block once their own DNS cache expires. Devices with a DNS server set by hand,
  or using DNS over HTTPS, are not filtered.

## Taking over from Netzverwaltung

Lanvex replaces Netzverwaltung (KlassenServer's tool for VLANs, DHCP, access and DNS filtering) without
rebuilding the networks by hand, in two steps:

1. **Import** (server page, *Networks*, *Import from Netzverwaltung*). The change helper reads
   `/etc/netzverwaltung/netz.yaml` and Netzverwaltung's firewalld policies, and Lanvex documents each
   of its networks in Lanvex Core: VLAN, the server's address, DHCP pool and lease time, fixed
   addresses, and access rules (the Internet, the main LAN and the switch's web interface as
   destinations, other VLANs, services on the server as ports, the DNS filter). The report says what
   did not carry over: block lists by name, services Lanvex does not know by name. Nothing on the
   server changes. A network Lanvex Core already documents keeps what it has (VLAN, address, pool,
   fixed addresses, access rules) and gets what it lacks; the report names each item added or kept.
2. **Take over** (*Prepare takeover from Netzverwaltung*). One change replaces all of Netzverwaltung's
   networks at once: its NetworkManager profiles `vlan-<name>`, its zones `<name>`, its `nv-…`
   policies and `nv-…` dnsmasq files are deleted (and backed up), `netzverwaltung.service` and
   `dnsmasq-filter.service` are stopped and disabled, and Lanvex's own profiles, zones, policies,
   dnsmasq file and filters take their place. Approve it like any change; with *My browser confirms
   changes*, keep it from a browser on the LAN. If anything fails, or you undo it, the server puts
   Netzverwaltung back exactly as it was. Lanvex first compares Netzverwaltung's setup with Lanvex
   Core and refuses the takeover, naming what is missing, if it would drop a network, a DHCP pool or
   a fixed address, or leave a network with access in Netzverwaltung without any access rules. Access
   rules narrower than Netzverwaltung's are shown in the preview.

Before the takeover:

- Remove Netzverwaltung's network names from the server's protected interfaces (`bw gast iot uebung`
  on KlassenServer): protected names are never changed.
- Files Netzverwaltung leaves to you stay as they are (policies without `nv-`, such as `lan-to-switch`,
  and dnsmasq files without `nv-`, such as `switch-mgmt.conf`). A policy of yours that names one of
  Netzverwaltung's zones would stop working when the zone goes, so Lanvex refuses the takeover and
  says which policy to change to Lanvex's zone (`lx-<name>`).
- Each VLAN is down for a moment while its new profile comes up; devices keep their addresses (the
  leases stay where dnsmasq keeps them).

Netzverwaltung itself stays installed but stopped. Once you are sure, uninstall it.

## Lanvex on the server it changes

On a server that runs Lanvex itself (KlassenServer), Lanvex reaches the server only through loopback.
Turn on *My browser confirms changes* in *Edit server*: after the checks pass, you keep the change in
your browser, through one of the server's own addresses; without that, the server undoes it. See
[changes.md](changes.md#when-lanvex-reaches-the-server-indirectly).

## Your own NetBox instead of Lanvex Core

Lanvex Core has everything set up. With your own NetBox, create three custom fields (not required)
and give Lanvex's token these permissions:

| What | Details |
|---|---|
| Custom field `mac_address` on *IPAM > IP address* | Type *Text*, validation `^([0-9a-f]{2}:){5}[0-9a-f]{2}$` |
| Custom field `dhcp_lease_time` on *IPAM > IP range* | Type *Text*, validation `^([1-9][0-9]{0,4}[mhd]\|infinite)$` |
| Custom field `lanvex_access` on *DCIM > Interface* | Type *JSON*; Lanvex writes and checks it |
| IP ranges | view, add, change, delete |
| Interfaces | view, add, change, delete |
| VLANs, prefixes, IP addresses | view, add, change, delete (already needed) |
| Tags | view, add (Lanvex creates `lanvex-dhcp` once) |

The *Networks* section says which fields are missing.
