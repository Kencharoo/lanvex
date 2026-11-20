"""A server's gateways, DNS and static routes compared with the documentation.

- Default gateway: each default route the server has, against the gateway documented for the prefix
  its next hop is in (Lanvex's `lanvex-gateway` tag). Routes through a next hop in no documented prefix
  are left out: nothing documents them.
- DNS servers and search domains, and static routes: against the device's config context, key
  `lanvex` (`dns_servers`, `dns_search`, `routes: [{to, via}]`), as NetBox renders it from config
  contexts and the device's own local context data. A key that is missing documents nothing.
"""

import ipaddress
from dataclasses import dataclass
from typing import Any

from lanvex.integrations.linux.facts import HostFacts
from lanvex.servers.schemas import SettingRow

KEY = "lanvex"


@dataclass
class DocumentedPrefix:
    prefix: str
    prefix_id: int
    gateway: str | None  # the gateway's address, without its prefix length


def server_dns(facts: HostFacts) -> tuple[list[str], list[str]]:
    """The DNS servers the server uses, and its search domains."""
    search: list[str] = []
    for scope in facts.dns.resolved or []:
        for d in scope.domains:
            if d != "~." and d not in search:
                search.append(d)
    if not search:
        search = list(facts.dns.resolv_conf_search)
    return list(facts.dns.effective_servers), search


def static_routes(facts: HostFacts) -> list[tuple[str, str, str | None]]:
    """(destination, next hop, interface) of the routes set in the configuration or by hand."""
    out = []
    for r in facts.routes:
        if (
            r.is_default
            or not r.gateway
            or r.protocol not in ("static", "boot")
            or r.type not in (None, "unicast")
        ):
            continue
        out.append((_net(r.destination), _ip(r.gateway), r.interface))
    return out


def default_routes(facts: HostFacts) -> list[tuple[int, str, str | None]]:
    out: list[tuple[int, str, str | None]] = []
    for r in facts.routes:
        if r.is_default and r.gateway and not any(o[:2] == (r.family, _ip(r.gateway)) for o in out):
            out.append((r.family, _ip(r.gateway), r.interface))  # once, also when several tables have it
    return out


def _net(text: str) -> str:
    try:
        return str(ipaddress.ip_network(text, strict=False))
    except ValueError:
        return text


def _ip(text: str | None) -> str:
    try:
        return str(ipaddress.ip_address(str(text).split("%")[0]))
    except ValueError:
        return str(text)


def documented(config_context: dict[str, Any] | None) -> dict[str, Any] | None:
    doc = (config_context or {}).get(KEY)
    return doc if isinstance(doc, dict) else None


def documented_routes(doc: dict[str, Any] | None) -> list[dict[str, str]] | None:
    """The documented static routes as [{to, via}], normalised; None when none are documented."""
    raw = (doc or {}).get("routes")
    if not isinstance(raw, list):
        return None
    return [
        {"to": _net(str(r["to"])), "via": _ip(str(r["via"]))}
        for r in raw
        if isinstance(r, dict) and r.get("to") and r.get("via")
    ]


def _list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    return [str(v).strip() for v in value if str(v).strip()] if isinstance(value, list) else None


def compare(
    facts: HostFacts, doc: dict[str, Any] | None, prefixes: dict[str, DocumentedPrefix]
) -> list[SettingRow]:
    """`prefixes`: for each next hop of a default route, the documented prefix it is in."""
    rows: list[SettingRow] = []
    for family, via, iface in default_routes(facts):
        p = prefixes.get(via)
        if p is None:
            continue
        status = "only_on_server" if p.gateway is None else "match" if _ip(p.gateway) == via else "differs"
        rows.append(
            SettingRow(
                id=f"gateway:{via}",
                kind="gateway",
                status=status,
                key=f"IPv{family}",
                interface=iface,
                server=via,
                documented=p.gateway,
                prefix=p.prefix,
                prefix_id=p.prefix_id,
            )
        )

    doc = doc or {}
    servers, search = server_dns(facts)
    for kind, have, want in (
        ("dns_servers", [_ip(s) for s in servers], _list(doc.get("dns_servers"))),
        ("dns_search", [s.lower().rstrip(".") for s in search], _list(doc.get("dns_search"))),
    ):
        if want is not None and kind == "dns_servers":
            want = [_ip(s) for s in want]
        elif want is not None:
            want = [s.lower().rstrip(".") for s in want]
        if want is None and not have:
            continue
        status = "only_on_server" if want is None else "match" if have == want else "differs"
        if want is not None and not have:
            status = "only_documented"
        rows.append(
            SettingRow(
                id=kind,
                kind=kind,  # type: ignore[arg-type]
                status=status,  # type: ignore[arg-type]
                key=kind,
                server=", ".join(have) or None,
                documented=", ".join(want) if want is not None else None,
            )
        )

    have_routes = {to: (via, iface) for to, via, iface in static_routes(facts)}
    listed = documented_routes(doc)
    want_routes = {r["to"]: r["via"] for r in listed} if listed is not None else None
    for to in sorted(set(have_routes) | set(want_routes or {}), key=_sort_key):
        via, iface = have_routes.get(to, (None, None))
        want = (want_routes or {}).get(to)
        if via is None:
            status = "only_documented"
        elif want_routes is None or want is None:
            status = "only_on_server"
        else:
            status = "match" if want == via else "differs"
        rows.append(
            SettingRow(
                id=f"route:{to}",
                kind="route",
                status=status,  # type: ignore[arg-type]
                key=to,
                interface=iface,
                server=via,  # the next hop
                documented=want,
            )
        )
    return rows


def _sort_key(net: str) -> tuple[int, Any]:
    try:
        n = ipaddress.ip_network(net)
        return (n.version, n)
    except ValueError:
        return (9, net)
