"""TEST DATA: what two switches answer over SNMP, as OID tables for tests/snmp_agent.py.

sw-access (Q-BRIDGE-MIB, LLDP with its own port numbering) and sw-core, joined by an uplink: sw-access's
Po1 (a LAG of Gi1/0/7 and Gi1/0/8) to sw-core's Gi1/0/24. Devices:

    A  02:00:00:00:00:0a  sw-access Gi1/0/1, VLAN 10 (pc-a)
    B  02:00:00:00:00:0b  sw-access Gi1/0/2, VLAN 10 (not documented)
    D  02:00:00:00:00:0d  sw-access Gi1/0/3, VLAN 10 (web-01, also announces itself over LLDP)
    E  02:00:00:00:00:0e  sw-access Gi1/0/5, VLAN 20 (phone)
    F  02:00:00:00:00:0f  sw-access Gi1/0/5, VLAN 20 (pc-f, behind the phone)
    C  02:00:00:00:00:0c  sw-core Gi1/0/1, VLAN 10 (router)

Each MAC is also learned on the uplink, as on real switches.
"""

from typing import Any

ACCESS_MAC, CORE_MAC = "00:11:22:aa:00:ff", "00:11:22:bb:00:ff"
A, B, C, D, E, F = (f"02:00:00:00:00:0{x}" for x in "abcdef")


def raw(mac: str) -> bytes:
    return bytes(int(b, 16) for b in mac.split(":"))


def oid(mac: str) -> str:
    return ".".join(str(b) for b in raw(mac))


def portlist(ports: set[int], size: int = 4) -> bytes:
    out = bytearray(size)
    for p in ports:
        out[(p - 1) // 8] |= 0x80 >> ((p - 1) % 8)
    return bytes(out)


def system(name: str, descr: str, bridge_mac: str) -> dict[str, Any]:
    return {
        "1.3.6.1.2.1.1.1.0": descr,
        "1.3.6.1.2.1.1.2.0": ("oid", "1.3.6.1.4.1.99999.1"),
        "1.3.6.1.2.1.1.3.0": ("ticks", 360000),
        "1.3.6.1.2.1.1.5.0": name,
        "1.3.6.1.2.1.1.6.0": "Rack 1",
        "1.3.6.1.2.1.17.1.1.0": raw(bridge_mac),
    }


def iface(
    idx: int, name: str, descr: str, kind: int = 6, up: bool = True, alias: str = "", mac: str | None = None
) -> dict[str, Any]:
    return {
        f"1.3.6.1.2.1.2.2.1.2.{idx}": descr,
        f"1.3.6.1.2.1.2.2.1.3.{idx}": kind,
        f"1.3.6.1.2.1.2.2.1.5.{idx}": ("gauge", 1_000_000_000),
        f"1.3.6.1.2.1.2.2.1.6.{idx}": raw(mac) if mac else b"",
        f"1.3.6.1.2.1.2.2.1.7.{idx}": 1,
        f"1.3.6.1.2.1.2.2.1.8.{idx}": 1 if up else 2,
        f"1.3.6.1.2.1.31.1.1.1.1.{idx}": name,
        f"1.3.6.1.2.1.31.1.1.1.15.{idx}": ("gauge", 1000),
        f"1.3.6.1.2.1.31.1.1.1.18.{idx}": alias,
    }


def qfdb(vlan: int, mac: str, bridge_port: int, status: int = 3) -> dict[str, Any]:
    return {
        f"1.3.6.1.2.1.17.7.1.2.2.1.2.{vlan}.{oid(mac)}": bridge_port,
        f"1.3.6.1.2.1.17.7.1.2.2.1.3.{vlan}.{oid(mac)}": status,
    }


def lldp_remote(
    local_port: int,
    index: int,
    chassis: str,
    port_id: str,
    port_desc: str,
    name: str,
    caps: int,
    address: str | None = None,
    port_subtype: int = 5,
) -> dict[str, Any]:
    base = "1.0.8802.1.1.2.1.4.1.1"
    key = f"0.{local_port}.{index}"
    out = {
        f"{base}.4.{key}": 4,
        f"{base}.5.{key}": raw(chassis),
        f"{base}.6.{key}": port_subtype,
        f"{base}.7.{key}": raw(port_id) if port_subtype == 3 else port_id,
        f"{base}.8.{key}": port_desc,
        f"{base}.9.{key}": name,
        f"{base}.10.{key}": f"{name} system",
        f"{base}.12.{key}": bytes([caps]),
    }
    if address:
        out[f"1.0.8802.1.1.2.1.4.2.1.3.{key}.1.4.{address}"] = 2
    return out


def access_switch() -> dict[str, Any]:
    t: dict[str, Any] = system("sw-access", "Example switch 8P, firmware 1.2", ACCESS_MAC)
    for i in range(1, 9):
        t |= iface(
            i,
            f"Gi1/0/{i}",
            f"GigabitEthernet1/0/{i}",
            up=i != 4,
            alias="desk 1" if i == 1 else "",
            mac=f"00:11:22:aa:00:0{i}",
        )
    t |= iface(9, "Po1", "Port-channel1", kind=161, mac="00:11:22:aa:00:09")
    t |= iface(100, "Vlan10", "Vlan10", kind=53, mac=ACCESS_MAC)
    for bp in (1, 2, 3, 4, 5, 6, 9):
        t[f"1.3.6.1.2.1.17.1.4.1.2.{bp}"] = bp
    t["1.2.840.10006.300.43.1.2.1.1.13.7"] = 9
    t["1.2.840.10006.300.43.1.2.1.1.13.8"] = 9
    for vlan, name, members, plain in (
        (1, "default", {6, 9}, {6, 9}),
        (10, "office", {1, 2, 3, 4, 9}, {1, 2, 3, 4}),
        (20, "voice", {5, 9}, {5}),
        (30, "lab", set(), set()),
    ):
        t[f"1.3.6.1.2.1.17.7.1.4.3.1.1.{vlan}"] = name
        t[f"1.3.6.1.2.1.17.7.1.4.2.1.3.0.{vlan}"] = vlan
        t[f"1.3.6.1.2.1.17.7.1.4.2.1.4.0.{vlan}"] = portlist(members)
        t[f"1.3.6.1.2.1.17.7.1.4.2.1.5.0.{vlan}"] = portlist(plain)
        # configured (static) membership: what a change writes
        t[f"1.3.6.1.2.1.17.7.1.4.3.1.2.{vlan}"] = portlist(members)
        t[f"1.3.6.1.2.1.17.7.1.4.3.1.4.{vlan}"] = portlist(plain)
    for bp, pvid in ((1, 10), (2, 10), (3, 10), (4, 10), (5, 20), (6, 1), (9, 1)):
        t[f"1.3.6.1.2.1.17.7.1.4.5.1.1.{bp}"] = pvid
    t |= qfdb(10, A, 1) | qfdb(10, B, 2) | qfdb(10, D, 3) | qfdb(20, E, 5) | qfdb(20, F, 5)
    t |= qfdb(10, C, 9) | qfdb(1, CORE_MAC, 9)
    t |= qfdb(1, ACCESS_MAC, 0, status=4)  # the switch itself
    # LLDP, with port numbers of its own (101… for Gi1/0/1…, 109 for Po1)
    t["1.0.8802.1.1.2.1.3.1.0"] = 4
    t["1.0.8802.1.1.2.1.3.2.0"] = raw(ACCESS_MAC)
    t["1.0.8802.1.1.2.1.3.3.0"] = "sw-access.example.net"
    for i, name in [*((n, f"Gi1/0/{n}") for n in range(1, 9)), (9, "Po1")]:
        t[f"1.0.8802.1.1.2.1.3.7.1.2.{100 + i}"] = 5
        t[f"1.0.8802.1.1.2.1.3.7.1.3.{100 + i}"] = name
    t |= lldp_remote(109, 1, CORE_MAC, "Gi1/0/24", "uplink", "sw-core", 0x28)  # Bridge, Router
    t |= lldp_remote(103, 2, D, D, "eth0", "web-01", 0x01, "192.0.2.10", port_subtype=3)  # Station
    return t


def core_switch() -> dict[str, Any]:
    t: dict[str, Any] = system("sw-core", "Example core switch 24P", CORE_MAC)
    for i in (1, 2, 24):
        t |= iface(i, f"Gi1/0/{i}", f"GigabitEthernet1/0/{i}", mac=f"00:11:22:bb:00:{i:02d}")
        t[f"1.3.6.1.2.1.17.1.4.1.2.{i}"] = i
    t |= iface(100, "Vlan10", "Vlan10", kind=53, mac=CORE_MAC)
    t["1.3.6.1.2.1.17.7.1.4.3.1.1.10"] = "office"
    t["1.3.6.1.2.1.17.7.1.4.2.1.4.0.10"] = portlist({1, 24})
    t["1.3.6.1.2.1.17.7.1.4.2.1.5.0.10"] = portlist({1})
    t |= qfdb(10, C, 1) | qfdb(10, A, 24) | qfdb(10, B, 24) | qfdb(10, D, 24)
    t |= qfdb(20, E, 24) | qfdb(20, F, 24) | qfdb(1, ACCESS_MAC, 24)
    # No lldpLocPortTable: its LLDP port numbers are bridge ports.
    t["1.0.8802.1.1.2.1.3.1.0"] = 4
    t["1.0.8802.1.1.2.1.3.2.0"] = raw(CORE_MAC)
    t["1.0.8802.1.1.2.1.3.3.0"] = "sw-core"
    t |= lldp_remote(24, 1, ACCESS_MAC, "Po1", "Port-channel1", "sw-access.example.net", 0x20)
    # ARP on its VLAN interface
    t["1.3.6.1.2.1.4.22.1.2.100.10.10.0.5"] = raw(A)
    t["1.3.6.1.2.1.4.22.1.4.100.10.10.0.5"] = 3
    return t


def cisco_switch() -> tuple[dict[str, Any], dict[int, dict[str, Any]]]:
    """A Cisco IOS switch without Q-BRIDGE-MIB: VTP VLANs, and bridge tables per VLAN."""
    x = "02:00:00:00:00:99"
    t: dict[str, Any] = system("cat-1", "Cisco IOS Software, C2960", "00:11:22:cc:00:ff")
    t |= iface(1, "Gi0/1", "GigabitEthernet0/1") | iface(2, "Gi0/2", "GigabitEthernet0/2")
    for vlan, name, state in ((1, "default", 1), (10, "office", 1), (1002, "fddi-default", 1)):
        t[f"1.3.6.1.4.1.9.9.46.1.3.1.1.2.1.{vlan}"] = state
        t[f"1.3.6.1.4.1.9.9.46.1.3.1.1.4.1.{vlan}"] = name
    t["1.3.6.1.4.1.9.9.68.1.2.2.1.2.1"] = 10  # Gi0/1: access port in VLAN 10
    t["1.3.6.1.4.1.9.9.46.1.6.1.1.14.2"] = 1  # Gi0/2: trunking
    t["1.3.6.1.4.1.9.9.46.1.6.1.1.5.2"] = 1  # native VLAN 1
    enabled = bytearray(128)
    for v in (1, 10):
        enabled[v // 8] |= 0x80 >> (v % 8)
    t["1.3.6.1.4.1.9.9.46.1.6.1.1.4.2"] = bytes(enabled)
    vlans = {
        1: {"1.3.6.1.2.1.17.1.4.1.2.2": 2}
        | {f"1.3.6.1.2.1.17.4.3.1.2.{oid(x)}": 2, f"1.3.6.1.2.1.17.4.3.1.3.{oid(x)}": 3},
        10: {"1.3.6.1.2.1.17.1.4.1.2.1": 1, "1.3.6.1.2.1.17.1.4.1.2.2": 2}
        | {f"1.3.6.1.2.1.17.4.3.1.2.{oid(A)}": 1, f"1.3.6.1.2.1.17.4.3.1.3.{oid(A)}": 3},
    }
    return t, vlans
