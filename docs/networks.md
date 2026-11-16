# Networks: VLANs and DHCP on a server

A Linux server can be the router for several networks: one VLAN interface per network on its trunk to
the switch, an address in each, and DHCP handing out addresses to the devices there. Lanvex sets this
up from the network documentation, with the same safety as every other server change.

New in Lanvex 0.7, on servers whose network NetworkManager manages (Fedora, RHEL, Ubuntu with
NetworkManager). Access rules (what each network may reach) follow in 0.9; see [roadmap.md](roadmap.md).

## How it works

1. **You document the network** in Lanvex (server page, *Networks*, *Add network*): VLAN ID, the
   interface name on the server, the trunk it sits on, the server's address in the network, and the
   DHCP pool. Lanvex writes this to Lanvex Core at once. Nothing on the server changes.
2. **Lanvex compares** what Lanvex Core documents with the server's last read: each network is *Not on
   the server yet*, *Differs on the server*, *On the server*, or *To be removed*, and so is its DHCP.
3. **You prepare the change.** One change covers every network on the server: the helper shows exactly
   which files it writes and which commands it runs.
4. **You approve it**, and the change runs like every other: backup, rollback timer, apply, checks
   (each VLAN interface exists with its address, is in its firewalld zone, and dnsmasq answers DHCP),
   then confirmation, or the previous configuration comes back by itself.

| In Lanvex Core | What it means on the server |
|---|---|
| A virtual interface of the server's device, with a parent interface and one VLAN (802.1Q mode *Access*) | A VLAN interface (NetworkManager profile `lanvex-<name>`) |
| An IPv4 address on that interface | The server's address in the network; devices get it as gateway and DNS server |
| An IP range in that network, tagged `lanvex-dhcp`; custom field *DHCP lease time* (empty: 12h) | The DHCP pool (`/etc/dnsmasq.d/lanvex.conf`) |
| An IP address in that network with the custom field *MAC address* | A fixed address: that device always gets this address |

The server page shows the leases devices hold (as of the last read), and *Make fixed* turns one into a
fixed address. Devices with a lease also appear in *Discovery*.

## What Lanvex writes on the server, and what it leaves alone

- Only its own files: NetworkManager profiles `lanvex-*`, firewalld zones `lx-*` (one per network,
  allowing DHCP and DNS on the server), and `/etc/dnsmasq.d/lanvex.conf`.
- It never changes another tool's interface, profile, zone or dnsmasq file. A network whose name is
  already in use on the server, or whose interface another dnsmasq file serves, is refused with the
  file that serves it.
- It serves DHCP only on VLAN interfaces it created: a second DHCP server on an existing network would
  disturb every device on it.
- [Protected interfaces](changes.md#protected-interfaces) are never touched, also when Lanvex created
  them: a network whose name is protected stays exactly as the server has it.
- The VLAN interface that carries Lanvex's own connection is never removed or moved.
- The first DHCP pool enables and starts the dnsmasq service. Without one, Lanvex does not stop it.

## What the server needs

- NetworkManager manages its network (`nmcli general` answers).
- The **dnsmasq service**: `dnf install dnsmasq` or `apt install dnsmasq`. NetworkManager brings only
  the dnsmasq program, which serves nothing by itself; Lanvex says so if that is all there is.
- dnsmasq reads `/etc/dnsmasq.d` (the default on Fedora, RHEL, Debian and Ubuntu).
- The change helper in version 2 (Lanvex 0.7). After updating Lanvex, every server's helper shows as
  outdated: install it again from the server page (*Configuration changes*), as the first time.
- With firewalld, nothing else: Lanvex puts each network in its own zone. Without firewalld, nothing
  blocks DHCP.

## Devices on the new network

They get an address, the server as gateway and DNS server, and reach DHCP and DNS on the server. That
is all in 0.7: reaching other networks, the main LAN or the Internet through the server needs access
rules, which come with Lanvex 0.9. Until then, add firewalld policies for a Lanvex network yourself
if you need them; Lanvex leaves policies it did not write alone.

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
