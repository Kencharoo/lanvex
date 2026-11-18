"""Changing a switch port's VLANs over SNMP (Q-BRIDGE-MIB): its untagged VLAN, which is also its PVID,
and its tagged VLANs. Used only for an approved change (switches/changes.py).

Q-BRIDGE-MIB keeps each VLAN's member ports as one bitmap (dot1qVlanStaticEgressPorts, and the
untagged ones in dot1qVlanStaticUntaggedPorts), so a change writes those of the VLANs the port joins or
leaves: the bitmaps as read just before, with only this port's bit changed. Every write is recorded
with the value it replaces, so it can be undone in reverse order.
"""

from dataclasses import dataclass
from typing import Any

from lanvex.integrations.snmp.client import SnmpError, SnmpSession
from lanvex.integrations.snmp.switch import (
    BASE_PORT_IFINDEX,
    Q_PVID,
    Q_STATIC_EGRESS,
    Q_STATIC_UNTAGGED,
    portlist,
)

# The switch's default route (RFC 1213 ipRouteNextHop for 0.0.0.0): the way back to Lanvex when it is
# in another network.
DEFAULT_ROUTE = "1.3.6.1.2.1.4.21.1.7.0.0.0.0"


class PlanError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class PortVlans:
    bridge_port: int
    pvid: int | None
    egress: dict[int, bytes]  # VLAN -> its member ports, as configured
    untagged: dict[int, bytes]  # VLAN -> the members it sends untagged

    def member(self, vlan: int) -> bool:
        return self.bridge_port in portlist(self.egress.get(vlan, b""))

    @property
    def untagged_vlans(self) -> list[int]:
        return sorted(
            v
            for v in self.egress
            if self.member(v) and self.bridge_port in portlist(self.untagged.get(v, b""))
        )

    @property
    def tagged_vlans(self) -> list[int]:
        return sorted(v for v in self.egress if self.member(v) and v not in self.untagged_vlans)

    def summary(self) -> dict[str, Any]:
        return {"pvid": self.pvid, "untagged": self.untagged_vlans, "tagged": self.tagged_vlans}


def _single(rows: list[tuple[tuple[int, ...], Any]]) -> dict[int, Any]:
    return {oid[0]: v for oid, v in rows if len(oid) == 1}


async def read_port(s: SnmpSession, if_index: int) -> PortVlans:
    """The port's VLAN configuration as the switch has it now."""
    bridge = {idx: bp for bp, idx in _single(await s.walk(BASE_PORT_IFINDEX)).items() if isinstance(idx, int)}
    if if_index not in bridge:
        raise SnmpError("unsupported", "The switch does not list this port in its bridge (BRIDGE-MIB).")
    egress = {v: b for v, b in _single(await s.walk(Q_STATIC_EGRESS)).items() if isinstance(b, bytes)}
    if not egress:
        raise SnmpError("unsupported", "The switch has no VLAN table that can be changed (Q-BRIDGE-MIB).")
    untagged = {v: b for v, b in _single(await s.walk(Q_STATIC_UNTAGGED)).items() if isinstance(b, bytes)}
    bp = bridge[if_index]
    pvid = (await s.get(f"{Q_PVID}.{bp}")).get(f"{Q_PVID}.{bp}")
    return PortVlans(bp, pvid if isinstance(pvid, int) else None, egress, untagged)


def with_port(bits: bytes, port: int, on: bool) -> bytes:
    """`bits` with bridge port `port` (counted from 1, the first byte's high bit) on or off."""
    out = bytearray(bits)
    i = (port - 1) // 8
    if len(out) <= i:
        out.extend(bytes(i + 1 - len(out)))
    mask = 0x80 >> ((port - 1) % 8)
    out[i] = out[i] | mask if on else out[i] & ~mask
    return bytes(out)


def _encode(value: bytes | int) -> dict[str, Any]:
    return {"hex": value.hex()} if isinstance(value, bytes) else {"int": value}


def decode(value: dict[str, Any]) -> bytes | int:
    return bytes.fromhex(value["hex"]) if "hex" in value else int(value["int"])


def plan(state: PortVlans, port: str, untagged: int, tagged: list[int]) -> list[dict[str, Any]]:
    """The writes that give the port `untagged` (and that PVID) and `tagged`, in a safe order: join the
    new VLANs, then move the PVID, then leave the old ones. Each write: {text, binds, undo}."""
    tagged = sorted(set(tagged) - {untagged})
    missing = [v for v in [untagged, *tagged] if v not in state.egress]
    if missing:
        raise PlanError(
            f"VLAN {', '.join(map(str, missing))} does not exist on the switch. Create it there first."
        )
    bp = state.bridge_port
    ops: list[dict[str, Any]] = []

    def vlan(v: int, member: bool, untag: bool, text: str) -> None:
        eg_before = state.egress[v]
        un_before = state.untagged.get(v, bytes(len(eg_before)))
        eg_after, un_after = with_port(eg_before, bp, member), with_port(un_before, bp, untag)
        if (eg_after, un_after) == (eg_before, un_before):
            return
        eg, un = f"{Q_STATIC_EGRESS}.{v}", f"{Q_STATIC_UNTAGGED}.{v}"
        # Untagged members must be members: joining writes the members first, leaving the untagged first.
        order = [(eg, eg_before, eg_after), (un, un_before, un_after)]
        if not member or (state.member(v) and not untag):
            order.reverse()
        order = [w for w in order if w[1] != w[2]]
        ops.append(
            {
                "text": text,
                "binds": [[oid, _encode(after)] for oid, _, after in order],
                "undo": [[oid, _encode(before)] for oid, before, _ in reversed(order)],
            }
        )

    vlan(untagged, True, True, f"VLAN {untagged}: {port} untagged")
    for v in tagged:
        vlan(v, True, False, f"VLAN {v}: {port} tagged")
    if state.pvid != untagged:
        pvid = f"{Q_PVID}.{bp}"
        ops.append(
            {
                "text": f"{port}: PVID {state.pvid if state.pvid is not None else '—'} → {untagged}",
                "binds": [[pvid, _encode(untagged)]],
                "undo": [[pvid, _encode(state.pvid)]] if state.pvid is not None else [],
            }
        )
    for v in sorted(state.egress):
        if v != untagged and v not in tagged and state.member(v):
            vlan(v, False, False, f"VLAN {v}: {port} removed")
    return ops


def drifted(state: PortVlans, ops: list[dict[str, Any]]) -> list[str]:
    """The planned writes whose current value on the switch is no longer the one they were planned
    against (someone changed the VLAN since the preview)."""
    now: dict[str, bytes | int | None] = {}
    for v, b in state.egress.items():
        now[f"{Q_STATIC_EGRESS}.{v}"] = b
    for v, b in state.untagged.items():
        now[f"{Q_STATIC_UNTAGGED}.{v}"] = b
    now[f"{Q_PVID}.{state.bridge_port}"] = state.pvid
    return [
        op["text"] for op in ops if any(not _same(now.get(oid), decode(value)) for oid, value in op["undo"])
    ]


def _same(a: bytes | int | None, b: bytes | int) -> bool:
    """Port lists of different lengths are the same when the longer one adds only zeros."""
    if not isinstance(a, bytes) or not isinstance(b, bytes):
        return a == b
    n = max(len(a), len(b))
    return a.ljust(n, b"\0") == b.ljust(n, b"\0")


async def write(s: SnmpSession, op: dict[str, Any]) -> None:
    """One planned write, in one request."""
    await s.set(*[(oid, decode(value)) for oid, value in op["binds"]])


async def undo(s: SnmpSession, ops: list[dict[str, Any]], done: int, log: Any) -> None:
    """Undo the first `done` writes of `ops`, the last one first."""
    for op in reversed(ops[:done]):
        if op["undo"]:
            await s.set(*[(oid, decode(value)) for oid, value in op["undo"]])
            log(f"Undone: {op['text']}.")
