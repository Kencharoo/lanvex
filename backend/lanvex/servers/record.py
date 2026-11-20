"""Record what a server really has in NetBox (Lanvex Core).

The comparison shows where the documentation and the server differ. For the rows an administrator
picks, this module plans the NetBox writes that make the documentation describe the server as it was
last read, shows them as a preview, and applies exactly the previewed plan. Nothing on the server
changes: this updates documentation, never configuration.

Planned per comparison result:
- only on the server: create the address on the server's interface (status "dhcp" when the server got
  it from DHCP), or assign an existing unassigned record; add the interface first when NetBox lacks it.
- only in NetBox: unassign the record (it stays in NetBox, so nothing is lost).
- prefix length differs: correct the record's prefix length.
- interface differs: move the record to the server's interface.

And for the network settings picked (servers/settings_drift.py):
- default gateway: make the server's gateway the gateway of the documented prefix it is in.
- DNS servers, search domains, static routes: write what the server has into the device's own local
  config context data, key "lanvex" (the rest of it is kept). Local context data overrides config
  contexts, so this records the server's own settings also where a site-wide config context documents
  others.
"""

import hashlib
import ipaddress
import json
from typing import Any, Literal

from pydantic import Field, model_validator

from lanvex.core.errors import Conflict, LanvexError, ValidationFailed
from lanvex.integrations.linux.facts import HostFacts
from lanvex.netbox.client import NetBoxClient
from lanvex.schemas import ApiModel
from lanvex.servers import settings_drift
from lanvex.servers.models import Server
from lanvex.servers.schemas import ComparisonRow, NetBoxRef, SettingRow

ActionKind = Literal[
    "create_interface", "create", "assign", "update_mask", "move", "unassign", "gateway", "settings"
]
SETTING_NAMES = {"dns_servers": "DNS servers", "dns_search": "search domains"}

DEVICE_INTERFACE_TYPES = {"bridge": ("bridge", "Bridge"), "bond": ("lag", "Link aggregation group")}


class RecordAction(ApiModel):
    address: str | None  # the comparison row (host address) this belongs to; None for interfaces
    setting: str | None = None  # or the setting row's id
    kind: ActionKind
    text: str
    problem: str | None = None  # set when this cannot be done; the rest of the plan still can


class RecordRequest(ApiModel):
    addresses: list[str] = Field(default_factory=list, max_length=200)
    settings: list[str] = Field(default_factory=list, max_length=200)  # setting rows' ids
    token: str | None = None  # the preview's token; without it nothing is written

    @model_validator(mode="after")
    def _something(self) -> "RecordRequest":
        if not self.addresses and not self.settings:
            raise ValueError("Pick at least one difference to record.")
        return self


class RecordPlan(ApiModel):
    target: NetBoxRef
    actions: list[RecordAction]
    token: str
    applied: bool = False
    done: list[RecordAction] = Field(default_factory=list)
    failed: RecordAction | None = None
    error: str | None = None


class _Step:
    def __init__(self, action: RecordAction, method: str, path: str, body: dict[str, Any] | None) -> None:
        self.action, self.method, self.path, self.body = action, method, path, body


class Recorder:
    def __init__(self, nb: NetBoxClient, server: Server, target: NetBoxRef, facts: HostFacts) -> None:
        self.nb, self.s, self.target, self.facts = nb, server, target, facts
        self.vm = server.netbox_object_type == "virtualization.virtualmachine"
        self.iface_path = "virtualization/interfaces/" if self.vm else "dcim/interfaces/"
        self.iface_type = "virtualization.vminterface" if self.vm else "dcim.interface"
        self.where = target.name or f"#{target.object_id}"

    async def plan(
        self,
        rows: list[ComparisonRow],
        addresses: list[str],
        setting_rows: list[SettingRow] | None = None,
        settings: list[str] | None = None,
    ) -> tuple[RecordPlan, list[_Step]]:
        by_address = {r.address: r for r in rows}
        key = "virtual_machine_id" if self.vm else "device_id"
        interfaces = (
            {
                i["name"]: i["id"]
                for i in await self.nb.list_all(self.iface_path, {key: self.s.netbox_object_id}, cap=5000)
            }
            if addresses
            else {}
        )
        parent: dict[str, Any] | None = None
        steps: list[_Step] = []
        new_interfaces: set[str] = set()

        async def interface_ref(name: str) -> int | str:
            """The NetBox interface id, or a placeholder filled in when the interface is created."""
            if name in interfaces:
                return interfaces[name]
            if name not in new_interfaces:
                new_interfaces.add(name)
                body: dict[str, Any] = {"name": name}
                if self.vm:
                    body["virtual_machine"] = self.s.netbox_object_id
                    label = "VM interface"
                else:
                    kind = next((i.kind for i in self.facts.interfaces if i.name == name), "")
                    value, type_label = DEVICE_INTERFACE_TYPES.get(
                        kind, ("other", "Other") if kind == "ethernet" else ("virtual", "Virtual")
                    )
                    body |= {"device": self.s.netbox_object_id, "type": value}
                    label = f"type {type_label}"
                steps.append(
                    _Step(
                        RecordAction(
                            address=None,
                            kind="create_interface",
                            text=f"Add interface {name} to {self.where} ({label})",
                        ),
                        "post",
                        self.iface_path,
                        body,
                    )
                )
            return f"interface:{name}"

        def assign_to(ref: int | str) -> dict[str, Any]:
            return {"assigned_object_type": self.iface_type, "assigned_object_id": ref}

        for host in addresses:
            r = by_address.get(host)
            if r is None:
                steps.append(
                    self._blocked(
                        host,
                        "create",
                        f"Record {host}",
                        "It no longer differs. The comparison changed; review it again.",
                    )
                )
                continue
            if r.status == "match":
                continue
            if r.status == "only_on_server":
                assert r.server_address and r.server_interface
                dynamic = any(
                    a.address == r.server_address and a.dynamic
                    for i in self.facts.interfaces
                    if i.name == r.server_interface
                    for a in i.addresses
                )
                existing = await self.nb.list_all("ipam/ip-addresses/", {"address": host}, cap=10)
                if len(existing) > 1:
                    steps.append(
                        self._blocked(
                            host,
                            "create",
                            f"Record {r.server_address}",
                            f"{host} is recorded {len(existing)} times in {self.nb.name}. Resolve that there first.",
                        )
                    )
                    continue
                if existing and existing[0].get("assigned_object"):
                    obj = existing[0]["assigned_object"]
                    owner = (obj.get("device") or obj.get("virtual_machine") or {}).get("name")
                    steps.append(
                        self._blocked(
                            host,
                            "assign",
                            f"Assign {existing[0]['address']} to {self.where} / {r.server_interface}",
                            f"It is recorded on {owner} / {obj.get('name')}. If it belongs to this server, move it in {self.nb.name}.",
                        )
                    )
                    continue
                ref = await interface_ref(r.server_interface)
                if existing:
                    ip = existing[0]
                    body = assign_to(ref)
                    text = (
                        f"Assign the existing record {ip['address']} to {self.where} / {r.server_interface}"
                    )
                    if ip["address"] != r.server_address:
                        body["address"] = r.server_address
                        text += f", as {r.server_address}"
                    steps.append(
                        _Step(
                            RecordAction(address=host, kind="assign", text=text),
                            "patch",
                            f"ipam/ip-addresses/{ip['id']}/",
                            body,
                        )
                    )
                else:
                    status = "dhcp" if dynamic else "active"
                    steps.append(
                        _Step(
                            RecordAction(
                                address=host,
                                kind="create",
                                text=f"Record {r.server_address} on {self.where} / {r.server_interface}"
                                + (" with status DHCP" if dynamic else ""),
                            ),
                            "post",
                            "ipam/ip-addresses/",
                            {"address": r.server_address, "status": status, **assign_to(ref)},
                        )
                    )
            elif r.status == "only_in_netbox":
                text = f"Unassign {r.netbox_address} from {self.where} / {r.netbox_interface} (the record stays in {self.nb.name})"
                if parent is None:
                    path = "virtualization/virtual-machines/" if self.vm else "dcim/devices/"
                    parent = await self.nb.get(f"{path}{self.s.netbox_object_id}/")
                role = next(
                    (
                        k.replace("_", " ")
                        .replace("ip4", "IPv4")
                        .replace("ip6", "IPv6")
                        .replace("oob ip", "out-of-band IP")
                        for k in ("primary_ip4", "primary_ip6", "oob_ip")
                        if (parent.get(k) or {}).get("id") == r.netbox_ip_id
                    ),
                    None,
                )
                if role:
                    steps.append(
                        self._blocked(
                            host,
                            "unassign",
                            text,
                            f"It is the {role} of {self.where}. Choose another one in {self.nb.name} first.",
                        )
                    )
                    continue
                steps.append(
                    _Step(
                        RecordAction(address=host, kind="unassign", text=text),
                        "patch",
                        f"ipam/ip-addresses/{r.netbox_ip_id}/",
                        {"assigned_object_type": None, "assigned_object_id": None},
                    )
                )
            elif r.status == "mask_differs":
                steps.append(
                    _Step(
                        RecordAction(
                            address=host,
                            kind="update_mask",
                            text=f"Change the record {r.netbox_address} to {r.server_address}",
                        ),
                        "patch",
                        f"ipam/ip-addresses/{r.netbox_ip_id}/",
                        {"address": r.server_address},
                    )
                )
            elif r.status == "interface_differs":
                assert r.server_interface
                ref = await interface_ref(r.server_interface)
                steps.append(
                    _Step(
                        RecordAction(
                            address=host,
                            kind="move",
                            text=f"Move the record {r.netbox_address} from {r.netbox_interface} to {r.server_interface}",
                        ),
                        "patch",
                        f"ipam/ip-addresses/{r.netbox_ip_id}/",
                        assign_to(ref),
                    )
                )

        if settings:
            steps += await self._plan_settings(setting_rows or [], settings)

        # Interfaces first, then addresses and settings in the order picked.
        steps.sort(key=lambda st: st.action.kind != "create_interface")
        actions = [st.action for st in steps]
        return RecordPlan(
            target=self.target, actions=actions, token=_token(self.target, actions, steps)
        ), steps

    def _blocked(self, host: str, kind: ActionKind, text: str, problem: str) -> _Step:
        return _Step(RecordAction(address=host, kind=kind, text=text, problem=problem), "", "", None)

    async def _plan_settings(self, rows: list[SettingRow], picked: list[str]) -> list[_Step]:
        from lanvex.ipam import addressing as addr
        from lanvex.ipam.prefixes import PREFIXES, PrefixService, to_prefix

        by_id = {r.id: r for r in rows}
        steps: list[_Step] = []
        path = (
            f"{'virtualization/virtual-machines/' if self.vm else 'dcim/devices/'}{self.s.netbox_object_id}/"
        )
        obj: dict[str, Any] | None = None
        local: dict[str, Any] = {}
        lanvex: dict[str, Any] = {}
        routes: list[dict[str, str]] | None = None

        def blocked(sid: str, kind: ActionKind, text: str, problem: str) -> _Step:
            return _Step(
                RecordAction(address=None, setting=sid, kind=kind, text=text, problem=problem), "", "", None
            )

        for sid in dict.fromkeys(picked):
            r = by_id.get(sid)
            if r is None:
                steps.append(
                    blocked(
                        sid, "settings", f"Record {sid}", "It no longer differs. Review the comparison again."
                    )
                )
                continue
            if r.status == "match":
                continue
            action = RecordAction(address=None, setting=sid, kind="settings", text="")
            if r.kind == "gateway":
                assert r.prefix and r.prefix_id is not None and r.server
                action.kind = "gateway"
                ps = PrefixService(self.nb)
                p = to_prefix(await self.nb.get(f"{PREFIXES}{r.prefix_id}/"))
                gw = f"{r.server}/{addr.parse_network(p.prefix).prefixlen}"
                old = await ps.find_gateway(p)
                action.text = (
                    f"Make {r.server} the gateway of {p.prefix} (instead of {r.documented})"
                    if r.documented
                    else f"Record {r.server} as the gateway of {p.prefix}"
                )
                problem = addr.address_in_prefix(
                    addr.parse_interface(gw), addr.parse_network(p.prefix), p.is_pool
                )
                if problem:
                    action.problem = problem
                    steps.append(_Step(action, "", "", None))
                    continue
                body = {"prefix_id": p.id, "gateway": gw, "clear": old.ip_address_id if old else None}
                steps.append(_Step(action, "gateway", f"ipam/prefixes/{p.id}/", body))
                continue

            if obj is None:
                obj = await self.nb.get(path)
                raw = obj.get("local_context_data")
                local = dict(raw) if isinstance(raw, dict) else {}
                own = local.get(settings_drift.KEY)
                lanvex = dict(own) if isinstance(own, dict) else {}
                routes = settings_drift.documented_routes(
                    settings_drift.documented(obj.get("config_context"))
                )
            if r.kind in SETTING_NAMES:
                values = [v.strip() for v in (r.server or "").split(",") if v.strip()]
                lanvex[r.kind] = values
                name = SETTING_NAMES[r.kind]
                was = f" (instead of {r.documented})" if r.documented else ""
                action.text = (
                    f"Record {name} {r.server} for {self.where}{was}"
                    if values
                    else f"Record that {self.where} has no {name}{was}"
                )
            else:  # a static route
                current = [x for x in (routes or []) if x["to"] != r.key]
                if r.server:
                    current.append({"to": r.key, "via": r.server})
                    current.sort(key=lambda x: settings_drift._sort_key(x["to"]))
                    action.text = f"Record the route to {r.key} via {r.server} for {self.where}" + (
                        f" (instead of via {r.documented})" if r.documented else ""
                    )
                else:
                    action.text = (
                        f"Remove the route to {r.key} via {r.documented} from {self.where}'s documentation"
                    )
                routes = current
                lanvex["routes"] = current
            # Each write carries everything recorded so far, so the documentation is whole after each.
            body = {"local_context_data": {**local, settings_drift.KEY: dict(lanvex)}}
            steps.append(_Step(action, "patch", path, body))
        return steps

    async def apply(self, plan: RecordPlan, steps: list[_Step]) -> RecordPlan:
        created: dict[str, int] = {}
        plan.applied = True
        for st in steps:
            if st.action.problem:
                continue
            body = dict(st.body or {})
            ref = body.get("assigned_object_id")
            if isinstance(ref, str):
                body["assigned_object_id"] = created[ref.removeprefix("interface:")]
            try:
                if st.method == "gateway":
                    await self._gateway(body)
                elif st.method == "post":
                    obj = await self.nb.post(st.path, body)
                    if st.action.kind == "create_interface":
                        created[body["name"]] = obj["id"]
                else:
                    await self.nb.patch(st.path, body)
            except LanvexError as e:
                plan.failed, plan.error = st.action, e.message
                break
            plan.done.append(st.action)
        return plan

    async def _gateway(self, body: dict[str, Any]) -> None:
        from lanvex.ipam.prefixes import PREFIXES, PrefixService, to_prefix

        ps = PrefixService(self.nb)
        await ps.set_gateway(
            to_prefix(await self.nb.get(f"{PREFIXES}{body['prefix_id']}/")), str(body["gateway"])
        )
        if body.get("clear"):  # after: if setting the new one fails, the old one is still documented
            await ps.clear_gateway(int(body["clear"]))


def _token(target: NetBoxRef, actions: list[RecordAction], steps: list[_Step]) -> str:
    payload = {
        "target": [target.object_type, target.object_id],
        "steps": [
            [a.model_dump(mode="json"), st.method, st.path, st.body]
            for a, st in zip(actions, steps, strict=True)
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:32]


def check_addresses(addresses: list[str]) -> list[str]:
    out: list[str] = []
    for a in addresses:
        try:
            out.append(str(ipaddress.ip_address(a)))
        except ValueError as e:
            raise ValidationFailed(
                f"{a} is not an IP address.", fields={"addresses": [f"{a} is not an IP address."]}
            ) from e
    return list(dict.fromkeys(out))


def stale(netbox_name: str) -> Conflict:
    return Conflict(
        f"The server or {netbox_name} changed since the preview, so the plan is different now. "
        "Review the new preview."
    )
