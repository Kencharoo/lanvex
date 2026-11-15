"""What changed on a server between two reads.

Reads that find the same state as the previous one are not stored again (the previous snapshot is
marked as confirmed), so a server's snapshots are its history: each one is a state that differed
from the one before. `fingerprint` decides "the same"; `changes` says what differs, in words.
"""

import hashlib
import json
from typing import Any, Literal

from lanvex.integrations.linux.facts import HostFacts, Interface, Route
from lanvex.schemas import ApiModel

ChangeKind = Literal["added", "removed", "changed"]


class HistoryChange(ApiModel):
    kind: ChangeKind
    text: str


def _comparable(facts: dict[str, Any]) -> dict[str, Any]:
    """The facts without what changes on every read or means nothing for the configuration."""
    out = {k: v for k, v in facts.items() if k not in ("collected_at", "warnings")}
    out["interfaces"] = [
        {
            **{k: v for k, v in i.items() if k != "index"},
            "addresses": sorted(
                (
                    {k: v for k, v in a.items() if k not in ("tentative", "deprecated")}
                    for a in i.get("addresses", [])
                ),
                key=lambda a: a["address"],
            ),
        }
        for i in sorted(facts.get("interfaces", []), key=lambda i: i["name"])
    ]
    out["routes"] = sorted(facts.get("routes", []), key=lambda r: json.dumps(r, sort_keys=True))
    stack = dict(facts.get("stack") or {})
    stack.pop("evidence", None)
    out["stack"] = stack
    return out


def fingerprint(facts: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(_comparable(facts), sort_keys=True).encode()).hexdigest()


def _route_text(r: Route) -> str:
    via = f" via {r.gateway}" if r.gateway else ""
    dev = f" on {r.interface}" if r.interface else ""
    return f"{'Default route' if r.destination in ('0.0.0.0/0', '::/0') else r.destination}{via}{dev}"


def _interface_changes(old: Interface, new: Interface) -> list[HistoryChange]:
    out: list[HistoryChange] = []
    name = new.name
    before = {a.address for a in old.addresses}
    after = {a.address for a in new.addresses}
    out += [HistoryChange(kind="added", text=f"{name}: address {a} added") for a in sorted(after - before)]
    out += [
        HistoryChange(kind="removed", text=f"{name}: address {a} removed") for a in sorted(before - after)
    ]
    if old.admin_up != new.admin_up:
        out.append(HistoryChange(kind="changed", text=f"{name} switched {'on' if new.admin_up else 'off'}"))
    elif old.carrier != new.carrier:
        out.append(
            HistoryChange(
                kind="changed", text=f"{name} {'has a link again' if new.carrier else 'lost its link'}"
            )
        )
    if old.mtu != new.mtu:
        out.append(HistoryChange(kind="changed", text=f"{name}: MTU {old.mtu} → {new.mtu}"))
    if old.mac != new.mac:
        out.append(HistoryChange(kind="changed", text=f"{name}: MAC address {old.mac} → {new.mac}"))
    if old.master != new.master:
        out.append(
            HistoryChange(
                kind="changed",
                text=f"{name} joined {new.master}" if new.master else f"{name} left {old.master}",
            )
        )
    if old.managed_by != new.managed_by:
        out.append(
            HistoryChange(kind="changed", text=f"{name}: now managed by {new.managed_by or 'nothing'}")
        )
    return out


def changes(old: HostFacts, new: HostFacts) -> list[HistoryChange]:
    out: list[HistoryChange] = []
    o, n = old.system, new.system
    if o.hostname != n.hostname:
        out.append(HistoryChange(kind="changed", text=f"Host name {o.hostname} → {n.hostname}"))
    if o.os.pretty_name != n.os.pretty_name:
        out.append(
            HistoryChange(kind="changed", text=f"Operating system {o.os.pretty_name} → {n.os.pretty_name}")
        )
    if o.kernel != n.kernel:
        out.append(HistoryChange(kind="changed", text=f"Kernel {o.kernel} → {n.kernel}"))
    if old.stack.primary != new.stack.primary:
        out.append(
            HistoryChange(
                kind="changed", text=f"Network configuration: {old.stack.primary} → {new.stack.primary}"
            )
        )

    before = {i.name: i for i in old.interfaces}
    after = {i.name: i for i in new.interfaces}
    for name in sorted(after.keys() - before.keys()):
        addrs = ", ".join(a.address for a in after[name].addresses)
        out.append(
            HistoryChange(kind="added", text=f"Interface {name} appeared" + (f" ({addrs})" if addrs else ""))
        )
    for name in sorted(before.keys() - after.keys()):
        out.append(HistoryChange(kind="removed", text=f"Interface {name} disappeared"))
    for name in sorted(before.keys() & after.keys()):
        out += _interface_changes(before[name], after[name])

    def key(r: Route) -> tuple[Any, ...]:
        return (r.destination, r.gateway, r.interface, r.family)

    defaults = {
        f: [r for r in rs if r.destination in ("0.0.0.0/0", "::/0")]
        for f, rs in (("old", old.routes), ("new", new.routes))
    }
    if {key(r) for r in defaults["old"]} != {key(r) for r in defaults["new"]}:
        was = ", ".join(sorted({r.gateway or r.interface or "?" for r in defaults["old"]})) or "none"
        now = ", ".join(sorted({r.gateway or r.interface or "?" for r in defaults["new"]})) or "none"
        out.append(HistoryChange(kind="changed", text=f"Default gateway {was} → {now}"))
    others = {
        f: {key(r): r for r in rs if r.destination not in ("0.0.0.0/0", "::/0") and r.protocol != "kernel"}
        for f, rs in (("old", old.routes), ("new", new.routes))
    }
    for k in sorted(others["new"].keys() - others["old"].keys(), key=str):
        out.append(HistoryChange(kind="added", text=f"Route {_route_text(others['new'][k])} added"))
    for k in sorted(others["old"].keys() - others["new"].keys(), key=str):
        out.append(HistoryChange(kind="removed", text=f"Route {_route_text(others['old'][k])} removed"))

    if old.dns.effective_servers != new.dns.effective_servers:
        was = ", ".join(old.dns.effective_servers) or "none"
        now = ", ".join(new.dns.effective_servers) or "none"
        out.append(HistoryChange(kind="changed", text=f"DNS servers {was} → {now}"))
    if old.dns.resolv_conf_search != new.dns.resolv_conf_search:
        was = ", ".join(old.dns.resolv_conf_search) or "none"
        now = ", ".join(new.dns.resolv_conf_search) or "none"
        out.append(HistoryChange(kind="changed", text=f"DNS search domains {was} → {now}"))
    return out
