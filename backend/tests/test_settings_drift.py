"""Gateways, DNS and static routes compared with the documentation (servers/settings_drift.py)."""

from lanvex.integrations.linux.assemble import assemble
from lanvex.integrations.linux.facts import Route
from lanvex.servers.settings_drift import DocumentedPrefix, compare, documented

from .test_linux_parsing import load


def facts():  # type: ignore[no-untyped-def]
    f = assemble(load("netplan"))  # default route via 172.30.0.1 on eth0 (in two tables); DNS on eth0
    f.routes.append(
        Route(
            destination="10.50.0.0/16", family=4, gateway="172.30.0.254", interface="eth0", protocol="static"
        )
    )
    f.routes.append(
        Route(destination="10.60.0.0/16", family=4, gateway="172.30.0.254", interface="eth0", protocol="dhcp")
    )
    return f


PREFIX = {"172.30.0.1": DocumentedPrefix(prefix="172.30.0.0/24", prefix_id=7, gateway="172.30.0.1")}


def rows(doc, prefixes=PREFIX):  # type: ignore[no-untyped-def]
    return {(r.kind, r.key): r for r in compare(facts(), doc, prefixes)}


def test_everything_as_documented() -> None:
    doc = {
        "dns_servers": ["127.0.0.11"],
        "dns_search": ["Lab.Example.Net."],
        "routes": [{"to": "10.50.0.0/16", "via": "172.30.0.254"}],
    }
    r = rows(doc)
    assert {k: v.status for k, v in r.items()} == {
        ("gateway", "IPv4"): "match",
        ("dns_servers", "dns_servers"): "match",
        ("dns_search", "dns_search"): "match",
        ("route", "10.50.0.0/16"): "match",  # the DHCP route to 10.60.0.0/16 is not a static route
    }
    gw = r[("gateway", "IPv4")]
    assert (gw.server, gw.documented, gw.prefix, gw.prefix_id, gw.interface) == (
        "172.30.0.1",
        "172.30.0.1",
        "172.30.0.0/24",
        7,
        "eth0",
    )


def test_what_differs() -> None:
    doc = {
        "dns_servers": ["10.0.0.53", "127.0.0.11"],
        "routes": [
            {"to": "10.50.1.0/16", "via": "172.30.0.253"},
            {"to": "192.0.2.0/24", "via": "172.30.0.254"},
        ],
    }
    other = {"172.30.0.1": DocumentedPrefix(prefix="172.30.0.0/24", prefix_id=7, gateway="172.30.0.2")}
    r = rows(doc, other)
    assert r[("gateway", "IPv4")].status == "differs" and r[("gateway", "IPv4")].documented == "172.30.0.2"
    assert r[("dns_servers", "dns_servers")].status == "differs"  # order counts: the first is asked first
    assert r[("dns_servers", "dns_servers")].documented == "10.0.0.53, 127.0.0.11"
    assert r[("dns_search", "dns_search")].status == "only_on_server"  # not documented
    assert r[("route", "10.50.0.0/16")].status == "differs"  # host bits in the documentation do not matter
    assert (r[("route", "10.50.0.0/16")].server, r[("route", "10.50.0.0/16")].documented) == (
        "172.30.0.254",
        "172.30.0.253",
    )
    assert r[("route", "10.50.0.0/16")].id == "route:10.50.0.0/16"
    assert r[("route", "192.0.2.0/24")].status == "only_documented"


def test_nothing_documented() -> None:
    no_gateway = {"172.30.0.1": DocumentedPrefix(prefix="172.30.0.0/24", prefix_id=7, gateway=None)}
    r = rows(None, no_gateway)
    assert {k: v.status for k, v in r.items()} == {
        ("gateway", "IPv4"): "only_on_server",
        ("dns_servers", "dns_servers"): "only_on_server",
        ("dns_search", "dns_search"): "only_on_server",
        ("route", "10.50.0.0/16"): "only_on_server",
    }
    assert ("gateway", "IPv4") not in rows(None, {})  # its next hop is in no documented prefix


def test_documented_settings_from_the_config_context() -> None:
    assert documented({"ntp": ["x"], "lanvex": {"dns_servers": ["1.1.1.1"]}}) == {"dns_servers": ["1.1.1.1"]}
    assert documented({"lanvex": "nonsense"}) is None and documented(None) is None


def test_alerts_name_what_contradicts_the_documentation() -> None:
    from lanvex.monitoring.checks import drift
    from lanvex.monitoring.schemas import MonitoringSettings
    from lanvex.servers.schemas import Comparison

    doc = {"dns_servers": ["10.0.0.53"], "routes": [{"to": "192.0.2.0/24", "via": "172.30.0.254"}]}
    other = {"172.30.0.1": DocumentedPrefix(prefix="172.30.0.0/24", prefix_id=7, gateway="172.30.0.2")}
    comparison = Comparison(
        linked=True,
        netbox=None,
        suggestions=[],
        snapshot_taken_at=None,
        rows=[],
        counts={},
        settings=compare(facts(), doc, other),
        documented_settings=doc,
    )
    [finding] = drift("web-01", comparison, None, MonitoringSettings())
    assert finding.detail == [
        "DNS servers: 127.0.0.11, documented 10.0.0.53",
        "Default gateway 172.30.0.1 (eth0): documented gateway of 172.30.0.0/24 is 172.30.0.2",
        "Route to 10.50.0.0/16 via 172.30.0.254: on the server, not documented",
        "Route to 192.0.2.0/24 via 172.30.0.254: documented, not on the server",
    ]  # the search domains are documented nowhere: no alert
    assert drift("web-01", comparison, None, MonitoringSettings(drift_ignore_interfaces=["eth0"]))[
        0
    ].detail == [
        "DNS servers: 127.0.0.11, documented 10.0.0.53",
        "Route to 192.0.2.0/24 via 172.30.0.254: documented, not on the server",
    ]
