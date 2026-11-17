"""TEST DOUBLE: an in-memory stand-in for the subset of the NetBox 4.7 REST API that Lanvex uses.

For UI development and end-to-end tests when no NetBox is available. It is NOT NetBox: it
implements only the endpoints and filters Lanvex calls, with simplified validation. Passing
tests against it does not prove compatibility with a real NetBox — run tests/test_integration_netbox.py
against a disposable NetBox for that.

    uv run uvicorn tests.fake_netbox:app --port 8001
    LANVEX_NETBOX_URL=http://127.0.0.1:8001 LANVEX_NETBOX_TOKEN=nbt_fake.token ...
"""

import ipaddress
import itertools
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response

app = FastAPI(title="Fake NetBox (test double)")
BASE = "http://127.0.0.1:8001"
_ids = itertools.count(1000)

DB: dict[str, dict[int, dict[str, Any]]] = {}


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _choice(value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    return {
        "value": value,
        "label": {"dhcp": "DHCP", "slaac": "SLAAC"}.get(value, value.replace("-", " ").title()),
    }


def _brief(kind: str, obj: dict[str, Any] | None) -> dict[str, Any] | None:
    if not obj:
        return None
    out = {
        "id": obj["id"],
        "url": f"{BASE}/api/{kind}/{obj['id']}/",
        "display": obj.get("name") or obj.get("address"),
    }
    for k in ("name", "slug", "rd", "vid", "address", "model"):
        if k in obj:
            out[k] = obj[k]
    return out


def seed() -> None:
    DB.clear()
    for k in (
        "vrfs",
        "roles",
        "tenants",
        "sites",
        "vlan-groups",
        "vlans",
        "prefixes",
        "ip-addresses",
        "ip-ranges",
        "tags",
        "devices",
        "interfaces",
        "device-roles",
        "manufacturers",
        "object-changes",
        "cables",
    ):
        DB[k] = {}

    def add(kind: str, **obj: Any) -> dict[str, Any]:
        obj.setdefault("id", next(_ids))
        obj.setdefault("created", now())
        obj.setdefault("last_updated", now())
        DB[kind][obj["id"]] = obj
        return obj

    prod = add("vrfs", name="prod", rd="65000:10", description="")
    add("vrfs", name="mgmt", rd="65000:99", description="")
    hq = add("sites", name="Koblenz HQ", slug="koblenz-hq")
    add("sites", name="Frankfurt DC", slug="frankfurt-dc")
    infra = add("roles", name="Infrastructure", slug="infrastructure")
    add("roles", name="Users", slug="users")
    campus = add("vlan-groups", name="Campus", slug="campus")
    v10 = add(
        "vlans",
        vid=10,
        name="Staff",
        status="active",
        site=hq["id"],
        group=campus["id"],
        description="Staff workstations",
    )
    v20 = add(
        "vlans", vid=20, name="Voice", status="active", site=hq["id"], group=campus["id"], description=""
    )
    v99 = add(
        "vlans",
        vid=99,
        name="Management",
        status="active",
        site=hq["id"],
        group=campus["id"],
        description="Out-of-band",
    )
    add(
        "vlans",
        vid=30,
        name="Guest Wi-Fi",
        status="reserved",
        site=hq["id"],
        group=campus["id"],
        description="",
    )

    add("prefixes", prefix="10.0.0.0/8", status="container", description="Corporate RFC1918", vrf=None)
    add("prefixes", prefix="10.10.0.0/16", status="container", description="Koblenz campus", vrf=None)
    staff = add(
        "prefixes",
        prefix="10.10.10.0/24",
        status="active",
        description="Staff LAN",
        vlan=v10["id"],
        vrf=None,
        role=infra["id"],
    )
    add("prefixes", prefix="10.10.20.0/24", status="active", description="Voice", vlan=v20["id"], vrf=None)
    add(
        "prefixes",
        prefix="10.10.99.0/26",
        status="active",
        description="Switch management",
        vlan=v99["id"],
        vrf=None,
    )
    add("prefixes", prefix="10.10.128.0/20", status="reserved", description="Future expansion", vrf=None)
    add("prefixes", prefix="172.16.0.0/22", status="active", description="Production servers", vrf=prod["id"])
    add("prefixes", prefix="2001:db8:10::/48", status="container", description="Campus IPv6", vrf=None)
    add(
        "prefixes",
        prefix="2001:db8:10:10::/64",
        status="active",
        description="Staff LAN v6",
        vlan=v10["id"],
        vrf=None,
    )

    gw_tag = add("tags", name="Lanvex gateway", slug="lanvex-gateway", color="2563eb")
    mfr = add("manufacturers", name="Cisco", slug="cisco")
    role_core = add("device-roles", name="Core switch", slug="core-switch", color="1f6feb")
    role_access = add("device-roles", name="Access switch", slug="access-switch", color="2da44e")
    role_server = add("device-roles", name="Server", slug="server", color="bf8700")
    sw = add(
        "devices",
        name="kob-core-01",
        status="active",
        role=role_core["id"],
        site=hq["id"],
        device_type={
            "id": 1,
            "model": "Catalyst 9300-48P",
            "manufacturer": _brief("dcim/manufacturers", mfr),
        },
        serial="FOC2419X0AB",
        asset_tag=None,
        primary_ip=None,
        description="",
    )
    names = ["Gi1/0/1", "Gi1/0/2", "Gi1/0/3", "Gi1/0/48", "Vlan99"]
    ifaces = [
        add(
            "interfaces",
            device=sw["id"],
            name=n,
            type="1000base-t" if n.startswith("Gi") else "virtual",
            enabled=True,
            mgmt_only=n == "Vlan99",
            mode=None,
            untagged_vlan=None,
            tagged_vlans=[],
            description="",
        )
        for n in names
    ]
    ifaces[0].update(mode="access", untagged_vlan=v10["id"])

    # A small campus around the core switch, and a second site joined by a provider circuit.
    def device(
        name: str, role: dict[str, Any], site: dict[str, Any], model: str, ports: list[str]
    ) -> list[dict[str, Any]]:
        dev = add(
            "devices",
            name=name,
            status="active",
            role=role["id"],
            site=site["id"],
            device_type={"id": 2, "model": model, "manufacturer": _brief("dcim/manufacturers", mfr)},
            serial="",
            asset_tag=None,
            primary_ip=None,
            description="",
        )
        return [
            add(
                "interfaces",
                device=dev["id"],
                name=n,
                type="1000base-t",
                enabled=True,
                mgmt_only=False,
                mode=None,
                untagged_vlan=None,
                tagged_vlans=[],
                description="",
            )
            for n in ports
        ]

    fra = next(x for x in DB["sites"].values() if x["slug"] == "frankfurt-dc")
    acc1 = device("kob-acc-01", role_access, hq, "Catalyst 9200-24P", ["Gi1/0/1", "Gi1/0/49"])
    acc2 = device("kob-acc-02", role_access, hq, "Catalyst 9200-24P", ["Gi1/0/1", "Gi1/0/49"])
    srv = device("kob-srv-01", role_server, hq, "PowerEdge R650", ["eno1"])
    fra_core = device("fra-core-01", role_core, fra, "Catalyst 9500-24Y4C", ["Te1/0/1", "Te1/0/24"])

    def cable(a: dict[str, Any] | tuple[str, int], b: dict[str, Any] | tuple[str, int], **extra: Any) -> None:
        end = lambda t: t if isinstance(t, tuple) else ("dcim.interface", t["id"])  # noqa: E731
        add(
            "cables",
            a=[end(a)],
            b=[end(b)],
            status=extra.pop("status", "connected"),
            type=extra.pop("type", "cat6"),
            label=extra.pop("label", ""),
            color="",
            description="",
        )

    cable(ifaces[1], acc1[1], label="uplink-1")
    cable(ifaces[2], acc2[1], label="uplink-2")
    cable(acc1[0], srv[0])
    cable(acc2[0], fra_core[0], status="planned")
    cable(ifaces[3], ("circuits.circuittermination", 1), type="smf")
    cable(fra_core[1], ("circuits.circuittermination", 2), type="smf")
    ifaces[3].update(mode="tagged", untagged_vlan=v99["id"], tagged_vlans=[v10["id"], v20["id"]])
    add(
        "ip-addresses",
        address="10.10.10.1/24",
        status="active",
        vrf=None,
        description="Gateway",
        tags=[gw_tag["id"]],
        dns_name="",
    )
    mgmt_ip = add(
        "ip-addresses",
        address="10.10.99.2/26",
        status="active",
        vrf=None,
        description="kob-core-01 mgmt",
        dns_name="kob-core-01.mgmt.example.net",
        tags=[],
        assigned_object_id=ifaces[4]["id"],
    )
    sw["primary_ip"] = mgmt_ip["id"]
    for i in range(10, 140):
        add(
            "ip-addresses",
            address=f"10.10.10.{i}/24",
            status="dhcp" if i > 100 else "active",
            vrf=None,
            description="" if i > 100 else f"ws-{i:03d}",
            tags=[],
            dns_name="",
        )
    add(
        "ip-addresses",
        address="10.10.20.5/24",
        status="reserved",
        vrf=None,
        description="PBX",
        tags=[],
        dns_name="",
    )
    _ = staff


seed()


# ---- serialization ---------------------------------------------------------------


def ser(kind: str, o: dict[str, Any]) -> dict[str, Any]:
    d = dict(o)
    d["url"] = f"{BASE}/api/{kind}/{o['id']}/"
    d["display_url"] = f"{BASE}/{kind}/{o['id']}/"
    if kind == "prefixes":
        net = ipaddress.ip_network(o["prefix"])
        vrf_id = o.get("vrf")
        depth = sum(
            1
            for p in DB["prefixes"].values()
            if p.get("vrf") == vrf_id
            and p["id"] != o["id"]
            and ipaddress.ip_network(p["prefix"]).version == net.version
            and net != ipaddress.ip_network(p["prefix"])
            and net.subnet_of(ipaddress.ip_network(p["prefix"]))
        )
        children = sum(
            1
            for p in DB["prefixes"].values()
            if p.get("vrf") == vrf_id
            and p["id"] != o["id"]
            and ipaddress.ip_network(p["prefix"]).version == net.version
            and ipaddress.ip_network(p["prefix"]) != net
            and ipaddress.ip_network(p["prefix"]).subnet_of(net)
        )
        d.update(
            family={"value": net.version, "label": f"IPv{net.version}"},
            status=_choice(o["status"]),
            vrf=_brief("ipam/vrfs", DB["vrfs"].get(vrf_id)),
            vlan=_brief("ipam/vlans", DB["vlans"].get(o.get("vlan"))),
            role=_brief("ipam/roles", DB["roles"].get(o.get("role"))),
            tenant=None,
            scope_type=None,
            scope_id=None,
            scope=None,
            is_pool=o.get("is_pool", False),
            mark_utilized=o.get("mark_utilized", False),
            comments=o.get("comments", ""),
            tags=[],
            custom_fields={},
            _depth=depth,
            children=children,
        )
    elif kind == "ip-addresses":
        iface = DB["interfaces"].get(o.get("assigned_object_id"))
        assigned = None
        if iface:
            dev = DB["devices"][iface["device"]]
            assigned = {
                "id": iface["id"],
                "name": iface["name"],
                "display": iface["name"],
                "device": _brief("dcim/devices", dev),
            }
        v = ipaddress.ip_interface(o["address"]).version
        d.update(
            family={"value": v, "label": f"IPv{v}"},
            status=_choice(o["status"]),
            role=_choice(o.get("role")),
            vrf=_brief("ipam/vrfs", DB["vrfs"].get(o.get("vrf"))),
            tenant=None,
            assigned_object_type="dcim.interface" if iface else None,
            assigned_object_id=iface["id"] if iface else None,
            assigned_object=assigned,
            tags=[ser_tag(t) for t in o.get("tags", [])],
        )
    elif kind == "vlans":
        d.update(
            status=_choice(o["status"]),
            site=_brief("dcim/sites", DB["sites"].get(o.get("site"))),
            group=_brief("ipam/vlan-groups", DB["vlan-groups"].get(o.get("group"))),
            role=None,
            tenant=None,
            tags=[],
            prefix_count=sum(1 for p in DB["prefixes"].values() if p.get("vlan") == o["id"]),
        )
    elif kind == "devices":
        ip = DB["ip-addresses"].get(o.get("primary_ip"))
        d.update(
            status=_choice(o["status"]),
            role=_brief("dcim/device-roles", DB["device-roles"].get(o.get("role"))),
            site=_brief("dcim/sites", DB["sites"].get(o.get("site"))),
            primary_ip=_brief("ipam/ip-addresses", ip),
            primary_ip4=_brief("ipam/ip-addresses", ip),
            oob_ip=None,
            interface_count=sum(1 for i in DB["interfaces"].values() if i["device"] == o["id"]),
            platform=None,
            location=None,
            rack=None,
            tenant=None,
            comments="",
        )
    elif kind == "interfaces":
        d.update(
            device=_brief("dcim/devices", DB["devices"].get(o["device"])),
            type=_choice(o.get("type")),
            mode=_choice(o.get("mode")),
            untagged_vlan=_brief("ipam/vlans", DB["vlans"].get(o.get("untagged_vlan"))),
            tagged_vlans=[
                _brief("ipam/vlans", DB["vlans"][v]) for v in o.get("tagged_vlans", []) if v in DB["vlans"]
            ],
            cable=next(
                (
                    {"id": c["id"]}
                    for c in DB["cables"].values()
                    if ("dcim.interface", o["id"]) in [tuple(t) for t in c["a"] + c["b"]]
                ),
                None,
            ),
            mark_connected=False,
            mac_address=None,
            mtu=None,
            lag=None,
        )
    elif kind == "cables":

        def term(t: tuple[str, int] | list[Any]) -> dict[str, Any]:
            kind_, oid = t
            if kind_ == "circuits.circuittermination":
                circuit = {
                    "id": 1,
                    "cid": "CID-4711",
                    "display": "CID-4711",
                    "provider": {"id": 1, "name": "Example Carrier", "slug": "example-carrier"},
                }
                obj = {
                    "id": oid,
                    "circuit": circuit,
                    "term_side": "A" if oid == 1 else "Z",
                    "cable": {"id": o["id"]},
                }
            else:
                i = DB["interfaces"][oid]
                obj = {
                    "id": oid,
                    "name": i["name"],
                    "device": _brief("dcim/devices", DB["devices"][i["device"]]),
                    "cable": {"id": o["id"]},
                }
            return {"object_type": kind_, "object_id": oid, "object": obj}

        d = {
            "id": o["id"],
            "url": d["url"],
            "display_url": d["display_url"],
            "display": f"#{o['id']}",
            "a_terminations": [term(t) for t in o["a"]],
            "b_terminations": [term(t) for t in o["b"]],
            "status": _choice(o["status"]),
            "type": o.get("type", ""),
            "label": o.get("label", ""),
            "color": o.get("color", ""),
            "description": "",
            "created": o.get("created"),
            "last_updated": o.get("last_updated"),
        }
    return d


def ser_tag(tid: int) -> dict[str, Any]:
    t = DB["tags"][tid]
    return {"id": t["id"], "name": t["name"], "slug": t["slug"], "color": t["color"], "display": t["name"]}


# ---- filtering ------------------------------------------------------------------


def _net(o: dict[str, Any], kind: str) -> Any:
    return ipaddress.ip_network(o["prefix"]) if kind == "prefixes" else ipaddress.ip_interface(o["address"])


def matches(kind: str, o: dict[str, Any], qp: Any) -> bool:
    def vrf_ok() -> bool:
        vals = qp.getlist("vrf_id")
        if not vals:
            return True
        return any(
            (v == "null" and o.get("vrf") is None) or (v != "null" and o.get("vrf") == int(v)) for v in vals
        )

    if kind in ("prefixes", "ip-addresses", "ip-ranges") and not vrf_ok():
        return False
    status = qp.getlist("status")
    if status and o.get("status") not in status:
        return False
    if (fam := qp.get("family")) and kind in ("prefixes", "ip-addresses"):
        if _net(o, kind).version != int(fam):
            return False
    q = (qp.get("q") or "").lower()
    if kind == "prefixes":
        net = _net(o, kind)
        if q and q not in o["prefix"].lower() and q not in (o.get("description") or "").lower():
            return False
        if (p := qp.get("prefix")) and net != ipaddress.ip_network(p, strict=False):
            return False
        if c := qp.get("contains"):
            target = ipaddress.ip_network(c, strict=False)
            if target.version != net.version or not target.subnet_of(net):
                return False
        if w := qp.get("within"):
            parent = ipaddress.ip_network(w)
            if parent.version != net.version or net == parent or not net.subnet_of(parent):
                return False
        if w := qp.get("within_include"):
            parent = ipaddress.ip_network(w)
            if parent.version != net.version or not net.subnet_of(parent):
                return False
    elif kind == "ip-addresses":
        iface = _net(o, kind)
        if q and not (
            o["address"].startswith(q)
            or q in (o.get("description") or "").lower()
            or q in (o.get("dns_name") or "").lower()
        ):
            return False
        if (p := qp.get("parent")) and iface.ip not in ipaddress.ip_network(p, strict=False):
            return False
        if wanted := qp.getlist("address"):
            # As in NetBox: a value with a mask matches exactly, a bare host matches any mask.
            if not any(o["address"] == a if "/" in a else str(iface.ip) == a for a in wanted):
                return False
        if (m := qp.get("mask_length")) and iface.network.prefixlen != int(m):
            return False
        if (t := qp.get("tag")) and not any(DB["tags"][x]["slug"] == t for x in o.get("tags", [])):
            return False
        if (a := qp.get("assigned")) is not None:
            if (a == "true") != (o.get("assigned_object_id") is not None):
                return False
        if dev := qp.get("device_id"):
            i = DB["interfaces"].get(o.get("assigned_object_id"))
            if not i or i["device"] != int(dev):
                return False
    elif kind in (
        "vlans",
        "devices",
        "vrfs",
        "sites",
        "roles",
        "vlan-groups",
        "device-roles",
        "manufacturers",
        "tags",
    ):
        text = " ".join(str(o.get(k, "")) for k in ("name", "vid", "serial", "slug")).lower()
        if q and q not in text:
            return False
        for f, key in (("site_id", "site"), ("group_id", "group"), ("role_id", "role")):
            if (v := qp.get(f)) and o.get(key) != int(v):
                return False
        if (s := qp.get("slug")) and o.get("slug") != s:
            return False
        if (names := qp.getlist("name")) and o.get("name") not in names:
            return False
    elif kind == "interfaces":
        if (dev := qp.get("device_id")) and o["device"] != int(dev):
            return False
        if (macs := qp.getlist("mac_address")) and (o.get("mac_address") or "").upper() not in {
            m.upper() for m in macs
        }:
            return False
    elif kind == "cables":
        if site := qp.get("site_id"):
            sites = {
                DB["devices"][DB["interfaces"][oid]["device"]]["site"]
                for t, oid in o["a"] + o["b"]
                if t == "dcim.interface"
            }
            if int(site) not in sites:
                return False
    return True


def sort_key(kind: str, o: dict[str, Any]) -> Any:
    if kind == "prefixes":
        n = ipaddress.ip_network(o["prefix"])
        return (o.get("vrf") or 0, n.version, int(n.network_address), n.prefixlen)
    if kind == "ip-addresses":
        i = ipaddress.ip_interface(o["address"])
        return (o.get("vrf") or 0, i.version, int(i.ip))
    if kind == "vlans":
        return o["vid"]
    return o.get("name") or o["id"]


ROUTES = {
    "ipam/prefixes": "prefixes",
    "ipam/ip-addresses": "ip-addresses",
    "ipam/ip-ranges": "ip-ranges",
    "ipam/vrfs": "vrfs",
    "ipam/roles": "roles",
    "ipam/vlans": "vlans",
    "ipam/vlan-groups": "vlan-groups",
    "extras/tags": "tags",
    "dcim/devices": "devices",
    "dcim/interfaces": "interfaces",
    "dcim/sites": "sites",
    "dcim/device-roles": "device-roles",
    "dcim/manufacturers": "manufacturers",
    "dcim/cables": "cables",
    "tenancy/tenants": "tenants",
    "core/object-changes": "object-changes",
}
FK = {"vrf", "vlan", "role", "site", "group", "untagged_vlan"}
CONTENT_TYPES = {
    "prefixes": "ipam.prefix",
    "ip-addresses": "ipam.ipaddress",
    "vlans": "ipam.vlan",
    "tags": "extras.tag",
    "interfaces": "dcim.interface",
    "devices": "dcim.device",
}


def _auth(authorization: str | None) -> None:
    if not authorization or not authorization.startswith(("Bearer nbt_", "Token ")):
        raise HTTPException(403, "Authentication credentials were not provided.")


def _log(action: str, kind: str, o: dict[str, Any]) -> None:
    cid = next(_ids)
    DB["object-changes"][cid] = {
        "id": cid,
        "time": now(),
        "user_name": "lanvex",
        "action": {"value": action, "label": action.title()},
        "changed_object_type": CONTENT_TYPES.get(kind, kind),
        "object_repr": o.get("prefix") or o.get("address") or o.get("name", ""),
    }


@app.exception_handler(HTTPException)
async def _err(_: Request, exc: HTTPException) -> JSONResponse:
    body = exc.detail if isinstance(exc.detail, dict) else {"detail": exc.detail}
    return JSONResponse(body, status_code=exc.status_code)


@app.get("/api/status/")
def status() -> dict[str, Any]:
    return {
        "netbox-version": "4.7.4",
        "django-version": "6.1.2",
        "python-version": "3.13",
        "plugins": {},
        "rq-workers-running": 1,
    }


@app.get("/api/authentication-check/")
def auth_check(authorization: str | None = Header(None)) -> dict[str, Any]:
    _auth(authorization)
    return {"id": 1, "username": "lanvex"}


@app.post("/__reset__")
def reset() -> dict[str, str]:
    seed()
    return {"status": "reset"}


def _available(p: dict[str, Any]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    net = ipaddress.ip_network(p["prefix"])
    used = [
        ipaddress.ip_network(c["prefix"])
        for c in DB["prefixes"].values()
        if c.get("vrf") == p.get("vrf")
        and c["id"] != p["id"]
        and ipaddress.ip_network(c["prefix"]).version == net.version
        and ipaddress.ip_network(c["prefix"]) != net
        and ipaddress.ip_network(c["prefix"]).subnet_of(net)
    ]
    free = [net]
    for u in ipaddress.collapse_addresses(used):  # type: ignore[arg-type]
        nxt = []
        for f in free:
            if u.subnet_of(f):
                nxt.extend(f.address_exclude(u))
            else:
                nxt.append(f)
        free = nxt
    return sorted(free, key=lambda n: int(n.network_address))


def _free_ips(p: dict[str, Any], limit: int) -> list[str]:
    net = ipaddress.ip_network(p["prefix"])
    taken = {
        ipaddress.ip_interface(i["address"]).ip
        for i in DB["ip-addresses"].values()
        if i.get("vrf") == p.get("vrf")
    }
    hosts = net.hosts() if net.version == 4 and net.prefixlen < 31 else iter(net)
    out = []
    for h in hosts:
        if net.version == 6 and h == net.network_address:
            continue
        if h not in taken:
            out.append(f"{h}/{net.prefixlen}")
            if len(out) >= limit:
                break
    return out


@app.api_route("/api/{app_name}/{model}/{rest:path}", methods=["GET", "POST", "PATCH", "DELETE"])
async def generic(
    app_name: str, model: str, rest: str, request: Request, authorization: str | None = Header(None)
) -> Any:
    _auth(authorization)
    kind = ROUTES.get(f"{app_name}/{model}")
    if kind is None:
        raise HTTPException(404, "Not found.")
    parts = [x for x in rest.split("/") if x]
    store = DB[kind]
    qp = request.query_params

    if parts and parts[0].isdigit():
        oid = int(parts[0])
        if oid not in store:
            raise HTTPException(404, "No object matches the given query.")
        obj = store[oid]
        if len(parts) == 2 and kind == "prefixes" and parts[1] == "available-prefixes":
            return [{"prefix": str(n), "family": n.version, "vrf": None} for n in _available(obj)]
        if len(parts) == 2 and kind == "prefixes" and parts[1] == "available-ips":
            if request.method == "POST":
                body = await request.json()
                free = _free_ips(obj, 1)
                if not free:
                    raise HTTPException(
                        409, "Insufficient space is available to accommodate the requested IP(s)."
                    )
                new = {
                    "id": next(_ids),
                    "address": free[0],
                    "vrf": obj.get("vrf"),
                    "status": body.get("status", "active"),
                    "description": body.get("description", ""),
                    "dns_name": body.get("dns_name", ""),
                    "tags": [],
                    "assigned_object_id": body.get("assigned_object_id"),
                    "created": now(),
                    "last_updated": now(),
                }
                DB["ip-addresses"][new["id"]] = new
                _log("create", "ip-addresses", new)
                return JSONResponse(ser("ip-addresses", new), status_code=201)
            limit = int(qp.get("limit", 50))
            return [
                {"family": ipaddress.ip_interface(a).version, "address": a, "vrf": None}
                for a in _free_ips(obj, limit)
            ]
        if request.method == "GET":
            return ser(kind, obj)
        if request.method == "DELETE":
            del store[oid]
            _log("delete", kind, obj)
            return Response(status_code=204)
        body = await request.json()
        for k, v in body.items():
            obj[k] = v
        obj["last_updated"] = now()
        _log("update", kind, obj)
        return ser(kind, obj)

    if request.method == "POST":
        body = await request.json()
        if kind == "prefixes":
            try:
                ipaddress.ip_network(body["prefix"])
            except ValueError:
                raise HTTPException(
                    400,
                    {
                        "prefix": [
                            f"{body.get('prefix')} is not a valid prefix. Did you mean {ipaddress.ip_network(body['prefix'], strict=False)}?"
                        ]
                    },
                ) from None
            if any(p["prefix"] == body["prefix"] and p.get("vrf") == body.get("vrf") for p in store.values()):
                raise HTTPException(
                    400, {"prefix": [f"Duplicate prefix found in global table: {body['prefix']}"]}
                )
        if (
            kind == "vlans"
            and body.get("group")
            and any(v["vid"] == body["vid"] and v.get("group") == body["group"] for v in store.values())
        ):
            raise HTTPException(400, {"__all__": ["VLAN with this Group and VID already exists."]})
        for k in FK:
            if (
                body.get(k) is not None
                and k in ("vrf", "vlan", "role")
                and body[k] not in DB[{"vrf": "vrfs", "vlan": "vlans", "role": "roles"}[k]]
            ):
                raise HTTPException(
                    400, {k: [f"Related object not found using the provided numeric ID: {body[k]}"]}
                )
        obj = {"id": next(_ids), **body, "created": now(), "last_updated": now()}
        obj.setdefault("status", "active")
        store[obj["id"]] = obj
        _log("create", kind, obj)
        return JSONResponse(ser(kind, obj), status_code=201)

    items = sorted((o for o in store.values() if matches(kind, o, qp)), key=lambda o: sort_key(kind, o))
    if kind == "object-changes":
        items = sorted(store.values(), key=lambda o: o["time"], reverse=True)
    limit, offset = min(int(qp.get("limit", 50)), 1000), int(qp.get("offset", 0))
    page = items[offset : offset + limit]
    return {"count": len(items), "next": None, "previous": None, "results": [ser(kind, o) for o in page]}
