# Switches

Lanvex reads managed switches over SNMP: their ports, VLANs, MAC address table and LLDP neighbours.
From that it shows which device is plugged into which port, and compares it with the cables the
documentation (Lanvex Core or NetBox) records. Reading is all it does: Lanvex only sends SNMP GET
requests, never SET, and nothing is written to the documentation unless an administrator records it
after a preview.

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
| MAC address table: which MAC address on which port, in which VLAN | Q-BRIDGE-MIB, else BRIDGE-MIB; on Cisco IOS, per VLAN (community `@vlan` or SNMPv3 context `vlan-<id>`) |
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
documentation, and the addresses servers and switches saw it with. Hosts in *Discovery* show the
switch port too.

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

## Monitoring

| Alert | When |
|---|---|
| **Unreachable** (critical) | The switch did not answer the last *n* checks (default 3). Lanvex asks every switch for its uptime every two minutes |
| **Read failed** (warning) | A read was refused: credentials rejected, address not allowed, or the switch let the sign-in see nothing |

Both end by themselves, like the servers' ([monitoring.md](monitoring.md)).

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
