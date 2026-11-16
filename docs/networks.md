# Networks: VLANs and DHCP on a server

A Linux server can be the router for several networks: one VLAN interface per network on its trunk to
the switch, an address in each, and DHCP handing out addresses to the devices there. Lanvex sets this
up from the network documentation, with the same safety as every other server change.

New in Lanvex 0.7 on servers whose network NetworkManager manages (Fedora, RHEL); since 0.8 also on
Ubuntu Server (netplan with systemd-networkd) and Debian (ifupdown). Access rules (what each network
may reach) follow in 0.9; see [roadmap.md](roadmap.md).

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
| An IP range in that network, tagged `lanvex-dhcp`; custom field *DHCP lease time* (empty: 12h) | The DHCP pool (`/etc/dnsmasq.d/lanvex.conf`) |
| An IP address in that network with the custom field *MAC address* | A fixed address: that device always gets this address |

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
- The choice is kept as long as Lanvex's file exists, so later changes do not flip it.

## What the server needs

- NetworkManager, netplan or ifupdown manages the trunk. On ifupdown, `/etc/network/interfaces`
  reads `/etc/network/interfaces.d` (the line `source /etc/network/interfaces.d/*`, Debian's default).
- The **dnsmasq service**: `dnf install dnsmasq` or `apt install dnsmasq`. NetworkManager brings only
  the dnsmasq program, which serves nothing by itself; Lanvex says so if that is all there is. On
  Ubuntu, installing dnsmasq next to systemd-resolved leaves it failed until Lanvex's first pool; that
  is expected.
- dnsmasq reads `/etc/dnsmasq.d` (the default on Fedora, RHEL, Debian and Ubuntu).
- The change helper in version 3 (Lanvex 0.8), with Python 3. After updating Lanvex, every
  server's helper shows as outdated: copy the new file to the server from the server page
  (*Configuration changes*) and run the commands shown there as an administrator of the server, as
  the first time ([changes.md](changes.md#2-on-each-server-install-the-helper)).
- The switch carries the VLAN to the server: the server's port is a trunk with that VLAN tagged.
  Lanvex does not configure switches.
- If dnsmasq already serves other networks on the server (Netzverwaltung's, for example), Lanvex's
  first pool restarts it: their DHCP and DNS pause for a moment, and the preview says so.
- Firewall: with firewalld, Lanvex puts each network in its own zone; with ufw, it adds its rules.
  A hand-written nftables firewall that drops incoming traffic is pointed out in the preview: allow
  DHCP (UDP 67) and DNS (port 53) on the new interfaces yourself until Lanvex 0.9.

## Devices on the new network

They get an address, the server as gateway and DNS server, and reach DHCP and DNS on the server. That
is all for now: reaching other networks, the main LAN or the Internet through the server needs access
rules, which come with Lanvex 0.9. Until then, add firewalld policies (or ufw route rules) for a
Lanvex network yourself if you need them; Lanvex leaves rules it did not write alone.

## Lanvex on the server it changes

On a server that runs Lanvex itself (KlassenServer), Lanvex reaches the server only through loopback.
Turn on *My browser confirms changes* in *Edit server*: after the checks pass, you keep the change in
your browser, through one of the server's own addresses; without that, the server undoes it. See
[changes.md](changes.md#when-lanvex-reaches-the-server-indirectly).

## Your own NetBox instead of Lanvex Core

Lanvex Core has everything set up. With your own NetBox, create two custom fields (text, not required)
and give Lanvex's token these permissions:

| What | Details |
|---|---|
| Custom field `mac_address` on *IPAM > IP address* | Validation `^([0-9a-f]{2}:){5}[0-9a-f]{2}$` |
| Custom field `dhcp_lease_time` on *IPAM > IP range* | Validation `^([1-9][0-9]{0,4}[mhd]\|infinite)$` |
| IP ranges | view, add, change, delete |
| Interfaces | view, add, change, delete |
| VLANs, prefixes, IP addresses | view, add, change, delete (already needed) |
| Tags | view, add (Lanvex creates `lanvex-dhcp` once) |

The *Networks* section says which fields are missing.
