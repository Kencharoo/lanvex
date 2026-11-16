import asyncio
import ipaddress
from typing import Any

from lanvex.core.errors import NotFound, ValidationFailed
from lanvex.ipam import addressing as addr
from lanvex.ipam.schemas import (
    AvailableIP,
    AvailablePrefix,
    Finding,
    Gateway,
    Prefix,
    PrefixCreate,
    PrefixStatus,
    PrefixUpdate,
    PrefixValidateRequest,
    PrefixValidation,
    RelatedPrefix,
    UtilizationOut,
    VlanRef,
)
from lanvex.netbox.client import NetBoxClient
from lanvex.schemas import Page, choice, meta, ref, tags

PREFIXES = "ipam/prefixes/"
IPS = "ipam/ip-addresses/"
GATEWAY_TAG_SLUG = "lanvex-gateway"
NULL = "null"  # NetBox FILTERS_NULL_CHOICE_VALUE: matches objects with no VRF (the global table)
UTILIZATION_CONCURRENCY = 8
CHILD_PREFIX_CAP = 5000


def vrf_filter(vrf_id: int | None) -> str | int:
    return vrf_id if vrf_id is not None else NULL


def to_prefix(raw: dict[str, Any]) -> Prefix:
    vlan = raw.get("vlan")
    return Prefix(
        id=raw["id"],
        prefix=raw["prefix"],
        family=int((raw.get("family") or {}).get("value") or ipaddress.ip_network(raw["prefix"]).version),
        name=raw.get("description") or "",
        status=choice(raw.get("status")) or choice("active"),  # type: ignore[arg-type]
        vrf=ref(raw.get("vrf")),
        vlan=VlanRef(id=vlan["id"], vid=vlan["vid"], name=vlan.get("name") or "") if vlan else None,
        role=ref(raw.get("role")),
        tenant=ref(raw.get("tenant")),
        scope_type=raw.get("scope_type"),
        scope=ref(raw.get("scope")),
        is_pool=bool(raw.get("is_pool")),
        mark_utilized=bool(raw.get("mark_utilized")),
        depth=int(raw.get("_depth") or 0),
        children=int(raw.get("children") or 0),
        comments=raw.get("comments") or "",
        tags=tags(raw.get("tags")),
        meta=meta(raw),
    )


def _range_in(r: dict[str, Any], net: addr.IPNetwork, vrf_id: int | None) -> bool:
    if ((r.get("vrf") or {}).get("id")) != vrf_id:
        return False
    try:
        start = ipaddress.ip_interface(r["start_address"]).ip
        end = ipaddress.ip_interface(r["end_address"]).ip
    except (KeyError, ValueError):
        return False
    return start.version == net.version and start in net and end in net


class PrefixService:
    def __init__(self, nb: NetBoxClient):
        self.nb = nb

    # ---- reads -----------------------------------------------------------

    async def search(
        self,
        *,
        q: str | None = None,
        family: int | None = None,
        vrf_id: int | None = None,
        global_only: bool = False,
        status: list[str] | None = None,
        within: str | None = None,
        limit: int = 50,
        offset: int = 0,
        include_utilization: bool = True,
        include_gateway: bool = False,
    ) -> Page[Prefix]:
        if within:
            within = str(addr.parse_network(within))
        params = {
            "q": q,
            "family": family,
            "vrf_id": NULL if global_only else vrf_id,
            "status": status,
            "within_include": within,
            "ordering": "vrf,prefix",
        }
        page = await self.nb.page(PREFIXES, params, limit=limit, offset=offset)
        items = [to_prefix(r) for r in page.results]
        if include_utilization and items:
            ranges = await self.utilized_ranges(items)
            sem = asyncio.Semaphore(UTILIZATION_CONCURRENCY)

            async def fill(p: Prefix) -> None:
                async with sem:
                    p.utilization = await self.utilization(p, ranges=ranges)

            await asyncio.gather(*(fill(p) for p in items))
        if include_gateway and items:
            found = await self.gateways(items)
            for p in items:
                p.gateway = found.get(p.id)
        return Page(count=page.count, limit=limit, offset=offset, results=items)

    async def gateways(self, prefixes: list[Prefix]) -> dict[int, Gateway]:
        """The gateways of a page of prefixes in one NetBox call (`parent` takes several values), each
        matched as in find_gateway: same network and mask, same VRF."""
        leaves = [p for p in prefixes if p.status.value != PrefixStatus.CONTAINER]
        if not leaves:
            return {}
        try:
            tagged = await self.nb.list_all(
                IPS, {"tag": GATEWAY_TAG_SLUG, "parent": sorted({p.prefix for p in leaves})}, cap=5000
            )
        except ValidationFailed as e:
            if "tag" in e.fields:  # no gateway recorded yet anywhere: the tag does not exist
                return {}
            raise
        out: dict[int, Gateway] = {}
        for p in leaves:
            net = addr.parse_network(p.prefix)
            vrf_id = p.vrf.id if p.vrf else None
            for ip in tagged:
                if ((ip.get("vrf") or {}).get("id")) == vrf_id and addr.parse_interface(
                    ip["address"]
                ).network == net:
                    out[p.id] = Gateway(ip_address_id=ip["id"], address=ip["address"])
                    break
        return out

    async def utilized_ranges(self, prefixes: list[Prefix]) -> list[dict[str, Any]]:
        """All IP ranges marked utilized inside any of these leaf prefixes, in one NetBox call.

        NetBox's `parent` filter on ip-ranges is multi-valued, so a page of prefixes costs one
        request here instead of one per prefix. Each prefix then picks its own ranges (same VRF).
        """
        leaves = [
            p.prefix for p in prefixes if p.status.value != PrefixStatus.CONTAINER and not p.mark_utilized
        ]
        if not leaves:
            return []
        return await self.nb.list_all(
            "ipam/ip-ranges/", {"parent": sorted(set(leaves)), "mark_utilized": True}, cap=5000
        )

    async def get(self, prefix_id: int) -> Prefix:
        raw = await self.nb.get(f"{PREFIXES}{prefix_id}/")
        p = to_prefix(raw)
        p.utilization, p.gateway = await asyncio.gather(self.utilization(p), self.find_gateway(p))
        return p

    async def utilization(self, p: Prefix, ranges: list[dict[str, Any]] | None = None) -> UtilizationOut:
        net = addr.parse_network(p.prefix)
        if p.mark_utilized:
            return UtilizationOut.model_validate(addr.marked_utilized(net, p.is_pool), from_attributes=True)
        vrf_id = p.vrf.id if p.vrf else None
        global_container = vrf_id is None and p.status.value == PrefixStatus.CONTAINER
        if p.status.value == PrefixStatus.CONTAINER:
            params: dict[str, Any] = {"within": str(net), "brief": True}
            if not global_container:
                params["vrf_id"] = vrf_filter(vrf_id)
            children = await self.nb.list_all(PREFIXES, params, cap=CHILD_PREFIX_CAP)
            nets = [ipaddress.ip_network(c["prefix"]) for c in children]
            u = addr.container_utilization(net, nets)
        else:
            ip_params: dict[str, Any] = {"parent": str(net), "vrf_id": vrf_filter(vrf_id)}
            if ranges is None:
                range_params = {"parent": str(net), "vrf_id": vrf_filter(vrf_id), "mark_utilized": True}
                ip_count, mine = await asyncio.gather(
                    self.nb.count(IPS, ip_params), self.nb.list_all("ipam/ip-ranges/", range_params, cap=1000)
                )
            else:
                ip_count = await self.nb.count(IPS, ip_params)
                mine = [r for r in ranges if _range_in(r, net, vrf_id)]
            # As NetBox: utilized ranges count whole (overlaps once), addresses inside them do not count again.
            intervals = addr.merge_ranges(
                (
                    int(ipaddress.ip_interface(r["start_address"]).ip),
                    int(ipaddress.ip_interface(r["end_address"]).ip),
                )
                for r in mine
            )
            range_size = sum(end - start + 1 for start, end in intervals)
            if intervals and ip_count and range_size < addr.utilization_denominator(net, p.is_pool):
                inside = await self.nb.count(
                    IPS,
                    {"parent": addr.interval_networks(intervals, net.version), "vrf_id": vrf_filter(vrf_id)},
                )
                ip_count = max(ip_count - inside, 0)
            u = addr.address_utilization(net, p.is_pool, ip_count, range_size)
        return UtilizationOut.model_validate(u, from_attributes=True)

    async def find_gateway(self, p: Prefix) -> Gateway | None:
        """The tagged address that belongs to *this* prefix: inside it and with the same mask.

        `parent=` alone would also match gateways of child prefixes (10.0.1.1/24 is inside 10.0.0.0/16).
        """
        if p.status.value == PrefixStatus.CONTAINER:
            return None
        net = addr.parse_network(p.prefix)
        try:
            page = await self.nb.page(
                IPS,
                {
                    "parent": p.prefix,
                    "mask_length": net.prefixlen,
                    "vrf_id": vrf_filter(p.vrf.id if p.vrf else None),
                    "tag": GATEWAY_TAG_SLUG,
                },
                limit=50,
            )
        except ValidationFailed as e:
            # NetBox rejects a filter on a tag that does not exist yet. Until Lanvex records its first
            # gateway (and creates the tag), no address can carry it.
            if "tag" in e.fields:
                return None
            raise
        for ip in page.results:
            if addr.parse_interface(ip["address"]).network == net:
                return Gateway(ip_address_id=ip["id"], address=ip["address"])
        return None

    async def available_prefixes(self, prefix_id: int) -> list[AvailablePrefix]:
        data = await self.nb.get(f"{PREFIXES}{prefix_id}/available-prefixes/")
        out = []
        for item in data or []:
            net = ipaddress.ip_network(item["prefix"])
            out.append(AvailablePrefix(prefix=str(net), family=net.version, size=net.num_addresses))
        return out

    async def available_ips(self, prefix_id: int, limit: int = 10) -> list[AvailableIP]:
        data = await self.nb.get(f"{PREFIXES}{prefix_id}/available-ips/", {"limit": min(max(limit, 1), 100)})
        return [AvailableIP(address=i["address"], family=int(i.get("family") or 4)) for i in data or []]

    # ---- validation ------------------------------------------------------

    async def validate(self, req: PrefixValidateRequest) -> PrefixValidation:
        findings: list[Finding] = []
        try:
            net = addr.parse_network(req.prefix)
        except addr.AddressError as e:
            findings.append(Finding(level="error", code="invalid_prefix", message=e.message, field="prefix"))
            return PrefixValidation(
                valid=False,
                normalized=e.suggestion,
                family=None,
                size=None,
                usable=None,
                suggested_gateway=None,
                findings=findings,
                related=[],
            )

        if req.vrf_id is not None:
            await self.nb.get(f"ipam/vrfs/{req.vrf_id}/")  # raises NotFound for an unknown VRF

        vrf = vrf_filter(req.vrf_id)
        parents_page, children_page = await asyncio.gather(
            self.nb.page(PREFIXES, {"contains": str(net), "vrf_id": vrf}, limit=100),
            self.nb.page(PREFIXES, {"within": str(net), "vrf_id": vrf}, limit=20),
        )
        related: list[RelatedPrefix] = []
        for raw in parents_page.results + children_page.results:
            relation = addr.classify_overlap(net, ipaddress.ip_network(raw["prefix"]))
            if relation is None:
                continue
            related.append(
                RelatedPrefix(
                    id=raw["id"],
                    prefix=raw["prefix"],
                    name=raw.get("description") or "",
                    status=(raw.get("status") or {}).get("value", ""),
                    relation=relation,  # type: ignore[arg-type]
                )
            )
        scope = "the selected VRF" if req.vrf_id is not None else "the global table"
        duplicates = [r for r in related if r.relation == "duplicate"]
        parents = sorted(
            (r for r in related if r.relation == "parent"),
            key=lambda r: ipaddress.ip_network(r.prefix).prefixlen,
        )
        if duplicates:
            findings.append(
                Finding(
                    level="error",
                    code="duplicate_prefix",
                    message=f"{net} already exists in {scope}.",
                    field="prefix",
                )
            )
        if parents:
            closest = parents[-1]
            label = f" ({closest.name})" if closest.name else ""
            findings.append(
                Finding(
                    level="info", code="has_parent", message=f"Will be nested under {closest.prefix}{label}."
                )
            )
        else:
            findings.append(
                Finding(level="info", code="top_level", message=f"Will be a top-level prefix in {scope}.")
            )
        if children_page.count:
            findings.append(
                Finding(
                    level="info",
                    code="has_children",
                    message=f"{children_page.count} existing prefix(es) in {scope} fall inside {net} "
                    "and will become its children.",
                )
            )
        if net.version == 4 and net.prefixlen < 8:
            findings.append(
                Finding(
                    level="warning",
                    code="very_large",
                    message=f"/{net.prefixlen} is unusually large for a network.",
                )
            )

        if req.gateway:
            try:
                gw = addr.parse_interface(req.gateway)
                problem = addr.address_in_prefix(gw, net)
                if problem:
                    findings.append(
                        Finding(level="error", code="invalid_gateway", message=problem, field="gateway")
                    )
            except addr.AddressError as e:
                findings.append(
                    Finding(level="error", code="invalid_gateway", message=e.message, field="gateway")
                )

        first, last = addr.usable_bounds(net)
        return PrefixValidation(
            valid=not any(f.level == "error" for f in findings),
            normalized=str(net),
            family=net.version,
            size=net.num_addresses,
            usable=last - first + 1,
            suggested_gateway=addr.suggest_gateway(net),
            findings=findings,
            related=sorted(related, key=lambda r: (r.relation, ipaddress.ip_network(r.prefix))),
        )

    # ---- writes ----------------------------------------------------------

    async def create(self, data: PrefixCreate) -> tuple[Prefix, list[str]]:
        validation = await self.validate(
            PrefixValidateRequest(prefix=data.prefix, vrf_id=data.vrf_id, gateway=data.gateway)
        )
        errors = [f for f in validation.findings if f.level == "error"]
        if errors:
            fields: dict[str, list[str]] = {}
            for f in errors:
                fields.setdefault(f.field or "__all__", []).append(f.message)
            raise ValidationFailed(errors[0].message, fields=fields)

        payload: dict[str, Any] = {
            "prefix": validation.normalized,
            "status": data.status.value,
            "description": data.name or "",
            "vrf": data.vrf_id,
            "vlan": data.vlan_id,
            "role": data.role_id,
            "tenant": data.tenant_id,
            "scope_type": "dcim.site" if data.site_id else None,
            "scope_id": data.site_id,
            "is_pool": bool(data.is_pool),
            "mark_utilized": bool(data.mark_utilized),
            "comments": data.comments or "",
        }
        raw = await self.nb.post(PREFIXES, payload)
        created = to_prefix(raw)

        warnings: list[str] = []
        if data.gateway:
            try:
                await self.set_gateway(created, data.gateway)
            except Exception as e:  # the prefix exists; report the partial outcome truthfully
                message = getattr(e, "message", None) or str(e)
                warnings.append(f"The prefix was created, but recording the gateway failed: {message}")
        return await self.get(created.id), warnings

    async def update(self, prefix_id: int, data: PrefixUpdate) -> tuple[Prefix, list[str]]:
        current = await self.get(prefix_id)
        sent = data.model_fields_set
        payload: dict[str, Any] = {}
        mapping = {
            "name": "description",
            "vrf_id": "vrf",
            "vlan_id": "vlan",
            "role_id": "role",
            "tenant_id": "tenant",
        }
        for field in (
            "name",
            "status",
            "vrf_id",
            "vlan_id",
            "role_id",
            "tenant_id",
            "is_pool",
            "mark_utilized",
            "comments",
        ):
            if field in sent:
                value = getattr(data, field)
                if field == "status" and value is not None:
                    value = value.value
                if field in ("name", "comments") and value is None:
                    value = ""
                payload[mapping.get(field, field)] = value

        if "site_id" in sent:
            payload["scope_type"] = "dcim.site" if data.site_id else None
            payload["scope_id"] = data.site_id
        if "vrf_id" in sent and data.vrf_id != (current.vrf.id if current.vrf else None):
            dup = await self.nb.count(PREFIXES, {"prefix": current.prefix, "vrf_id": vrf_filter(data.vrf_id)})
            if dup:
                raise ValidationFailed(
                    f"{current.prefix} already exists in the target VRF.",
                    fields={"vrf_id": ["Prefix already exists in this VRF."]},
                )
        if payload:
            await self.nb.patch(f"{PREFIXES}{prefix_id}/", payload)

        warnings: list[str] = []
        updated = await self.get(prefix_id)
        try:
            if data.clear_gateway and updated.gateway:
                await self.clear_gateway(updated.gateway.ip_address_id)
            elif data.gateway and (not updated.gateway or updated.gateway.address != data.gateway):
                if updated.gateway:
                    await self.clear_gateway(updated.gateway.ip_address_id)
                await self.set_gateway(updated, data.gateway)
        except ValidationFailed:
            raise
        except Exception as e:
            message = getattr(e, "message", None) or str(e)
            warnings.append(f"Prefix metadata was saved, but updating the gateway failed: {message}")
        return await self.get(prefix_id), warnings

    async def delete(self, prefix_id: int, confirm: str) -> Prefix:
        current = await self.get(prefix_id)
        if confirm.strip() != current.prefix:
            raise ValidationFailed(
                "Type the prefix exactly to confirm deletion.",
                fields={"confirm": [f"Expected {current.prefix}."]},
            )
        await self.nb.delete(f"{PREFIXES}{prefix_id}/")
        return current

    async def delete_impact(self, prefix_id: int) -> dict[str, Any]:
        p = await self.get(prefix_id)
        vrf = vrf_filter(p.vrf.id if p.vrf else None)
        child_prefixes, ip_count = await asyncio.gather(
            self.nb.count(PREFIXES, {"within": p.prefix, "vrf_id": vrf}),
            self.nb.count(IPS, {"parent": p.prefix, "vrf_id": vrf}),
        )
        return {"prefix": p.prefix, "child_prefixes": child_prefixes, "ip_addresses": ip_count}

    # ---- gateway ---------------------------------------------------------

    async def gateway_tag_id(self) -> int:
        page = await self.nb.page("extras/tags/", {"slug": GATEWAY_TAG_SLUG}, limit=1)
        if page.results:
            return int(page.results[0]["id"])
        raw = await self.nb.post(
            "extras/tags/",
            {
                "name": "Lanvex gateway",
                "slug": GATEWAY_TAG_SLUG,
                "color": "2563eb",
                "description": "Marks the gateway address of a prefix (managed by Lanvex).",
            },
        )
        return int(raw["id"])

    async def set_gateway(self, p: Prefix, gateway: str) -> None:
        net = addr.parse_network(p.prefix)
        try:
            gw = addr.parse_interface(gateway)
        except addr.AddressError as e:
            raise ValidationFailed(e.message, fields={"gateway": [e.message]}) from e
        problem = addr.address_in_prefix(gw, net, p.is_pool)
        if problem:
            raise ValidationFailed(problem, fields={"gateway": [problem]})
        tag_id = await self.gateway_tag_id()
        vrf_id = p.vrf.id if p.vrf else None
        existing = await self.nb.page(IPS, {"address": str(gw), "vrf_id": vrf_filter(vrf_id)}, limit=1)
        if existing.results:
            ip = existing.results[0]
            tag_ids = sorted({int(t["id"]) for t in ip.get("tags", [])} | {tag_id})
            await self.nb.patch(f"{IPS}{ip['id']}/", {"tags": tag_ids})
        else:
            await self.nb.post(
                IPS,
                {
                    "address": str(gw),
                    "vrf": vrf_id,
                    "status": "active",
                    "description": "Gateway",
                    "tags": [tag_id],
                },
            )

    async def clear_gateway(self, ip_address_id: int) -> None:
        """Remove the gateway marker. The IP address object itself is kept."""
        ip = await self.nb.get(f"{IPS}{ip_address_id}/")
        remaining = [int(t["id"]) for t in ip.get("tags", []) if t.get("slug") != GATEWAY_TAG_SLUG]
        await self.nb.patch(f"{IPS}{ip_address_id}/", {"tags": remaining})


async def require_prefix(nb: NetBoxClient, prefix_id: int) -> dict[str, Any]:
    try:
        return await nb.get(f"{PREFIXES}{prefix_id}/")
    except NotFound as e:
        raise NotFound(f"Prefix {prefix_id} was not found in {nb.name}.") from e
