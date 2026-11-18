# Switches

Lanvex reads managed switches over SNMP: their ports, VLANs, MAC address table and LLDP neighbours.
From that it shows which device is plugged into which port, and compares it with the cables the
documentation (Lanvex Core or NetBox) records. Lanvex reads with SNMP GET requests; it writes to a
switch (SNMP SET) only to change a port's VLANs, where that is allowed, after an administrator
approved the change. Nothing is written to the documentation unless an administrator records it after
a preview.

## Adding a switch

*Switches, Add switch*: the address, the SNMP port (161), and the credentials. *Test connection* signs
in and shows what the switch says it is; *Add and read* stores the switch and reads it once.

| | |
|---|---|
| **SNMPv3** (recommended) | A user with authentication (SHA-1, SHA-2 or MD5) and privacy (AES-128/192/256, DES). AES-192 and AES-256 come in two variants: the one most switches and Net-SNMP use, and Cisco's (*AES-256 (Cisco)*). If one does not work, try the other |
| **SNMPv2c** | A read-only community. It travels unencrypted, so use it only on a management network |

The credentials are stored encrypted with `LANVEX_SECRET_KEY` and never shown again; the switch page
shows the version, the user and the protocols. `LANVEX_SERVER_ALLOWED_NETWORKS` limits switch addresses
as it does servers'.

Give Lanvex a read-only SNMPv3 user, with authentication and privacy, whose view is the whole MIB
tree (`iso`, `.1`). For example:

```text
# Cisco IOS / IOS-XE
snmp-server view LANVEX iso included
snmp-server group LANVEX v3 priv read LANVEX
snmp-server user lanvex LANVEX v3 auth sha <auth password> priv aes 128 <privacy password>

# Juniper Junos
set snmp v3 usm local-engine user lanvex authentication-sha authentication-password <auth password>
set snmp v3 usm local-engine user lanvex privacy-aes128 privacy-password <privacy password>
set snmp v3 vacm security-to-group security-model usm security-name lanvex group lanvex
set snmp v3 vacm access group lanvex default-context-prefix security-model usm security-level privacy read-view all
set snmp view all oid .1 include
```

Turn on LLDP on the switches too: it tells Lanvex which switch is connected to which, and what servers
announce about themselves.

**Linking the switch** to its device in the documentation (on adding it, or *Edit* later) lets Lanvex
compare each port with the cable recorded on the device's interface of the same name.

## What is read

| | From |
|---|---|
| Name, model, uptime, location | SNMPv2-MIB |
| Ports: name, description, state, speed, MAC address | IF-MIB |
| Link aggregations and their member ports | IEEE8023-LAG-MIB |
| VLANs, each port's untagged and tagged VLANs and PVID | Q-BRIDGE-MIB; on Cisco IOS without it, CISCO-VTP-MIB and CISCO-VLAN-MEMBERSHIP-MIB |
| MAC address table: which MAC address on which port, in which VLAN | Q-BRIDGE-MIB, else BRIDGE-MIB; on Cisco IOS, per VLAN (community `@vlan`, or SNMPv3 contexts `vlan-<id>`, which the SNMP group must allow: `snmp-server group LANVEX v3 priv context vlan- match prefix read LANVEX`) |
| LLDP neighbours | LLDP-MIB |
| ARP and neighbour tables (layer 3 switches) | IP-MIB |

What a switch does not have is noted on its page and skipped. Switches are read every 15 minutes
(`LANVEX_SWITCH_READ_INTERVAL_MINUTES`, or per switch), and with *Read now*.

MAC addresses stay listed with the time they were last seen (`LANVEX_DISCOVERY_RETENTION_DAYS`), so a
device that is switched off still shows where it was plugged in.

## Which port a device is on

A MAC address is learned on the port the device is plugged into, and on every uplink between that port
and the switch reading it. Lanvex counts a port as an uplink when it leads to another switch Lanvex
reads: LLDP names that switch on the port, or that switch's own MAC address is learned there. A device
is placed on the one port that is not an uplink. If there are several (a switch Lanvex does not read
sits in between), it goes to the port with the fewest devices.

The device is then named from what Lanvex knows: a server it reads (by the MAC addresses of the
server's interfaces), a device or virtual machine whose interface has that MAC address in the
documentation, and the addresses servers and switches saw it with. Devices nobody documented show
who made them, from their MAC address (the IEEE registry); phones and laptops that use a random MAC
address per network show *Randomized address*. Hosts in *Discovery* show the switch port and maker
too.

## Compared with the documentation

| Result | Meaning |
|---|---|
| **Documented** | The cable recorded on this port leads to what is seen there |
| **Different cable** | A cable is recorded, to something else; or the device seen has a cable recorded elsewhere |
| **No cable recorded** | What is seen is documented, the cable is not. It can be recorded: *Record cable…* shows the cable that would be added, and adds exactly that |
| **Unknown device** | Something is connected that the documentation does not know |
| **Several devices** | More than one documented device on one port: something Lanvex cannot see is in between (an unmanaged switch, a phone with a PC behind it) |
| **Virtual machines** | Only virtual machines seen: their host is not documented with this MAC address |
| **Port not documented** | The switch's device has no interface of this name |
| **Switch not linked** | Link the switch to its device first |

What LLDP says comes first: a neighbour that names itself and its port is matched with the device of
that name and its interface. Cables between two switches Lanvex reads are compared the same way. A
link aggregation cannot have a cable: Lanvex says so and leaves the cables to its member ports.

*Discovery, Switch ports* lists every port with something seen on it, by default those that differ from
the documentation. The topology draws connections switches see that no cable records as dotted lines
(amber: no cable recorded; red: recorded differently), apart from the documented cables, and marks the
switches Lanvex reads.

## Changing a port's VLANs

Off unless both are on: `LANVEX_AUTOMATION_ENABLED=true` for the installation, and *Allow VLAN changes
from Lanvex* in the switch's settings. Then the pencil next to a port's VLANs sets its untagged VLAN
(which is also its PVID) and its tagged VLANs.

1. **Preview.** Lanvex reads the port and lists the SNMP SET requests it would send, in order: join the
   new VLANs, move the PVID, leave the old ones. Nothing is written yet.
2. **Approval.** An administrator types the port's name (another administrator than the one who
   prepared it, with `LANVEX_CHANGES_REQUIRE_SECOND_ADMIN=true`).
3. **Writing.** Lanvex reads the port again first and stops if anything it would write changed since
   the preview, then sends one SET request per step.
4. **Read back.** The port must have exactly the requested VLANs and PVID. If the switch refuses a step
   or the result differs, the steps already written are undone, last first; the change ends as *Rolled
   back*, with an alert.

Not changed from Lanvex: uplinks to another switch Lanvex reads, link aggregations and their member
ports, anything but physical ports, and the port the switch reaches Lanvex through (where it learned
the MAC address of Lanvex's address, or of its default gateway). VLANs must exist on the switch. One
open change per switch.

Shown before approval: the devices on the port that lose their connection, a switch or access point
behind the port, and that the switch may forget the change on restart: Lanvex does not save the
switch's configuration.

**On the switch:** changes need SNMP write access to the Q-BRIDGE-MIB (`1.3.6.1.2.1.17.7`): on
SNMPv3 a user whose write view includes it, on v2c a read-write community. *Separate credentials for
changes* keeps the scheduled reads on a read-only user. Switches without writable Q-BRIDGE VLAN
tables (Cisco IOS among them, which uses its own MIBs) cannot be changed from Lanvex.

## Monitoring

| Alert | When | Ends |
|---|---|---|
| **Unreachable** (critical) | The switch did not answer the last *n* checks (default 3). Lanvex asks every switch for its uptime every two minutes | When it answers again |
| **Read failed** (warning) | A read was refused: credentials rejected, address not allowed, or the switch let the sign-in see nothing | With the next successful read |
| **Port down** (critical for an uplink, else warning) | A port that led to another device went down: an uplink to another switch Lanvex reads, or a port with an LLDP neighbour. Ports turned off on the switch are not reported | When the port is up again, or when acknowledged: then the port is no longer watched (unplugged on purpose) |
| **Unknown device** (info) | A device appeared on a port after the switch's first read and is documented nowhere (no interface with its MAC address, no fixed address, not a server Lanvex reads). A device already reported by a server or another switch is not reported again | When documented, or acknowledged |
| **Device moved** (info) | A documented device is now on another port than at the switch's last read | When acknowledged |
| **Change failed** (warning) | A port VLAN change was not applied, was undone, or ended in an unknown state | When acknowledged |

Unknown devices in some VLANs can be left out with `vlan:<id>` in the exclusions under *Settings,
Monitoring* (a guest VLAN, say). What a switch shows on its first read is its starting point: those
devices are not reported as new.

## Settings

| Variable | Default | |
|---|---|---|
| `LANVEX_SWITCH_READ_INTERVAL_MINUTES` | `15` | `0` turns scheduled switch reads off. Each switch can override it |
| `LANVEX_SNMP_TIMEOUT_SECONDS` | `2` | Per request |
| `LANVEX_SNMP_RETRIES` | `1` | Per request |

## Trying it without a switch

`cd backend && uv run python -m tests.snmp_lab` starts two switches in software on UDP ports 16101 and
16102 (SNMPv3 user `lanvex`, SHA `auth-pass-123`, AES `priv-pass-456`; or SNMPv2c community
`lanvex-test`). The end-to-end tests use them.
