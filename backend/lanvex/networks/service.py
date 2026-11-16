"""A server's networks: what Lanvex Core documents, what the server has, and the change in between.

A network on a server is documented in Lanvex Core as a virtual interface of the server's device with a
parent interface and one VLAN (802.1Q mode access, or a single tagged VLAN), and the server's IPv4 address
on it. Its DHCP pool is an IP range in that network tagged lanvex-dhcp (lease time in the custom field
dhcp_lease_time); a fixed address is an IP address in the network with the custom field mac_address.

Lanvex compares that with the server's last read and prepares the change that makes the server match.
Lanvex Core is never written here except when an administrator adds or removes a network or a fixed
address, and the server is never changed except through that approved change.
"""

import fnmatch
import ipaddress
from typing import Any

from pydantic import ValidationError

from lanvex.changes.schemas import (
    AccessRule,
    AccessSettings,
    AccessSpec,
    DhcpPool,
    DhcpSpec,
    NetworksRequest,
    Reservation,
    VlanSpec,
)
from lanvex.changes.service import NETWORK_STACKS, check_ssh_kept
from lanvex.core.errors import Conflict, LanvexError, NotFound, ValidationFailed
from lanvex.integrations.linux.facts import HostFacts
from lanvex.ipam.prefixes import GATEWAY_TAG_SLUG
from lanvex.netbox.client import NetBoxClient
from lanvex.networks.schemas import (
    FixedAddress,
    FixedIn,
    Lease,
    NetworksView,
    NewNetwork,
    OnServer,
    Pool,
    ServerNetwork,
    check_pool,
)
from lanvex.servers.models import Server
from lanvex.servers.service import ServerService

DHCP_TAG_SLUG = "lanvex-dhcp"
MAC_FIELD = "mac_address"
LEASE_FIELD = "dhcp_lease_time"
ACCESS_FIELD = "lanvex_access"
DEFAULT_LEASE = "12h"
INTERFACES, IPS, RANGES, PREFIXES, VLANS = (
    "dcim/interfaces/",
    "ipam/ip-addresses/",
    "ipam/ip-ranges/",
    "ipam/prefixes/",
    "ipam/vlans/",
)


def _host(address: str) -> str:
    return str(ipaddress.ip_interface(address).ip)


def _vid(iface: dict[str, Any]) -> tuple[int | None, str | None]:
    untagged = iface.get("untagged_vlan")
    tagged = iface.get("tagged_vlans") or []
    vlan = untagged or (tagged[0] if len(tagged) == 1 else None)
    return (vlan.get("vid"), vlan.get("name")) if vlan else (None, None)


def _is_vlan_interface(iface: dict[str, Any]) -> bool:
    kind = (iface.get("type") or {}).get("value")
    return kind == "virtual" and bool(iface.get("parent")) and _vid(iface)[0] is not None


def _access(raw: dict[str, Any]) -> AccessSettings:
    """Access rules as the helper applied them (it validated them)."""
    try:
        return AccessSettings.model_validate({k: v for k, v in raw.items() if k != "interface"})
    except ValidationError:
        return AccessSettings()


def _documented_access(iface: dict[str, Any]) -> tuple[AccessSettings | None, str | None]:
    """The network's access rules from the interface's custom field: (rules, problem)."""
    raw = (iface.get("custom_fields") or {}).get(ACCESS_FIELD)
    if raw in (None, "", {}):
        return AccessSettings(), None
    try:
        return AccessSettings.model_validate(raw), None
    except ValidationError as e:
        first = e.errors()[0]
        return None, f"The access rules documented for {iface['name']} cannot be used: {first['msg']}"


class NetworksService:
    def __init__(self, servers: ServerService, nb: NetBoxClient | None):
        self.servers = servers
        self.nb = nb

    # ---- reading -----------------------------------------------------------------------------------

    def _device(self, s: Server) -> int:
        if self.nb is None:
            raise ValidationFailed("Networks need a connection to Lanvex Core or NetBox.")
        if s.netbox_object_type != "dcim.device" or not s.netbox_object_id:
            raise ValidationFailed(
                f"Link {s.name} to its device in {self.nb.name} first: networks are documented as the device's interfaces."
            )
        return s.netbox_object_id

    async def view(self, s: Server) -> NetworksView:
        snap, facts = await self.servers.latest_facts(s.id)
        if self.nb is None or s.netbox_object_type != "dcim.device" or not s.netbox_object_id:
            return NetworksView(
                linked=False,
                supported=facts.stack.key in NETWORK_STACKS if facts else None,
                stack_label=facts.stack.primary if facts else None,
                read_at=facts.collected_at if facts else None,
                networks=[],
                in_sync=True,
                change=None,
                missing_fields=[],
                other_leases=[],
            )
        device = s.netbox_object_id
        interfaces = await self.nb.list_all(INTERFACES, {"device_id": device}, cap=5000)
        device_ips = await self.nb.list_all(IPS, {"device_id": device}, cap=5000)
        by_iface: dict[int, list[str]] = {}
        for ip in device_ips:
            obj = ip.get("assigned_object") or {}
            if ip.get("family", {}).get("value", 4) == 4 and ":" not in ip["address"] and obj.get("id"):
                by_iface.setdefault(obj["id"], []).append(ip["address"])
        documented = [i for i in interfaces if _is_vlan_interface(i)]
        nets = sorted(
            {
                str(ipaddress.ip_interface(a).network)
                for i in documented
                for a in by_iface.get(i["id"], [])[:1]
            }
        )
        ranges = await self._ranges(nets)
        fixed = await self._fixed(nets, {_host(a) for addrs in by_iface.values() for a in addrs})
        missing_fields = []
        sample = next((ip for ip in device_ips), None)
        if sample is not None and MAC_FIELD not in (sample.get("custom_fields") or {}):
            missing_fields.append(f"{MAC_FIELD} (on IP addresses)")
        if ranges and LEASE_FIELD not in (ranges[0].get("custom_fields") or {}):
            missing_fields.append(f"{LEASE_FIELD} (on IP ranges)")
        if documented and ACCESS_FIELD not in (documented[0].get("custom_fields") or {}):
            missing_fields.append(f"{ACCESS_FIELD} (on interfaces)")
        applied = {r["interface"]: _access(r) for r in (facts.lanvex_access if facts is not None else [])}

        ours = {}
        present = {}
        if facts is not None:
            present = {i.name: i for i in facts.interfaces}
            ours = {i.name: i for i in facts.interfaces if facts.lanvex_owned(i)}
        served = {p.interface: p for p in (facts.dhcp.pools if facts and facts.dhcp else [])}
        leases = facts.dhcp.leases if facts and facts.dhcp else []
        fixed_macs = {(f.ip, f.mac) for f in fixed}

        networks: list[ServerNetwork] = []
        vlans: list[VlanSpec] = []
        pools: list[DhcpPool] = []
        rules: list[AccessRule] = []
        claimed: set[str] = set()

        def keep_as_is(name: str) -> None:
            """A Lanvex VLAN that this change must leave exactly as the server has it (protected, or
            documented incompletely): the change lists the whole set, so leaving it out would remove it."""
            p = ours.get(name)
            if p is None:
                return
            current = sorted(a.address for a in p.addresses if a.family == 4 and not a.dynamic)
            if not current or p.parent is None or p.vlan_id is None:
                return
            vlans.append(VlanSpec(name=name, parent=p.parent, id=p.vlan_id, addresses=current))
            keep = applied.get(name)
            if keep is not None and not keep.is_default:
                rules.append(AccessRule(interface=name, **keep.model_dump()))
            if name in served:
                sp = served[name]
                pools.append(
                    DhcpPool(
                        interface=name,
                        start=sp.start,
                        end=sp.end,
                        lease=sp.lease or DEFAULT_LEASE,
                        router=sp.router,
                        dns=list(sp.dns) or None,
                        reservations=[
                            Reservation(mac=r.mac, ip=r.ip, name=r.name or "") for r in sp.reservations
                        ],
                    )
                )

        for i in sorted(documented, key=lambda x: x["name"]):
            name = i["name"]
            vid, vlan_name = _vid(i)
            addresses = sorted(a for a in by_iface.get(i["id"], []) if ":" not in a)
            network = str(ipaddress.ip_interface(addresses[0]).network) if addresses else None
            net = ipaddress.ip_network(network) if network else None
            pool = self._pool_in(ranges, net)
            mine = [f for f in fixed if net and ipaddress.ip_address(f.ip) in net]
            on_server = None
            if name in present:
                p = present[name]
                on_server = OnServer(
                    parent=p.parent,
                    id=p.vlan_id,
                    addresses=sorted(a.address for a in p.addresses if a.family == 4 and not a.dynamic),
                )
            status, note = self._status(s, name, i, vid, addresses, facts, ours, on_server)
            spec_pool = None
            access, problem = _documented_access(i)
            if status in ("protected", "incomplete"):
                keep_as_is(name)
            if status in ("in_sync", "missing", "differs"):
                vlans.append(VlanSpec(name=name, parent=i["parent"]["name"], id=vid, addresses=addresses))  # type: ignore[arg-type]
                # Unreadable documentation: the rules stay as the server has them until it is fixed.
                wanted = access if access is not None else applied.get(name)
                if wanted is not None and not wanted.is_default:
                    rules.append(AccessRule(interface=name, **wanted.model_dump()))
                if pool is not None:
                    own = _host(addresses[0])
                    spec_pool = DhcpPool(
                        interface=name,
                        start=pool.start,
                        end=pool.end,
                        lease=pool.lease,
                        router=own,
                        dns=[own],
                        reservations=[Reservation(mac=f.mac, ip=f.ip, name=f.name or "") for f in mine],
                    )
                    pools.append(spec_pool)
            here = [lz for lz in leases if net and ipaddress.ip_address(lz.ip) in net]
            claimed |= {lz.ip for lz in here}
            networks.append(
                ServerNetwork(
                    name=name,
                    parent=(i.get("parent") or {}).get("name"),
                    vlan_id=vid,
                    vlan_name=vlan_name,
                    addresses=addresses,
                    network=network,
                    netbox_interface_id=i["id"],
                    status=status,
                    note=note,
                    on_server=on_server,
                    pool=pool,
                    fixed=mine,
                    dhcp=(
                        self._dhcp_status(spec_pool, served.get(name), facts)
                        if status in ("in_sync", "missing", "differs")
                        else None
                    ),
                    leases=[self._lease(lz, fixed_macs) for lz in here],
                    access=access,
                    access_on_server=(applied.get(name) or AccessSettings()) if name in ours else None,
                    access_status=(
                        "invalid"
                        if problem
                        else (
                            ("in_sync" if (applied.get(name) or AccessSettings()) == access else "differs")
                            if status in ("in_sync", "differs")
                            else None
                        )
                    ),
                    access_problem=problem,
                )
            )
        documented_names = {i["name"] for i in documented}
        for name, p in sorted(ours.items()):
            if name in documented_names:
                continue
            guard = next((g for g in s.protected_interfaces or [] if fnmatch.fnmatchcase(name, g)), None)
            if guard is not None:
                keep_as_is(name)
            networks.append(
                ServerNetwork(
                    name=name,
                    parent=p.parent,
                    vlan_id=p.vlan_id,
                    addresses=[],
                    network=None,
                    netbox_interface_id=None,
                    status="protected" if guard else "removed",
                    note=(
                        f"Protected on {s.name} ({guard}). Lanvex leaves it alone."
                        if guard
                        else f"Lanvex created {name} on the server, and Lanvex Core no longer documents it."
                    ),
                    on_server=OnServer(
                        parent=p.parent,
                        id=p.vlan_id,
                        addresses=sorted(a.address for a in p.addresses if a.family == 4 and not a.dynamic),
                    ),
                    pool=None,
                    fixed=[],
                    dhcp=None
                    if guard
                    else ("removed" if name in served else ("none" if facts and facts.dhcp else None)),
                    leases=[],
                )
            )
        supported = facts.stack.key in NETWORK_STACKS if facts else None
        in_sync = (
            all(n.status not in ("missing", "differs", "removed") for n in networks)
            and all(n.dhcp in (None, "in_sync", "none") for n in networks)
            and all(n.access_status in (None, "in_sync", "invalid") for n in networks)
        )
        # Lanvex's dnsmasq file serving an interface no longer in the plan also needs a change.
        if (
            facts
            and facts.dhcp
            and set(served)
            - {p.interface for p in pools}
            - {n.name for n in networks if n.status == "removed"}
        ):
            in_sync = False
        change = None
        if facts is not None and supported and not in_sync:
            change = NetworksRequest(
                kind="networks", vlans=vlans, dhcp=DhcpSpec(pools=pools), access=AccessSpec(rules=rules)
            )
        return NetworksView(
            linked=True,
            supported=supported,
            stack_label=facts.stack.primary if facts else None,
            read_at=facts.collected_at if facts else None,
            networks=networks,
            in_sync=in_sync,
            change=change,
            missing_fields=missing_fields,
            other_leases=[self._lease(lz, fixed_macs) for lz in leases if lz.ip not in claimed],
        )

    def _status(
        self,
        s: Server,
        name: str,
        iface: dict[str, Any],
        vid: int | None,
        addresses: list[str],
        facts: HostFacts | None,
        ours: dict[str, Any],
        on_server: OnServer | None,
    ) -> tuple[str, str | None]:
        if not addresses:
            return "incomplete", f"Lanvex Core documents no IPv4 address on {name}."
        guard = next((p for p in s.protected_interfaces or [] if fnmatch.fnmatchcase(name, p)), None)
        if guard is not None:
            return (
                "protected",
                f"Protected on {s.name} ({guard}): another tool manages it. Lanvex leaves it alone.",
            )
        if facts is None:
            return "unknown", None
        if on_server is not None and name not in ours:
            return (
                "foreign",
                f"{name} exists on {s.name} and was not created by Lanvex. Lanvex leaves it alone.",
            )
        if on_server is None:
            return "missing", None
        want = ((iface.get("parent") or {}).get("name"), vid, sorted(addresses))
        have = (on_server.parent, on_server.id, sorted(on_server.addresses))
        return ("in_sync", None) if want == have else ("differs", None)

    def _dhcp_status(self, want: DhcpPool | None, have: Any, facts: HostFacts | None) -> str | None:
        if facts is None or facts.dhcp is None:
            return None
        if want is None:
            return "removed" if have is not None else "none"
        if have is None:
            return "missing"
        same = (have.start, have.end, have.lease, have.router, list(have.dns)) == (
            want.start,
            want.end,
            want.lease,
            want.router,
            want.dns,
        ) and {(r.mac, r.ip, r.name or "") for r in have.reservations} == {
            (r.mac, r.ip, r.name) for r in want.reservations
        }
        return "in_sync" if same else "differs"

    @staticmethod
    def _lease(lz: Any, fixed: set[tuple[str, str]]) -> Lease:
        return Lease(
            ip=lz.ip,
            mac=lz.mac,
            hostname=lz.hostname,
            expires_at=lz.expires_at,
            fixed=(lz.ip, lz.mac) in fixed,
        )

    async def _ranges(self, nets: list[str]) -> list[dict[str, Any]]:
        if not nets:
            return []
        assert self.nb is not None
        try:
            return await self.nb.list_all(RANGES, {"parent": nets, "tag": DHCP_TAG_SLUG}, cap=1000)
        except ValidationFailed:  # the tag does not exist yet: no pools
            return []

    @staticmethod
    def _pool_in(ranges: list[dict[str, Any]], net: Any) -> Pool | None:
        if net is None:
            return None
        for r in sorted(ranges, key=lambda x: x["id"]):
            start, end = _host(r["start_address"]), _host(r["end_address"])
            if ipaddress.ip_address(start) in net and ipaddress.ip_address(end) in net:
                lease = (r.get("custom_fields") or {}).get(LEASE_FIELD) or DEFAULT_LEASE
                return Pool(start=start, end=end, lease=lease, ip_range_id=r["id"])
        return None

    async def _fixed(self, nets: list[str], own: set[str]) -> list[FixedAddress]:
        if not nets:
            return []
        assert self.nb is not None
        out = []
        for ip in await self.nb.list_all(IPS, {"parent": nets}, cap=5000):
            mac = ((ip.get("custom_fields") or {}).get(MAC_FIELD) or "").lower()
            host = _host(ip["address"])
            if mac and host not in own:
                out.append(FixedAddress(ip=host, mac=mac, name=ip.get("dns_name") or None, ip_id=ip["id"]))
        return sorted(out, key=lambda f: ipaddress.ip_address(f.ip))

    # ---- documenting --------------------------------------------------------------------------------

    async def add(self, s: Server, body: NewNetwork) -> list[str]:
        """Document a new network on the server in Lanvex Core. Returns what was created, in order."""
        device_id = self._device(s)
        assert self.nb is not None
        device = await self.nb.get(f"dcim/devices/{device_id}/")
        site_id = (device.get("site") or {}).get("id")
        interfaces = {
            i["name"]: i for i in await self.nb.list_all(INTERFACES, {"device_id": device_id}, cap=5000)
        }
        if body.name in interfaces:
            raise ValidationFailed(
                f"{device['name']} already has an interface {body.name} in {self.nb.name}.",
                fields={"name": ["Taken."]},
            )
        parent = interfaces.get(body.parent)
        if parent is None:
            raise ValidationFailed(
                f"{self.nb.name} does not document {body.parent} on {device['name']}. Record the server's interfaces first "
                "(Documented vs. on the server).",
                fields={"parent": ["Not documented."]},
            )
        guard = next((p for p in s.protected_interfaces or [] if fnmatch.fnmatchcase(body.name, p)), None)
        if guard is not None:
            raise ValidationFailed(
                f"{body.name} is protected on {s.name} ({guard}). Choose another name.",
                fields={"name": ["Protected."]},
            )
        iface = ipaddress.ip_interface(body.address)
        clash = await self.nb.list_all(IPS, {"address": str(iface.ip)}, cap=10)
        if clash:
            raise ValidationFailed(
                f"{iface.ip} is already documented in {self.nb.name}.",
                fields={"address": ["Already documented."]},
            )
        created: list[str] = []
        try:
            vlan = await self._vlan(body, site_id, created)
            await self._prefix(str(iface.network), vlan["id"], site_id, created)
            new_iface = await self.nb.post(
                INTERFACES,
                {
                    "device": device_id,
                    "name": body.name,
                    "type": "virtual",
                    "parent": parent["id"],
                    "mode": "access",
                    "untagged_vlan": vlan["id"],
                    "description": "Network added in Lanvex",
                },
            )
            created.append(f"interface {body.name}")
            gateway_tag = await self._tag(
                GATEWAY_TAG_SLUG,
                "Lanvex gateway",
                "Marks the gateway address of a prefix (managed by Lanvex).",
            )
            await self.nb.post(
                IPS,
                {
                    "address": body.address,
                    "assigned_object_type": "dcim.interface",
                    "assigned_object_id": new_iface["id"],
                    "status": "active",
                    "tags": [gateway_tag],
                    "description": f"{s.name} on {body.name}",
                },
            )
            created.append(f"address {body.address}")
            if body.pool_start and body.pool_end:
                await self._create_pool(body.address, body.pool_start, body.pool_end, body.lease, created)
        except LanvexError as e:
            if not created:
                raise
            raise Conflict(
                f"{self.nb.name} refused part of the new network: {e.message} Already created: {', '.join(created)}."
            ) from e
        return created

    async def _vlan(self, body: NewNetwork, site_id: int | None, created: list[str]) -> dict[str, Any]:
        assert self.nb is not None
        found = await self.nb.list_all(
            VLANS, {"vid": body.vlan_id, "site_id": site_id} if site_id else {"vid": body.vlan_id}, cap=10
        )
        if not found and site_id:
            found = await self.nb.list_all(
                VLANS, {"vid": body.vlan_id, "site_id": "null", "group_id": "null"}, cap=10
            )
        if found:
            return found[0]
        vlan = await self.nb.post(
            VLANS,
            {
                "vid": body.vlan_id,
                "name": body.vlan_name or body.name,
                "status": "active",
                **({"site": site_id} if site_id else {}),
            },
        )
        created.append(f"VLAN {body.vlan_id}")
        return vlan

    async def _prefix(self, network: str, vlan_id: int, site_id: int | None, created: list[str]) -> None:
        assert self.nb is not None
        found = await self.nb.list_all(PREFIXES, {"prefix": network, "vrf_id": "null"}, cap=10)
        if found:
            if not found[0].get("vlan"):
                await self.nb.patch(f"{PREFIXES}{found[0]['id']}/", {"vlan": vlan_id})
            return
        await self.nb.post(
            PREFIXES,
            {
                "prefix": network,
                "status": "active",
                "vlan": vlan_id,
                **({"scope_type": "dcim.site", "scope_id": site_id} if site_id else {}),
            },
        )
        created.append(f"prefix {network}")

    async def _tag(self, slug: str, name: str, description: str) -> int:
        assert self.nb is not None
        page = await self.nb.page("extras/tags/", {"slug": slug}, limit=1)
        if page.results:
            return int(page.results[0]["id"])
        raw = await self.nb.post(
            "extras/tags/", {"name": name, "slug": slug, "color": "16a34a", "description": description}
        )
        return int(raw["id"])

    async def _create_pool(self, address: str, start: str, end: str, lease: str, created: list[str]) -> None:
        assert self.nb is not None
        start, end = check_pool(address, start, end)
        plen = ipaddress.ip_interface(address).network.prefixlen
        tag = await self._tag(DHCP_TAG_SLUG, "Lanvex DHCP", "An IP range Lanvex serves by DHCP.")
        body: dict[str, Any] = {
            "start_address": f"{start}/{plen}",
            "end_address": f"{end}/{plen}",
            "status": "active",
            "tags": [tag],
            "description": "DHCP pool (Lanvex)",
        }
        if lease != DEFAULT_LEASE:
            body["custom_fields"] = {LEASE_FIELD: lease}
        await self.nb.post(RANGES, body)
        created.append(f"DHCP pool {start} to {end}")

    async def _network(self, s: Server, name: str) -> ServerNetwork:
        view = await self.view(s)
        n = next((x for x in view.networks if x.name == name and x.netbox_interface_id), None)
        if n is None:
            raise NotFound(f"Lanvex Core documents no network {name} on {s.name}.")
        return n

    async def set_access(self, s: Server, name: str, body: AccessSettings) -> None:
        """What devices on the network may reach, in the interface's custom field (null: the default)."""
        self._device(s)
        assert self.nb is not None
        view = await self.view(s)
        if any(f.startswith(ACCESS_FIELD) for f in view.missing_fields):
            raise ValidationFailed(
                f"{self.nb.name} has no custom field {ACCESS_FIELD} on interfaces yet. Lanvex Core 0.9 creates it; "
                "with your own NetBox, see docs/networks.md."
            )
        n = await self._network(s, name)
        if not n.network:
            raise ValidationFailed(f"Document an IPv4 address on {name} first.")
        own = ipaddress.ip_network(n.network)
        for d in body.destinations:
            if ipaddress.ip_network(d.network).overlaps(own):
                raise ValidationFailed(
                    f"{d.network} is {name}'s own network.",
                    fields={"destinations": [f"{d.network} is this network."]},
                )
        await check_ssh_kept(s, {name: body}, {name: n.addresses})
        await self.nb.patch(
            f"{INTERFACES}{n.netbox_interface_id}/",
            {"custom_fields": {ACCESS_FIELD: None if body.is_default else body.model_dump(mode="json")}},
        )

    async def set_pool(self, s: Server, name: str, start: str | None, end: str | None, lease: str) -> None:
        """Create, change or remove a network's DHCP pool (None for both removes it)."""
        self._device(s)
        assert self.nb is not None
        n = await self._network(s, name)
        if not n.addresses:
            raise ValidationFailed(f"Document an IPv4 address on {name} first.")
        if start is None or end is None:
            if n.pool and n.pool.ip_range_id:
                await self.nb.delete(f"{RANGES}{n.pool.ip_range_id}/")
            return
        try:
            start, end = check_pool(n.addresses[0], start, end)
        except ValueError as e:
            raise ValidationFailed(str(e), fields={"start": [str(e)]}) from e
        if n.pool and n.pool.ip_range_id:
            plen = ipaddress.ip_interface(n.addresses[0]).network.prefixlen
            await self.nb.patch(
                f"{RANGES}{n.pool.ip_range_id}/",
                {
                    "start_address": f"{start}/{plen}",
                    "end_address": f"{end}/{plen}",
                    "custom_fields": {LEASE_FIELD: None if lease == DEFAULT_LEASE else lease},
                },
            )
        else:
            await self._create_pool(n.addresses[0], start, end, lease, [])

    async def set_fixed(self, s: Server, name: str, body: FixedIn) -> None:
        """A fixed address for one device: an IP address in the network with its MAC address."""
        self._device(s)
        assert self.nb is not None
        n = await self._network(s, name)
        if not n.network:
            raise ValidationFailed(f"Document an IPv4 address on {name} first.")
        net = ipaddress.ip_network(n.network)
        ip = ipaddress.ip_address(body.ip)
        if ip not in net or ip in (net.network_address, net.broadcast_address):
            raise ValidationFailed(f"{ip} is not an address in {net}.", fields={"ip": [f"Not in {net}."]})
        if any(_host(a) == body.ip for a in n.addresses):
            raise ValidationFailed(
                f"{ip} is the server's own address.", fields={"ip": ["The server's own address."]}
            )
        other = next((f for f in n.fixed if f.mac == body.mac and f.ip != body.ip), None)
        if other is not None:
            raise ValidationFailed(
                f"{body.mac} already has the fixed address {other.ip}.", fields={"mac": [f"Has {other.ip}."]}
            )
        existing = await self.nb.list_all(IPS, {"address": body.ip, "parent": n.network}, cap=5)
        data: dict[str, Any] = {
            "status": "dhcp",
            "dns_name": body.name or "",
            "custom_fields": {MAC_FIELD: body.mac},
        }
        if existing:
            assigned = existing[0].get("assigned_object")
            if assigned:
                raise ValidationFailed(
                    f"{ip} is assigned to {assigned.get('name')} in {self.nb.name}; a fixed DHCP address must be free.",
                    fields={"ip": ["Assigned to an interface."]},
                )
            await self.nb.patch(f"{IPS}{existing[0]['id']}/", data)
        else:
            await self.nb.post(
                IPS,
                {"address": f"{ip}/{net.prefixlen}", "description": "Fixed DHCP address (Lanvex)", **data},
            )

    async def remove_fixed(self, s: Server, name: str, ip: str) -> None:
        """The address stays documented; it just no longer belongs to one device's MAC address."""
        self._device(s)
        assert self.nb is not None
        n = await self._network(s, name)
        f = next((x for x in n.fixed if x.ip == ip), None)
        if f is None or f.ip_id is None:
            raise NotFound(f"{ip} is not a fixed address on {name}.")
        await self.nb.patch(f"{IPS}{f.ip_id}/", {"custom_fields": {MAC_FIELD: None}})

    async def remove(self, s: Server, name: str) -> list[str]:
        """Remove a network's documentation from the server: its interface, address and DHCP pool. The
        VLAN and the prefix stay; other servers or switches may use them."""
        self._device(s)
        assert self.nb is not None
        n = await self._network(s, name)
        removed = []
        if n.pool and n.pool.ip_range_id:
            await self.nb.delete(f"{RANGES}{n.pool.ip_range_id}/")
            removed.append(f"DHCP pool {n.pool.start} to {n.pool.end}")
        for ip in await self.nb.list_all(IPS, {"interface_id": n.netbox_interface_id}, cap=50):
            await self.nb.delete(f"{IPS}{ip['id']}/")
            removed.append(f"address {ip['address']}")
        await self.nb.delete(f"{INTERFACES}{n.netbox_interface_id}/")
        removed.append(f"interface {name}")
        return removed
