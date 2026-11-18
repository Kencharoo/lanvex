#!/usr/bin/python3 -I
"""lanvex-netapply: applies a Lanvex network change on this server, with automatic rollback.

An administrator installs this file as /usr/local/libexec/lanvex/lanvex-netapply (root:root, 0755) and
allows the Lanvex SSH user to run it with sudo. Lanvex sends one JSON request on stdin and reads one JSON
result from stdout.

What it does, and nothing else:
  version   report the helper version and which network tools are present
  plan      show the exact change, without changing anything. Two kinds:
              interface_ipv4  one interface's IPv4 addresses, gateway and DNS, and optionally its IPv6
                              addresses and gateway and its static routes
              networks        the VLAN interfaces Lanvex manages, their firewalld zones, and the DHCP
                              service for them (dnsmasq), all in one change
              link            create one bridge or bond from free interfaces, or remove one Lanvex
                              created
  apply     back up the affected configuration, arm a rollback timer, then make the change
  verify    check the result (addresses, default route, gateway reachable, DNS)
  confirm   keep the change: stop the rollback timer
  rollback  restore the backed-up configuration now
  status    report what happened to a change

Safety rules:
  - It never runs a shell. Every program is called with a fixed argument list.
  - It never takes file paths, program names or option names from its input. It edits only the netplan
    file or NetworkManager profile that already defines the interface, and files of its own:
    NetworkManager profiles named lanvex-*, firewalld zones named lx-*, and /etc/dnsmasq.d/lanvex.conf.
    Another tool's VLAN, zone or dnsmasq setting for the same interface is reported, never changed.
  - The rollback timer is armed before anything is changed. If Lanvex does not confirm in time, systemd
    runs `timer-rollback` and the previous configuration comes back on its own.
  - One change at a time; a second one is refused while another waits for confirmation.

Runs on Python 3.8 or newer (Ubuntu 20.04, Debian 11, Fedora 38). netplan changes need PyYAML, which
netplan itself depends on.
"""

from __future__ import annotations

import copy
import fcntl
import fnmatch
import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HELPER_VERSION = 6
STATE_DIR = "/var/lib/lanvex/changes"
LOCK_FILE = "/run/lanvex-netapply.lock"
NETPLAN_DIR = "/etc/netplan"
NM_KEYFILE_DIR = "/etc/NetworkManager/system-connections/"
NM_NETPLAN_PREFIX = "/run/NetworkManager/system-connections/netplan-"
LANVEX_PROFILE = "lanvex-"  # NetworkManager profiles Lanvex owns: lanvex-<interface>
ZONE_PREFIX = "lx-"  # firewalld zones Lanvex owns: lx-<interface>; firewalld allows 17 characters
ZONE_DIR = "/etc/firewalld/zones"
POLICY_DIR = "/etc/firewalld/policies"
POLICY_PREFIX = "lxf-"  # firewalld policies Lanvex owns: lxf-<interface>-<zone hash>, 17 characters at most
# Without firewalld: Lanvex's own nftables table, loaded at boot by its own unit.
NFT_FILE = "/etc/lanvex/firewall.nft"
NFT_TABLE = ("inet", "lanvex")
NFT_UNIT_NAME = "lanvex-firewall.service"
NFT_UNIT = "/etc/systemd/system/" + NFT_UNIT_NAME
SYSCTL_FILE = (
    "/etc/sysctl.d/60-lanvex.conf"  # IP forwarding, while a Lanvex network may reach beyond the server
)
FORWARD_KEY = "/proc/sys/net/ipv4/ip_forward"
# Never reached through "the Internet": private, shared (CGNAT) and link-local addresses.
PRIVATE4 = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16")
PORT_RE = re.compile(r"^([0-9]{1,5})(?:-([0-9]{1,5}))?/(tcp|udp)$")
# DNS filter: one dnsmasq per filtered network, on an address of Lanvex's dummy link, handing every
# name it does not block to the main dnsmasq. Clients get that address as their DNS server by DHCP.
DNS_DIR = "/etc/lanvex/dns"
DNS_UNIT_NAME = "lanvex-dns@.service"
DNS_UNIT = "/etc/systemd/system/" + DNS_UNIT_NAME
DNS_LINK = "lxdns"
DNS_NET = "10.99.53.0/24"
# Taking over from Netzverwaltung (the tool Lanvex replaces on KlassenServer): what it keeps where.
NV_STATE = "/etc/netzverwaltung/netz.yaml"
NV_PREFIX = "nv-"
NV_PROFILE = "vlan-"  # its NetworkManager profiles: vlan-<name>
NV_UNITS = ("netzverwaltung.service", "dnsmasq-filter.service")
FILTER_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9])?\.)*[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"
)
DNSMASQ_CONF = "/etc/dnsmasq.conf"
DNSMASQ_DIR = "/etc/dnsmasq.d"
DNSMASQ_FILE = "/etc/dnsmasq.d/lanvex.conf"
# Kea instead of dnsmasq, on servers that run it: Lanvex writes its networks to a file of its own, which
# kea-dhcp4.conf includes as its shared networks (set up once by an administrator, see docs/networks.md).
KEA_CONF = "/etc/kea/kea-dhcp4.conf"
KEA_FILE = "/etc/kea/lanvex-shared-networks.json"
KEA_INCLUDE_RE = re.compile(
    r"<\?include\s+\"" + re.escape("/etc/kea/lanvex-shared-networks.json") + r"\"\s*\?>"
)
KEA_UNITS = ("kea-dhcp4.service", "kea-dhcp4-server.service", "isc-kea-dhcp4-server.service")
KEA_SOCKETS = ("/run/kea/kea4-ctrl-socket", "/var/run/kea/kea4-ctrl-socket", "/tmp/kea4-ctrl-socket")
NETPLAN_LANVEX = "/etc/netplan/60-lanvex.yaml"  # the VLANs Lanvex manages on a netplan server
IFUPDOWN_MAIN = "/etc/network/interfaces"
IFUPDOWN_DIR = "/etc/network/interfaces.d"
IFUPDOWN_LANVEX = "/etc/network/interfaces.d/lanvex"  # the VLANs Lanvex manages on an ifupdown server
# Which VLAN interfaces Lanvex manages here, readable without root (netplan files are not): Lanvex's
# server reads it to tell its own VLANs from others. Written as part of each change, so a rollback
# restores it with everything else.
MANIFEST = "/etc/lanvex/networks.json"
# Bridges and bonds Lanvex created (kind "link"): their own files, listed here so Lanvex can tell them
# apart and remove them again. NetworkManager profiles lxlink-<name> and lxlink-<name>-<port>, netplan
# files 61-lxlink-<name>.yaml; never lanvex-*, which are the VLAN profiles of the networks kind.
LINKS_MANIFEST = "/etc/lanvex/links.json"
LINK_PROFILE = "lxlink-"
BOND_MODES = (
    "active-backup",
    "802.3ad",
    "balance-rr",
    "balance-xor",
    "balance-tlb",
    "balance-alb",
    "broadcast",
)
UFW_MARK = "lanvex:"  # the comment on every ufw rule Lanvex adds, so it can find and remove its own
UFW_FILES = ("/etc/ufw/user.rules", "/etc/ufw/user6.rules")
MANAGED = "Managed by Lanvex. Edits here are replaced by the next Lanvex change."
ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "LANG": "C"}
KEEP_CHANGES = 20
NETPLAN_SECTIONS = (
    "ethernets",
    "bonds",
    "bridges",
    "vlans",
    "wifis",
    "tunnels",
    "vrfs",
    "dummy-devices",
    "virtual-ethernets",
)
ID_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_ROUTES = 64
IPV6_METHODS = ("auto", "static", "link-local")
NM_IPV6_METHOD = {"auto": "auto", "static": "manual", "link-local": "link-local"}
MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
LEASE_RE = re.compile(r"^([1-9][0-9]{0,4}[mhd]|infinite)$")
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
IFACE_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,15}$")
DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)([A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9])?)*\.?$"
)


# Values never shown to Lanvex: Wi-Fi keys, 802.1X and VPN passwords and the like. The preview shows
# "[redacted]" instead; the checksum Lanvex uses to detect later edits still covers the real file.
SECRET_LINE = re.compile(
    r"^(?P<lead>\s*(?:-\s+)?[\"']?)"
    r"(?P<key>[\w.-]*(?:password|passphrase|psk|secret)[\w.-]*|pin|leap-password|wep-key\d)"
    r"(?P<sep>[\"']?\s*[:=]\s*)(?P<value>\S.*)$",
    re.IGNORECASE,
)


def redact(text: str) -> str:
    out = []
    for line in text.split("\n"):
        m = SECRET_LINE.match(line)
        out.append("%s%s%s[redacted]" % (m.group("lead"), m.group("key"), m.group("sep")) if m else line)
    return "\n".join(out)


class Refused(Exception):
    """A handled error: reported to Lanvex as {"ok": false, "error": {...}}."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------------------------------------
# Small utilities


def run(argv: list, timeout: float = 60, check: bool = False) -> subprocess.CompletedProcess:
    """Run a program with a fixed argument list. Never through a shell."""
    try:
        r = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv, env=ENV, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError as e:
        raise Refused("missing_tool", "%s is not installed." % argv[0]) from e
    except subprocess.TimeoutExpired as e:
        raise Refused(
            "timeout", "%s did not finish within %ss.%s" % (" ".join(argv[:3]), timeout, _unit_log(argv))
        ) from e
    if check and r.returncode != 0:
        raise Refused(
            "command_failed",
            "%s failed: %s%s" % (" ".join(argv[:3]), (r.stderr or r.stdout).strip()[:500], _unit_log(argv)),
        )
    return r


def _unit_log(argv: list) -> str:
    """For a service that did not start: its last log lines, which say why."""
    if len(argv) < 3 or argv[0] != "systemctl" or argv[1] not in ("start", "restart", "reload"):
        return ""
    try:
        r = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["journalctl", "-u", argv[-1], "-n", "40", "-o", "cat", "--no-pager"],
            env=ENV,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    # The service's own words, not systemd's account of starting and stopping it.
    chatter = (
        "Starting ",
        "Started ",
        "Stopping ",
        "Stopped ",
        "Deactivated",
        "Consumed ",
        "Failed to start",
        "Control process",
        "Failed with result",
        "Main process exited",
        "Scheduled restart",
    )
    lines = [x for x in r.stdout.splitlines() if x.strip() and not any(c in x for c in chatter)][-6:]
    return (" Its log: " + " | ".join(lines))[:1200] if lines else ""


def which(name: str) -> str | None:
    return shutil.which(name, path=ENV["PATH"])


def sha256_file(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        return None


def read_text(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def atomic_write(path: str, data: bytes, mode: int, uid: int = 0, gid: int = 0) -> None:
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".lanvex-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        try:
            os.chown(tmp, uid, gid)
        except PermissionError:
            pass
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def now() -> float:
    return time.time()


# ---------------------------------------------------------------------------------------------------------
# Request validation. Everything from Lanvex is checked here again, whatever Lanvex already checked.


def check_id(value: object) -> str:
    if not isinstance(value, str) or not ID_RE.match(value):
        raise Refused("invalid_request", "Change id must be 32 lowercase hex characters.")
    return value


def check_interface(value: object) -> str:
    if not isinstance(value, str) or not IFACE_RE.match(value) or value in (".", ".."):
        raise Refused("invalid_request", "Invalid interface name.")
    if value == "lo":
        raise Refused("invalid_request", "The loopback interface cannot be changed.")
    return value


def check_ipv4_address(text: object, field: str) -> ipaddress.IPv4Interface:
    if not isinstance(text, str):
        raise Refused("invalid_request", "%s: expected an address with prefix length." % field)
    try:
        a = ipaddress.ip_interface(text)
    except ValueError as e:
        raise Refused(
            "invalid_request", "%s: %s is not an address with prefix length." % (field, text)
        ) from e
    if not isinstance(a, ipaddress.IPv4Interface):
        raise Refused("invalid_request", "%s: only IPv4 addresses are supported." % field)
    ip, net = a.ip, a.network
    if "/" not in text or net.prefixlen == 0:
        raise Refused("invalid_request", "%s: %s needs a prefix length between 1 and 32." % (field, text))
    if ip.is_loopback or ip.is_multicast or ip.is_link_local or ip.is_unspecified or ip.is_reserved:
        raise Refused("invalid_request", "%s: %s cannot be assigned to an interface." % (field, text))
    if net.prefixlen < 31 and ip in (net.network_address, net.broadcast_address):
        raise Refused(
            "invalid_request", "%s: %s is the network or broadcast address of %s." % (field, ip, net)
        )
    return a


def check_ipv4(text: object, field: str) -> ipaddress.IPv4Address:
    try:
        ip = ipaddress.ip_address(text)  # type: ignore[arg-type]
    except (ValueError, TypeError) as e:
        raise Refused("invalid_request", "%s: %r is not an IPv4 address." % (field, text)) from e
    if not isinstance(ip, ipaddress.IPv4Address) or ip.is_unspecified or ip.is_multicast:
        raise Refused("invalid_request", "%s: %s is not a usable IPv4 address." % (field, ip))
    return ip


def check_ipv6_address(text: object, field: str) -> ipaddress.IPv6Interface:
    if not isinstance(text, str) or "/" not in text:
        raise Refused("invalid_request", "%s: expected an IPv6 address with prefix length." % field)
    try:
        a = ipaddress.ip_interface(text)
    except ValueError as e:
        raise Refused(
            "invalid_request", "%s: %s is not an address with prefix length." % (field, text)
        ) from e
    if not isinstance(a, ipaddress.IPv6Interface) or a.network.prefixlen == 0:
        raise Refused("invalid_request", "%s: %s is not an IPv6 address with prefix length." % (field, text))
    ip = a.ip
    if ip.is_loopback or ip.is_multicast or ip.is_link_local or ip.is_unspecified or ip.ipv4_mapped:
        raise Refused("invalid_request", "%s: %s cannot be assigned to an interface." % (field, text))
    return a


def check_ip(text: object, field: str):
    """A unicast address of either family (a gateway or next hop)."""
    try:
        ip = ipaddress.ip_address(text)  # type: ignore[arg-type]
    except (ValueError, TypeError) as e:
        raise Refused("invalid_request", "%s: %r is not an IP address." % (field, text)) from e
    if ip.is_unspecified or ip.is_multicast or ip.is_loopback:
        raise Refused("invalid_request", "%s: %s is not a usable address." % (field, ip))
    return ip


def validate_ipv6(raw: object) -> dict | None:
    """The interface's IPv6 settings, or None to leave them as they are."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or raw.get("method") not in IPV6_METHODS:
        raise Refused("invalid_request", "ipv6.method must be one of %s." % ", ".join(IPV6_METHODS))
    method = raw["method"]
    raw_addresses = raw.get("addresses") or []
    if not isinstance(raw_addresses, list) or len(raw_addresses) > 32:
        raise Refused("invalid_request", "ipv6.addresses must be a list of at most 32 addresses.")
    addresses = [check_ipv6_address(a, "ipv6.addresses") for a in raw_addresses]
    if len({a.ip for a in addresses}) != len(addresses):
        raise Refused("invalid_request", "ipv6.addresses lists the same address twice.")
    gateway = raw.get("gateway")
    gw = check_ip(gateway, "ipv6.gateway") if gateway not in (None, "") else None
    if gw is not None and not isinstance(gw, ipaddress.IPv6Address):
        raise Refused("invalid_request", "ipv6.gateway: %s is not an IPv6 address." % gw)
    if method == "static":
        if not addresses:
            raise Refused("invalid_request", "A static IPv6 configuration needs at least one address.")
        if gw is not None and not gw.is_link_local and not any(gw in a.network for a in addresses):
            raise Refused(
                "invalid_request",
                "The IPv6 gateway %s is neither link-local nor inside one of the interface's networks." % gw,
            )
        if gw is not None and any(gw == a.ip for a in addresses):
            raise Refused(
                "invalid_request", "The IPv6 gateway cannot be one of the interface's own addresses."
            )
    elif addresses or gw is not None:
        raise Refused("invalid_request", "IPv6 addresses and gateway can only be set with the static method.")
    return {
        "method": method,
        "addresses": [str(a) for a in addresses],
        "gateway": str(gw) if gw else None,
    }


def validate_routes(raw: object, ipv4: dict, ipv6: dict | None) -> list | None:
    """Static routes through a next hop, or None to leave the interface's routes as they are.

    Default routes are not among them: they are the gateways. A next hop has to be on the link: inside a
    static network of the interface, or link-local for IPv6; with DHCP or router advertisements the
    networks are not known here, and netplan or NetworkManager refuses an unreachable one.
    """
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) > MAX_ROUTES:
        raise Refused("invalid_request", "routes must be a list of at most %d routes." % MAX_ROUTES)
    nets = {
        4: [ipaddress.ip_interface(a).network for a in ipv4["addresses"]]
        if ipv4["method"] == "static"
        else None,
        6: (
            [ipaddress.ip_interface(a).network for a in ipv6["addresses"]]
            if ipv6 is not None and ipv6["method"] == "static"
            else ([] if ipv6 is not None and ipv6["method"] == "link-local" else None)
        ),
    }
    out, seen = [], set()
    for r in raw:
        if not isinstance(r, dict):
            raise Refused("invalid_request", "Each route must be an object with to and via.")
        try:
            to = ipaddress.ip_network(r.get("to"))  # type: ignore[arg-type]
        except (ValueError, TypeError) as e:
            raise Refused(
                "invalid_request",
                "routes: %r is not a network with prefix length, e.g. 10.20.0.0/16." % r.get("to"),
            ) from e
        if to.prefixlen == 0:
            raise Refused(
                "invalid_request", "routes: the default route is set as the gateway, not as a route."
            )
        if to.is_multicast or to.is_loopback or (to.version == 6 and to.is_link_local):
            raise Refused("invalid_request", "routes: %s cannot be routed." % to)
        via = check_ip(r.get("via"), "routes.via")
        if via.version != to.version:
            raise Refused(
                "invalid_request", "routes: %s and its next hop %s are not the same family." % (to, via)
            )
        on_link = nets[to.version]
        if (
            on_link is not None
            and not (via.version == 6 and via.is_link_local)
            and not any(via in n for n in on_link)
        ):
            raise Refused(
                "invalid_request",
                "routes: the next hop %s for %s is not on any of the interface's networks." % (via, to),
            )
        metric = r.get("metric")
        if metric is not None and (
            not isinstance(metric, int) or isinstance(metric, bool) or not 0 <= metric < 2**32
        ):
            raise Refused("invalid_request", "routes: the metric of %s must be a whole number." % to)
        if to in seen:
            raise Refused("invalid_request", "routes: %s is listed twice." % to)
        seen.add(to)
        out.append({"to": str(to), "via": str(via), "metric": metric})
    return out


def check_domain(text: object, field: str) -> str:
    if not isinstance(text, str) or not DOMAIN_RE.match(text):
        raise Refused("invalid_request", "%s: %r is not a valid domain name." % (field, text))
    return text.rstrip(".")


def validate_change(req: dict) -> dict:
    """Normalise the requested IPv4 settings for one interface."""
    if not isinstance(req, dict):
        raise Refused("invalid_request", "Request must be a JSON object.")
    iface = check_interface(req.get("interface"))
    ipv4 = req.get("ipv4")
    if not isinstance(ipv4, dict) or ipv4.get("method") not in ("static", "dhcp"):
        raise Refused("invalid_request", "ipv4.method must be 'static' or 'dhcp'.")
    method = ipv4["method"]
    raw_addresses = ipv4.get("addresses") or []
    if not isinstance(raw_addresses, list) or len(raw_addresses) > 32:
        raise Refused("invalid_request", "ipv4.addresses must be a list of at most 32 addresses.")
    addresses = [check_ipv4_address(a, "ipv4.addresses") for a in raw_addresses]
    if len({str(a.ip) for a in addresses}) != len(addresses):
        raise Refused("invalid_request", "ipv4.addresses lists the same address twice.")
    gateway = ipv4.get("gateway")
    gw = check_ipv4(gateway, "ipv4.gateway") if gateway not in (None, "") else None
    if method == "static":
        if not addresses:
            raise Refused("invalid_request", "A static configuration needs at least one address.")
        if gw is not None and not any(gw in a.network for a in addresses):
            raise Refused(
                "invalid_request", "The gateway %s is not inside any of the interface's networks." % gw
            )
        if gw is not None and any(gw == a.ip for a in addresses):
            raise Refused("invalid_request", "The gateway cannot be one of the interface's own addresses.")
    elif addresses or gw is not None:
        raise Refused("invalid_request", "With DHCP, addresses and gateway come from the DHCP server.")
    dns = req.get("dns")
    if dns is None:
        servers = search = None  # keep the interface's DNS settings exactly as they are
    else:
        if not isinstance(dns, dict):
            raise Refused("invalid_request", "dns must be an object or null.")
        servers = [str(check_ipv4(s, "dns.servers")) for s in (dns.get("servers") or [])]
        search = [check_domain(d, "dns.search") for d in (dns.get("search") or [])]
        if len(servers) > 8 or len(search) > 16:
            raise Refused("invalid_request", "Too many DNS servers or search domains.")
    ipv6 = validate_ipv6(req.get("ipv6"))
    routes = validate_routes(
        req.get("routes"), {"method": method, "addresses": [str(a) for a in addresses]}, ipv6
    )
    return {
        "interface": iface,
        "method": method,
        "addresses": [str(a) for a in addresses],
        "gateway": str(gw) if gw else None,
        "dns_servers": servers,
        "dns_search": search,
        "ipv6": ipv6,
        "routes": routes,
    }


# ---------------------------------------------------------------------------------------------------------
# netplan


def _family(entry: object) -> int | None:
    """4 or 6 for a netplan address entry, which is either "a/len" or {"a/len": {options}}."""
    key = next(iter(entry)) if isinstance(entry, dict) and entry else entry
    try:
        return ipaddress.ip_interface(str(key)).version
    except ValueError:
        return None


def _is_v4(entry: object) -> bool:
    return _family(entry) == 4


def _is_default_route(route: object, family: int = 4) -> bool:
    if not isinstance(route, dict) or str(route.get("to")) not in ("default", "0.0.0.0/0", "::/0"):
        return False
    try:
        return ipaddress.ip_address(str(route.get("via"))).version == family
    except ValueError:
        return False


def _is_v4_default_route(route: object) -> bool:
    return _is_default_route(route, 4)


def _is_static_route(route: object) -> bool:
    """A route Lanvex manages as one of the interface's static routes: through a next hop, in the main
    table, not a default route. Routes in other tables, without a next hop or of other types stay."""
    if (
        not isinstance(route, dict)
        or "via" not in route
        or str(route.get("to")) in ("default", "0.0.0.0/0", "::/0")
    ):
        return False
    return route.get("table") in (None, 254, "main") and route.get("type") in (None, "unicast")


def netplan_route(r: dict) -> dict:
    out = {"to": r["to"], "via": r["via"]}
    if r.get("metric") is not None:
        out["metric"] = r["metric"]
    return out


def route_key(r: dict) -> tuple:
    """A route as (destination, next hop), whichever way it was written."""
    try:
        return (
            str(ipaddress.ip_network(str(r.get("to")), strict=False)),
            str(ipaddress.ip_address(str(r.get("via")))),
        )
    except ValueError:
        return (str(r.get("to")), str(r.get("via")))


def iface_facts(iface: str) -> dict:
    """MAC address and driver, for evaluating netplan match rules."""
    facts = {"mac": None, "driver": None}
    try:
        facts["mac"] = read_text("/sys/class/net/%s/address" % iface).strip().lower()
    except OSError:
        pass
    driver = "/sys/class/net/%s/device/driver" % iface
    if os.path.islink(driver):
        facts["driver"] = os.path.basename(os.readlink(driver))
    return facts


def match_covers(match: dict, iface: str, facts: dict) -> str:
    """'no' if the rule cannot select iface, 'exact' if it selects exactly iface, 'broad' otherwise."""
    if not isinstance(match, dict) or not match or set(match) - {"name", "macaddress", "driver"}:
        return "broad"
    name, mac, driver = match.get("name"), match.get("macaddress"), match.get("driver")
    if name is not None and not fnmatch.fnmatchcase(iface, str(name)):
        return "no"
    if mac is not None and str(mac).lower() != (facts.get("mac") or ""):
        return "no"
    if driver is not None:
        drivers = driver if isinstance(driver, list) else [driver]
        if not facts.get("driver") or not any(fnmatch.fnmatchcase(facts["driver"], str(d)) for d in drivers):
            return "no"
    exact_name = name is not None and not any(c in str(name) for c in "*?[")
    return "exact" if (exact_name or mac is not None) else "broad"


def netplan_definitions(iface: str, docs: dict, facts: dict | None = None) -> list:
    """Where `iface` is defined: [(path, section, key)].

    A definition is an entry named after the interface, one that renames a device to it (set-name), or
    one whose match rule selects exactly this interface (an exact name or its MAC address; NetworkManager
    writes its profiles this way). A rule that could select several interfaces is refused.
    """
    facts = facts if facts is not None else iface_facts(iface)
    found = []
    for path, doc in sorted(docs.items()):
        network = (doc or {}).get("network") if isinstance(doc, dict) else None
        if not isinstance(network, dict):
            continue
        for section in NETPLAN_SECTIONS:
            entries = network.get(section)
            if not isinstance(entries, dict):
                continue
            for key, value in entries.items():
                value = value if isinstance(value, dict) else {}
                if key == iface or value.get("set-name") == iface:
                    found.append((path, section, key))
                elif "match" in value and "set-name" not in value:
                    covers = match_covers(value.get("match"), iface, facts)
                    if covers == "exact":
                        found.append((path, section, key))
                    elif covers == "broad":
                        raise Refused(
                            "unsupported",
                            "%s in %s selects interfaces with a rule that also covers %s. Lanvex only edits "
                            "interfaces defined on their own." % (key, os.path.basename(path), iface),
                        )
    return found


def edit_netplan_definition(defn: dict, change: dict) -> dict:
    """Return a copy of one netplan interface definition with the IPv4 settings replaced, and the IPv6
    settings and static routes when the change sets them (not None).

    Whatever the change does not set is kept as it is: IPv6 addresses and routes, other routes, every
    other key.
    """
    d = copy.deepcopy(defn) if isinstance(defn, dict) else {}
    ipv6, static_routes = change.get("ipv6"), change.get("routes")
    d.pop("gateway4", None)
    if ipv6 is not None:
        d.pop("gateway6", None)
    nm = d.get("networkmanager")
    passthrough = nm.get("passthrough") if isinstance(nm, dict) else None
    if isinstance(passthrough, dict):
        keys = ["ipv4.method", "ipv4.addresses", "ipv4.gateway"]
        if change["dns_servers"] is not None:
            keys += ["ipv4.dns", "ipv4.dns-search"]
        if ipv6 is not None:
            keys += ["ipv6.method", "ipv6.addresses", "ipv6.gateway"]
        if static_routes is not None:
            keys += ["ipv4.routes", "ipv6.routes"]
        for k in keys:
            passthrough.pop(k, None)
        if change["method"] == "static" and change["gateway"]:
            passthrough.pop("ipv4.never-default", None)
        if ipv6 is not None and ipv6["gateway"]:
            passthrough.pop("ipv6.never-default", None)
    replaced = {4} | ({6} if ipv6 is not None else set())
    kept_addresses = [a for a in (d.get("addresses") or []) if _family(a) not in replaced]
    routes = [r for r in (d.get("routes") or []) if not any(_is_default_route(r, f) for f in replaced)]
    if static_routes is not None:
        routes = [r for r in routes if not _is_static_route(r)] + [netplan_route(r) for r in static_routes]
    new_addresses = list(change["addresses"]) if change["method"] == "static" else []
    if change["method"] == "static":
        if d.get("dhcp4"):
            d["dhcp4"] = False
        if change["gateway"]:
            routes.append({"to": "default", "via": change["gateway"]})
    else:
        d["dhcp4"] = True
    if ipv6 is not None:
        new_addresses += ipv6["addresses"]
        if ipv6["method"] == "auto":
            d["dhcp6"], d["accept-ra"] = True, True
        else:
            if "dhcp6" in d:
                d["dhcp6"] = False
            d["accept-ra"] = False
        if ipv6["gateway"]:
            routes.append({"to": "default", "via": ipv6["gateway"]})
    addresses = new_addresses + kept_addresses
    if addresses:
        d["addresses"] = addresses
    else:
        d.pop("addresses", None)
    if routes:
        d["routes"] = routes
    else:
        d.pop("routes", None)

    if change["dns_servers"] is None:
        return d
    ns = dict(d.get("nameservers") or {})
    kept_servers = [
        s for s in (ns.get("addresses") or []) if not isinstance(_ip_or_none(s), ipaddress.IPv4Address)
    ]
    servers = list(change["dns_servers"]) + kept_servers
    if servers:
        ns["addresses"] = servers
    else:
        ns.pop("addresses", None)
    if change["dns_search"]:
        ns["search"] = list(change["dns_search"])
    else:
        ns.pop("search", None)
    if ns:
        d["nameservers"] = ns
    else:
        d.pop("nameservers", None)
    return d


def _ip_or_none(value: object):
    try:
        return ipaddress.ip_address(str(value))
    except ValueError:
        return None


def _yaml():
    try:
        import yaml  # noqa: PLC0415 - only needed on netplan systems
    except ImportError as e:
        raise Refused("missing_tool", "PyYAML (python3-yaml) is not installed.") from e
    return yaml


def dump_netplan(doc: dict) -> str:
    return _yaml().safe_dump(doc, default_flow_style=False, sort_keys=False, allow_unicode=True)


def _block_dumper():
    """Mappings in block style, lists of plain values inline: the way netplan files are usually written."""
    yaml = _yaml()

    class Dumper(yaml.SafeDumper):
        pass

    def rep_list(dumper, data):
        inline = all(not isinstance(x, (dict, list)) for x in data)
        return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=inline)

    def rep_dict(dumper, data):
        return dumper.represent_mapping("tag:yaml.org,2002:map", data, flow_style=False)

    Dumper.add_representer(list, rep_list)
    Dumper.add_representer(dict, rep_dict)
    return Dumper


def dump_value(value: dict) -> str:
    return _yaml().dump(value, Dumper=_block_dumper(), sort_keys=False, width=4096, allow_unicode=True)


def splice_definition(text: str, keys: tuple, old_defn: dict, new_defn: dict) -> str:
    """Replace only the YAML block that defines one interface, leaving the rest of the file as it is.

    `keys` is ("network", section, name). Lines between this block and the next key (blank lines,
    comments, the next key's indentation) are kept. Raises ValueError when the layout is not one it
    can splice safely; the caller then rewrites the whole file instead.
    """
    yaml = _yaml()
    node = yaml.compose(text)
    for k in keys:
        if not isinstance(node, yaml.MappingNode):
            raise ValueError("not a mapping")
        node = next(v for kn, v in node.value if getattr(kn, "value", None) == k)
    if not isinstance(node, yaml.MappingNode) or not node.value:
        raise ValueError("definition is not a non-empty mapping")
    start, end = node.start_mark, node.end_mark
    if node.flow_style:
        block = yaml.safe_dump(new_defn, default_flow_style=True, sort_keys=False, width=4096).strip()
        return text[: start.index] + block + text[end.index :]
    # Keys whose value did not change keep their original text, so the preview shows only real changes.
    old_values = old_defn if isinstance(old_defn, dict) else {}
    originals = {}
    for kn, vn in node.value:
        pair = text[kn.start_mark.index : vn.end_mark.index].split("\n")
        while len(pair) > 1 and (not pair[-1].strip() or pair[-1].lstrip().startswith("#")):
            pair.pop()
        originals[kn.value] = "\n".join(pair)
    indent = "\n" + " " * start.column
    parts = []
    for k, v in new_defn.items():
        if k in originals and k in old_values and old_values[k] == v:
            parts.append(originals[k])
        else:
            dumped = dump_value({k: v})
            parts.append(indent.join(dumped.rstrip("\n").split("\n")))
    body = indent.join(parts)
    old = text[start.index : end.index].split("\n")
    if len(old) == 1:  # block runs to the end of a file without a final newline
        return text[: start.index] + body + text[end.index :]
    tail = [old.pop()]  # the next key's indentation, or "" at the end of the file
    while len(old) > 1 and (not old[-1].strip() or old[-1].lstrip().startswith("#")):
        tail.insert(0, old.pop())
    return text[: start.index] + body + "\n" + "\n".join(tail) + text[end.index :]


def render_netplan(text: str, keys: tuple, old_defn: dict, new_defn: dict, expected: dict) -> tuple:
    """(new file text, whether the whole file had to be rewritten)."""
    yaml = _yaml()
    try:
        spliced = splice_definition(text, keys, old_defn, new_defn)
        if yaml.safe_load(spliced) == expected:
            return spliced, False
    except (ValueError, StopIteration, yaml.YAMLError):
        pass
    return dump_netplan(expected), True


def netplan_plan(iface: str, change: dict) -> dict:
    if not which("netplan"):
        raise Refused("unsupported", "netplan is not installed.")
    yaml = _yaml()
    files = (
        sorted(os.path.join(NETPLAN_DIR, f) for f in os.listdir(NETPLAN_DIR) if f.endswith(".yaml"))
        if os.path.isdir(NETPLAN_DIR)
        else []
    )
    docs = {}
    for path in files:
        try:
            docs[path] = yaml.safe_load(read_text(path))
        except yaml.YAMLError as e:
            raise Refused("unsupported", "%s is not valid YAML: %s" % (path, e)) from e
    found = netplan_definitions(iface, docs)
    if not found:
        raise Refused(
            "unsupported",
            "%s is not defined in %s. Lanvex only changes interfaces that netplan already manages."
            % (iface, NETPLAN_DIR),
        )
    if len(found) > 1:
        raise Refused(
            "unsupported",
            "%s is defined in more than one netplan file (%s). Merge them into one file first."
            % (iface, ", ".join(sorted({os.path.basename(p) for p, _, _ in found}))),
        )
    path, section, key = found[0]
    doc = copy.deepcopy(docs[path])
    old = doc["network"][section][key]
    new = edit_netplan_definition(old, change)
    doc["network"][section][key] = new
    before = read_text(path)
    after, rewritten = render_netplan(before, ("network", section, key), old, new, doc)
    warnings = []
    if rewritten and "#" in before:
        warnings.append("Comments in %s are not kept: the file is rewritten." % os.path.basename(path))
    elif not rewritten and len(re.findall(r"#", before)) != len(re.findall(r"#", after)):
        warnings.append("Comments inside the %s block are not kept." % key)
    if "cloud-init" in os.path.basename(path):
        warnings.append(
            "%s is written by cloud-init, which may overwrite it at boot unless its network configuration "
            "is disabled." % os.path.basename(path)
        )
    if section != "ethernets":
        warnings.append("%s is a %s entry; only its IP settings change." % (iface, section[:-1]))
    removed = []
    if change.get("routes") is not None:
        wanted = {route_key(r) for r in change["routes"]}
        removed = [
            {"to": k[0], "via": k[1]}
            for k in (route_key(r) for r in ((old or {}).get("routes") or []) if _is_static_route(r))
            if k not in wanted
        ]
    return {
        "backend": "netplan",
        "files": [
            {
                "path": path,
                "label": "netplan file",
                "before": before,
                "after": after,
                "sha256": sha256_file(path),
                "action": "modify",
            }
        ],
        "commands": ["netplan generate", "netplan apply"],
        "warnings": warnings,
        "_write": {path: after},
        "_changed": new != (old if isinstance(old, dict) else {}),
        "_removed_routes": removed,
    }


def netplan_validate(candidates: dict) -> None:
    """Run `netplan generate` against a copy of the configuration with the candidate files in place."""
    root = tempfile.mkdtemp(prefix="lanvex-netplan-")
    try:
        for src in ("/etc/netplan", "/lib/netplan", "/run/netplan", "/usr/lib/netplan"):
            if os.path.isdir(src):
                dst = os.path.join(root, src.lstrip("/"))
                os.makedirs(dst, exist_ok=True)
                for f in os.listdir(src):
                    if f.endswith(".yaml"):
                        shutil.copy2(os.path.join(src, f), dst)
        for path, text in candidates.items():
            dst = os.path.join(root, path.lstrip("/"))
            if text is None:  # the change deletes this file
                if os.path.exists(dst):
                    os.unlink(dst)
                continue
            with open(dst, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(dst, 0o600)
        r = run([which("netplan") or "netplan", "generate", "--root-dir", root], timeout=60)
        if r.returncode != 0:
            raise Refused(
                "rejected",
                "netplan rejected the new configuration: %s" % (r.stderr or r.stdout).strip()[:800],
            )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def netplan_apply() -> None:
    run([which("netplan") or "netplan", "generate"], timeout=60, check=True)
    run([which("netplan") or "netplan", "apply"], timeout=120, check=True)


# ---------------------------------------------------------------------------------------------------------
# NetworkManager


def nm_split(line: str) -> list:
    """Split one line of `nmcli -t` output on unescaped colons."""
    out, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def nm_running() -> bool:
    if not which("nmcli"):
        return False
    r = run(["nmcli", "-t", "-f", "RUNNING", "general"], timeout=15)
    return r.returncode == 0 and r.stdout.strip() == "running"


def nm_active_profile(iface: str) -> dict | None:
    r = run(
        ["nmcli", "-t", "-f", "UUID,DEVICE,FILENAME,NAME", "connection", "show", "--active"],
        timeout=15,
        check=True,
    )
    for line in r.stdout.splitlines():
        parts = nm_split(line)
        if len(parts) >= 4 and parts[1] == iface:
            return {"uuid": parts[0], "file": parts[2], "name": ":".join(parts[3:])}
    return None


NM_IPV4_KEYS = re.compile(r"^(method|address\d*|addresses|gateway)$")
NM_DNS_KEYS = re.compile(r"^(dns|dns-search)$")
NM_ROUTE_KEYS = re.compile(r"^(route\d+(_options)?|routes)$")


def _key(line: str) -> str | None:
    """The key of a keyfile line, or None for headers, comments and blank lines."""
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", ";", "[")) or "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip()


def nm_route_lines(routes: list, family: int) -> list:
    """NetworkManager keyfile routes: routeN=destination,next hop[,metric]."""
    out = []
    for r in (r for r in routes if ipaddress.ip_network(r["to"]).version == family):
        out.append(
            "route%d=%s,%s%s"
            % (len(out) + 1, r["to"], r["via"], "" if r.get("metric") is None else ",%d" % r["metric"])
        )
    return out


def nm_ipv4_lines(change: dict) -> list:
    lines = ["method=%s" % ("manual" if change["method"] == "static" else "auto")]
    for i, a in enumerate(change["addresses"], 1):
        lines.append("address%d=%s" % (i, a))
    if change["gateway"]:
        lines.append("gateway=%s" % change["gateway"])
    if change["dns_servers"]:
        lines.append("dns=%s;" % ";".join(change["dns_servers"]))
    if change["dns_search"]:
        lines.append("dns-search=%s;" % ";".join(change["dns_search"]))
    if change.get("routes") is not None:
        lines += nm_route_lines(change["routes"], 4)
    return lines  # with DNS unchanged (None), the existing dns lines stay where they are


def nm_ipv6_lines(change: dict) -> list:
    ipv6 = change.get("ipv6")
    lines = []
    if ipv6 is not None:
        lines.append("method=%s" % NM_IPV6_METHOD[ipv6["method"]])
        for i, a in enumerate(ipv6["addresses"], 1):
            lines.append("address%d=%s" % (i, a))
        if ipv6["gateway"]:
            lines.append("gateway=%s" % ipv6["gateway"])
    if change.get("routes") is not None:
        lines += nm_route_lines(change["routes"], 6)
    return lines


def nm_keyfile_routes(text: str) -> list:
    """The static routes in a keyfile's [ipv4] and [ipv6] sections, as {to, via}."""
    out, section = [], None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        key = _key(line)
        if section in ("ipv4", "ipv6") and key is not None and re.match(r"^route\d+$", key):
            parts = stripped.split("=", 1)[1].split(",")
            if len(parts) >= 2 and parts[1].strip():
                out.append({"to": parts[0].strip(), "via": parts[1].strip()})
    return out


def edit_keyfile(text: str, change: dict) -> str:
    """Rewrite the IP settings Lanvex manages in a NetworkManager keyfile; keep every other line.

    [ipv4], replaced: method, addressN/addresses (an address's embedded gateway too), gateway, dns and
    dns-search (only when DNS changes), and never-default when a gateway is set (otherwise the gateway
    would never become the default route). [ipv6], when the change sets IPv6: method, addresses and
    gateway. In both, when it sets the routes: routeN and their options. The new lines go where the
    first replaced line was, so a preview shows only what really changes.
    """
    dns_changes = change["dns_servers"] is not None
    routes_change = change.get("routes") is not None
    ipv6 = change.get("ipv6")

    def managed4(key: str) -> bool:
        return bool(
            NM_IPV4_KEYS.match(key)
            or (dns_changes and NM_DNS_KEYS.match(key))
            or (key == "never-default" and change["gateway"])
            or (routes_change and NM_ROUTE_KEYS.match(key))
        )

    def managed6(key: str) -> bool:
        return bool(
            (ipv6 is not None and NM_IPV4_KEYS.match(key))
            or (ipv6 is not None and ipv6["gateway"] and key == "never-default")
            or (routes_change and NM_ROUTE_KEYS.match(key))
        )

    text = edit_keyfile_section(text, "ipv4", nm_ipv4_lines(change), managed4, create=True)
    if ipv6 is not None or routes_change:
        new6 = nm_ipv6_lines(change)
        text = edit_keyfile_section(text, "ipv6", new6, managed6, create=bool(new6))
    return text


def edit_keyfile_section(text: str, name: str, new: list, managed, create: bool) -> str:
    """Replace the `managed` keys of one keyfile section with `new`, where the first of them was."""

    def insert_at_section_end(out: list) -> None:
        pos = len(out)
        while pos > 0 and not out[pos - 1].strip():
            pos -= 1
        out[pos:pos] = new

    out, section, inserted, seen = [], None, False, False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if section == name and not inserted:
                insert_at_section_end(out)
                inserted = True
            section = stripped[1:-1]
            seen = seen or section == name
            out.append(line)
            continue
        key = _key(line)
        if section == name and key is not None and managed(key):
            if not inserted:
                out.extend(new)
                inserted = True
            continue
        out.append(line)
    if section == name and not inserted:
        insert_at_section_end(out)
        inserted = True
    if not inserted and not seen and create:
        if out and out[-1].strip():
            out.append("")
        out.append("[%s]" % name)
        out.extend(new)
    return "\n".join(out) + "\n"


def nm_plan(iface: str, profile: dict, change: dict) -> dict:
    path = profile["file"]
    if not path.startswith(NM_KEYFILE_DIR) or not path.endswith(".nmconnection"):
        where = path or "in memory"
        raise Refused(
            "unsupported",
            "%s is active on the NetworkManager profile '%s', which is not a keyfile in %s (%s). Activate a "
            "stored keyfile profile for it first (nmcli connection up <profile>)."
            % (iface, profile["name"], NM_KEYFILE_DIR, where),
        )
    before = read_text(path)
    after = edit_keyfile(before, change)
    removed = []
    if change.get("routes") is not None:
        wanted = {route_key(r) for r in change["routes"]}
        removed = [r for r in nm_keyfile_routes(before) if route_key(r) not in wanted]
    return {
        "backend": "networkmanager",
        "files": [
            {
                "path": path,
                "label": "NetworkManager profile '%s'" % profile["name"],
                "before": before,
                "after": after,
                "sha256": sha256_file(path),
                "action": "modify",
            }
        ],
        "commands": ["nmcli connection load %s" % path, "nmcli device reapply %s" % iface],
        "warnings": [],
        "_write": {path: after},
        "_changed": before != after,
        "_nm": {"uuid": profile["uuid"], "file": path},
        "_removed_routes": removed,
    }


def nm_activate(iface: str, uuid: str) -> None:
    r = run(["nmcli", "device", "reapply", iface], timeout=60)
    if r.returncode != 0:
        run(["nmcli", "connection", "up", uuid, "ifname", iface], timeout=90, check=True)


# ---------------------------------------------------------------------------------------------------------
# Bridges and bonds (kind "link"): one per change, created from interfaces with no addresses of their own,
# or removed again. Only Lanvex's own files are written; members' own configuration is never edited.


def read_links() -> dict:
    try:
        data = json.loads(read_text(LINKS_MANIFEST))
        return data.get("links") or {} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def render_links(links: dict) -> str:
    return json.dumps({"managed_by": "Lanvex", "links": links}, indent=2, sort_keys=True) + "\n"


def validate_link(req: dict) -> dict:
    if not isinstance(req, dict) or req.get("action") not in ("create", "remove"):
        raise Refused("invalid_request", "action must be 'create' or 'remove'.")
    name = check_interface(req.get("name"))
    if name.startswith(("lanvex", LINK_PROFILE)) or "." in name or ":" in name:
        raise Refused("invalid_request", "%s cannot be the name of a bridge or bond." % name)
    if req["action"] == "remove":
        return {"action": "remove", "name": name}
    kind = req.get("type")
    if kind not in ("bridge", "bond"):
        raise Refused("invalid_request", "type must be 'bridge' or 'bond'.")
    members = req.get("members")
    if not isinstance(members, list) or not 1 <= len(members) <= 8:
        raise Refused("invalid_request", "A bridge or bond needs between 1 and 8 member interfaces.")
    members = [check_interface(m) for m in members]
    if len(set(members)) != len(members) or name in members:
        raise Refused("invalid_request", "Each member once, and not the bridge or bond itself.")
    mode = req.get("mode") or "active-backup"
    if kind == "bond" and mode not in BOND_MODES:
        raise Refused("invalid_request", "Bond mode must be one of %s." % ", ".join(BOND_MODES))
    method = req.get("method") or "none"
    if method not in ("none", "static", "dhcp"):
        raise Refused("invalid_request", "method must be 'none', 'static' or 'dhcp'.")
    raw = req.get("addresses") or []
    if not isinstance(raw, list) or len(raw) > 32:
        raise Refused("invalid_request", "addresses must be a list of at most 32 addresses.")
    addresses = [check_ipv4_address(a, "addresses") for a in raw]
    gateway = req.get("gateway")
    gw = check_ipv4(gateway, "gateway") if gateway not in (None, "") else None
    if method == "static" and not addresses:
        raise Refused("invalid_request", "A static configuration needs at least one address.")
    if method != "static" and (addresses or gw):
        raise Refused("invalid_request", "Addresses and a gateway need the static method.")
    if gw is not None and not any(gw in a.network for a in addresses):
        raise Refused("invalid_request", "The gateway %s is not inside any of the networks." % gw)
    return {
        "action": "create",
        "name": name,
        "type": kind,
        "members": members,
        "mode": mode if kind == "bond" else None,
        "stp": bool(req.get("stp")) if kind == "bridge" else None,
        "method": method,
        "addresses": [str(a) for a in addresses],
        "gateway": str(gw) if gw else None,
    }


def link_members_free(change: dict) -> None:
    """Members must be there, in no other bridge or bond, the parent of no VLAN, and without addresses."""
    for m in change["members"]:
        base = "/sys/class/net/%s" % m
        if not os.path.exists(base):
            raise Refused("invalid_request", "There is no interface %s on this server." % m)
        if os.path.exists(base + "/master"):
            raise Refused("unsupported", "%s is already part of a bridge or bond." % m)
        if os.path.exists(base + "/bridge") or os.path.exists(base + "/bonding"):
            raise Refused("unsupported", "%s is itself a bridge or bond." % m)
        r = run(["ip", "-json", "address", "show", "dev", m], timeout=15, check=True)
        addrs = [
            a
            for link in json.loads(r.stdout or "[]")
            for a in link.get("addr_info", [])
            if a.get("scope") == "global"
        ]
        if addrs:
            raise Refused(
                "unsupported",
                "%s has addresses (%s). Only interfaces without addresses become members."
                % (m, ", ".join("%s/%s" % (a["local"], a["prefixlen"]) for a in addrs)),
            )
        r = run(["ip", "-json", "-details", "link", "show"], timeout=15, check=True)
        for link in json.loads(r.stdout or "[]"):
            if link.get("link") == m and (link.get("linkinfo") or {}).get("info_kind") == "vlan":
                raise Refused("unsupported", "%s carries the VLAN interface %s." % (m, link.get("ifname")))


def render_link_netplan(change: dict, member_ids: dict) -> str:
    """member_ids: member -> its netplan ID where another file defines it, else None (defined here)."""
    defn = {"interfaces": [member_ids.get(m) or m for m in change["members"]]}
    if change["type"] == "bond":
        defn["parameters"] = {"mode": change["mode"], "mii-monitor-interval": 100}
    else:
        defn["parameters"] = {"stp": bool(change["stp"])}
    if change["method"] == "static":
        defn["addresses"] = list(change["addresses"])
        if change["gateway"]:
            defn["routes"] = [{"to": "default", "via": change["gateway"]}]
    elif change["method"] == "dhcp":
        defn["dhcp4"] = True
    network = {"version": 2}
    own = [m for m in change["members"] if not member_ids.get(m)]
    if own:
        network["ethernets"] = {m: {} for m in own}
    network[change["type"] + "s"] = {change["name"]: defn}
    return "# " + MANAGED + "\n" + dump_netplan({"network": network})


def render_link_keyfiles(change: dict, uuids: dict) -> dict:
    """{path: text} for the bridge or bond profile and one port profile per member."""
    name, kind = change["name"], change["type"]
    head = [
        "# " + MANAGED,
        "[connection]",
        "id=" + LINK_PROFILE + name,
        "uuid=" + uuids[name],
        "type=" + kind,
    ]
    head += ["interface-name=" + name, "autoconnect-slaves=1", ""]
    if kind == "bond":
        head += ["[bond]", "mode=" + change["mode"], "miimon=100", ""]
    else:
        head += ["[bridge]", "stp=" + ("true" if change["stp"] else "false"), ""]
    ipv4 = {"none": "disabled", "static": "manual", "dhcp": "auto"}[change["method"]]
    head += ["[ipv4]", "method=" + ipv4]
    head += ["address%d=%s" % (i, a) for i, a in enumerate(change["addresses"], 1)]
    if change["gateway"]:
        head.append("gateway=" + change["gateway"])
    head += ["", "[ipv6]", "method=disabled", ""]
    out = {os.path.join(NM_KEYFILE_DIR, LINK_PROFILE + name + ".nmconnection"): "\n".join(head)}
    for m in change["members"]:
        port = LINK_PROFILE + name + "-" + m
        out[os.path.join(NM_KEYFILE_DIR, port + ".nmconnection")] = "\n".join(
            [
                "# " + MANAGED,
                "[connection]",
                "id=" + port,
                "uuid=" + uuids[m],
                "type=ethernet",
                "interface-name=" + m,
                "master=" + name,
                "slave-type=" + kind,
                # Wins over another profile for the same device when NetworkManager starts.
                "autoconnect-priority=100",
                "",
            ]
        )
    return out


def link_backend(members: list) -> str:
    if nm_running():
        states = nm_device_states()
        if all(states.get(m, "unmanaged") not in ("unmanaged", "unavailable") for m in members):
            return "networkmanager"
    if which("netplan") and os.path.isdir(NETPLAN_DIR):
        return "netplan"
    raise Refused(
        "unsupported",
        "Lanvex creates bridges and bonds where NetworkManager or netplan manages the member interfaces; "
        "neither does here.",
    )


def link_plan(change: dict) -> dict:
    name = change["name"]
    links = read_links()
    if change["action"] == "remove":
        own = links.get(name)
        if not own:
            raise Refused("unsupported", "%s was not created by Lanvex; Lanvex removes only its own." % name)
        rest = {k: v for k, v in links.items() if k != name}
        files = [
            {
                "path": path,
                "label": "Lanvex's %s %s" % (own.get("type", "link"), name),
                "before": read_text(path) if os.path.exists(path) else "",
                "after": "",
                "sha256": sha256_file(path),
                "action": "delete",
            }
            for path in own.get("files") or []
        ]
        files.append(
            {
                "path": LINKS_MANIFEST,
                "label": "Lanvex's list of its bridges and bonds",
                "before": read_text(LINKS_MANIFEST),
                "after": render_links(rest),
                "sha256": sha256_file(LINKS_MANIFEST),
                "action": "modify",
            }
        )
        backend = own.get("backend") or "netplan"
        commands = (
            ["netplan apply", "ip link delete %s" % name]
            if backend == "netplan"
            else ["nmcli connection reload", "ip link delete %s" % name]
        )
        return {
            "backend": backend,
            "files": files,
            "commands": commands,
            "warnings": [
                "%s and every address on it go; %s are free again."
                % (name, ", ".join(own.get("members") or []))
            ],
            "_write": {LINKS_MANIFEST: render_links(rest)},
            "_delete": list(own.get("files") or []),
            "_changed": True,
            "_link": {"name": name, "members": own.get("members") or [], "existed": True},
        }
    if os.path.exists("/sys/class/net/%s" % name) or name in links:
        raise Refused("unsupported", "There is already an interface %s on this server." % name)
    link_members_free(change)
    backend = link_backend(change["members"])
    write = {}
    if backend == "netplan":
        docs = netplan_docs()
        ids = {}
        for m in change["members"]:
            found = netplan_definitions(m, docs)
            if len(found) > 1:
                raise Refused("unsupported", "%s is defined in more than one netplan file." % m)
            if found:
                path, section, key = found[0]
                defn = (docs[path]["network"][section] or {}).get(key) or {}
                if any(defn.get(k) for k in ("addresses", "dhcp4", "dhcp6", "routes", "gateway4")):
                    raise Refused(
                        "unsupported",
                        "%s has IP settings of its own in %s. Remove them there first."
                        % (m, os.path.basename(path)),
                    )
                ids[m] = key
        path = os.path.join(NETPLAN_DIR, "61-%s%s.yaml" % (LINK_PROFILE, name))
        write[path] = render_link_netplan(change, ids)
        commands = ["netplan generate", "netplan apply"]
    else:
        import uuid as uuidlib  # noqa: PLC0415 - only here

        uuids = {k: str(uuidlib.uuid4()) for k in [name, *change["members"]]}
        write = render_link_keyfiles(change, uuids)
        commands = ["nmcli connection load %s" % p for p in write] + [
            "nmcli connection up id %s%s" % (LINK_PROFILE, name)
        ]
    links = dict(links)
    links[name] = {
        "type": change["type"],
        "members": change["members"],
        "backend": backend,
        "files": sorted(write),
    }
    files = [
        {
            "path": p,
            "label": "Lanvex's %s %s" % (change["type"], name),
            "before": "",
            "after": t,
            "sha256": None,
            "action": "create",
        }
        for p, t in sorted(write.items())
    ]
    files.append(
        {
            "path": LINKS_MANIFEST,
            "label": "Lanvex's list of its bridges and bonds",
            "before": read_text(LINKS_MANIFEST) if os.path.exists(LINKS_MANIFEST) else "",
            "after": render_links(links),
            "sha256": sha256_file(LINKS_MANIFEST),
            "action": "modify" if os.path.exists(LINKS_MANIFEST) else "create",
        }
    )
    write[LINKS_MANIFEST] = render_links(links)
    warnings = []
    if change["method"] == "none":
        warnings.append("%s gets no address: it carries traffic, the server does not use it." % name)
    return {
        "backend": backend,
        "files": files,
        "commands": commands,
        "warnings": warnings,
        "_write": write,
        "_delete": [],
        "_changed": True,
        "_link": {"name": name, "members": change["members"], "existed": False},
    }


def link_apply(plan: dict) -> None:
    os.makedirs(os.path.dirname(LINKS_MANIFEST), mode=0o755, exist_ok=True)
    for path in plan["_delete"]:
        if os.path.exists(path):
            os.unlink(path)
    for path, text in plan["_write"].items():
        atomic_write(path, text.encode("utf-8"), 0o644 if path == LINKS_MANIFEST else 0o600, 0, 0)
    link_activate(plan["backend"], plan["_link"], present=not plan["_link"]["existed"])


def link_activate(backend: str, link: dict, present: bool) -> None:
    """Bring the configuration on disk into effect: the link up with its members, or gone."""
    name = link["name"]
    if backend == "netplan":
        netplan_apply()
    else:
        run(["nmcli", "connection", "reload"], timeout=30, check=True)
        if present:
            run(["nmcli", "connection", "up", "id", LINK_PROFILE + name], timeout=90, check=True)
            for m in link["members"]:
                run(["nmcli", "connection", "up", "id", "%s%s-%s" % (LINK_PROFILE, name, m)], timeout=90)
    if not present and os.path.exists("/sys/class/net/%s" % name):
        run(["ip", "link", "delete", name], timeout=30)
    if not present and backend == "networkmanager":
        for m in link["members"]:
            run(["nmcli", "device", "connect", m], timeout=60)  # its own profile, if it has one


def link_restore(state: dict) -> None:
    """After the files are back: what was there before the change, in effect again."""
    link = state.get("link") or {}
    if link:
        link_activate(state["backend"], link, present=link.get("existed", False))


def link_verify(st: dict) -> list:
    link, change = st["link"], st["change"]
    name = link["name"]
    base = "/sys/class/net/%s" % name
    if change["action"] == "remove":
        ok, detail = _wait_for(
            lambda: (
                not os.path.exists(base),
                "%s is %s." % (name, "still there" if os.path.exists(base) else "gone"),
            ),
            15,
        )
        checks = [{"id": "link", "label": "%s removed" % name, "ok": ok, "detail": detail}]
        held = [m for m in link["members"] if os.path.exists("/sys/class/net/%s/master" % m)]
        checks.append(
            {
                "id": "members",
                "label": "Members free",
                "ok": not held,
                "detail": ("Still in a bridge or bond: %s." % ", ".join(held))
                if held
                else "%s free." % ", ".join(link["members"]),
            }
        )
        return checks
    kind_dir = base + ("/bonding" if change["type"] == "bond" else "/bridge")
    ok, detail = _wait_for(
        lambda: (
            os.path.isdir(kind_dir),
            "%s %s." % (name, "is a " + change["type"] if os.path.isdir(kind_dir) else "is missing"),
        ),
        20,
    )
    checks = [{"id": "link", "label": "%s is a %s" % (name, change["type"]), "ok": ok, "detail": detail}]

    def members_ok():
        out = []
        for m in change["members"]:
            master = "/sys/class/net/%s/master" % m
            out.append((m, os.path.basename(os.path.realpath(master)) if os.path.exists(master) else None))
        wrong = ["%s (%s)" % (m, w or "in none") for m, w in out if w != name]
        return (
            not wrong,
            ("Not in %s: %s." % (name, ", ".join(wrong)))
            if wrong
            else "%s in %s." % (", ".join(change["members"]), name),
        )

    ok, detail = _wait_for(members_ok, 20)
    checks.append({"id": "members", "label": "Members of %s" % name, "ok": ok, "detail": detail})
    if change["method"] == "static":
        want = sorted(change["addresses"])

        def addresses_ok():
            got = sorted(a["address"] for a in ipv4_addresses(name) if not a["dynamic"])
            return (got == want, "Expected %s, found %s." % (", ".join(want), ", ".join(got) or "none"))

        ok, detail = _wait_for(addresses_ok, 20)
        checks.append({"id": "addresses", "label": "IPv4 addresses on %s" % name, "ok": ok, "detail": detail})
        if change["gateway"]:
            checks.append(gateway_check(name, change["gateway"]))
    elif change["method"] == "dhcp":

        def lease():
            got = [a["address"] for a in ipv4_addresses(name) if a["dynamic"]]
            return (bool(got), "DHCP address: %s." % (", ".join(got) or "none yet"))

        ok, detail = _wait_for(lease, 45)
        checks.append({"id": "addresses", "label": "DHCP address on %s" % name, "ok": ok, "detail": detail})
    return checks


# ---------------------------------------------------------------------------------------------------------
# Networks: VLAN interfaces, their firewalld zones, and DHCP (kind "networks")
#
# The request is the complete set Lanvex manages on this server: every VLAN interface it should have,
# and every DHCP pool. A Lanvex-owned VLAN missing from the request is removed. Only Lanvex's own files
# are written: NetworkManager profiles lanvex-<name>, firewalld zones lx-<name>, /etc/dnsmasq.d/lanvex.conf.


def zone_name(iface: str) -> str:
    return (ZONE_PREFIX + iface)[:17]


def validate_networks(req: dict) -> dict:
    """Normalise the requested VLAN interfaces and DHCP pools. Everything is checked here again."""
    if not isinstance(req, dict):
        raise Refused("invalid_request", "Request must be a JSON object.")
    raw_vlans = req.get("vlans")
    if not isinstance(raw_vlans, list) or len(raw_vlans) > 64:
        raise Refused("invalid_request", "vlans must be a list of at most 64 VLAN interfaces.")
    vlans, names, ids, zones = [], set(), set(), set()
    for v in raw_vlans:
        if not isinstance(v, dict):
            raise Refused("invalid_request", "Each VLAN interface must be an object.")
        name, parent = check_interface(v.get("name")), check_interface(v.get("parent"))
        vid = v.get("id")
        if not isinstance(vid, int) or isinstance(vid, bool) or not 1 <= vid <= 4094:
            raise Refused("invalid_request", "%s: the VLAN ID must be between 1 and 4094." % name)
        if name in names:
            raise Refused("invalid_request", "%s is listed twice." % name)
        if (parent, vid) in ids:
            raise Refused("invalid_request", "VLAN %d on %s is listed twice." % (vid, parent))
        if zone_name(name) in zones:
            raise Refused(
                "invalid_request", "%s: its firewalld zone name %s is taken." % (name, zone_name(name))
            )
        raw_addresses = v.get("addresses") or []
        if not isinstance(raw_addresses, list) or not 1 <= len(raw_addresses) <= 8:
            raise Refused("invalid_request", "%s needs between 1 and 8 IPv4 addresses." % name)
        addresses = [check_ipv4_address(a, "%s addresses" % name) for a in raw_addresses]
        names.add(name)
        ids.add((parent, vid))
        zones.add(zone_name(name))
        vlans.append({"name": name, "parent": parent, "id": vid, "addresses": [str(a) for a in addresses]})
    for v in vlans:
        if v["parent"] in names:
            raise Refused(
                "invalid_request", "%s: a VLAN interface on another Lanvex VLAN is not supported." % v["name"]
            )

    dhcp = req.get("dhcp")
    if dhcp is not None:
        if not isinstance(dhcp, dict) or not isinstance(dhcp.get("pools"), list) or len(dhcp["pools"]) > 64:
            raise Refused("invalid_request", 'dhcp must be null or {"pools": [...]}, at most 64 pools.')
        by_name = {v["name"]: v for v in vlans}
        pools, served = [], set()
        for p in dhcp["pools"]:
            if not isinstance(p, dict):
                raise Refused("invalid_request", "Each DHCP pool must be an object.")
            iface = check_interface(p.get("interface"))
            if iface not in by_name:
                # A DHCP server on a network that already has one disturbs every device on it: Lanvex
                # serves DHCP only on the VLAN interfaces it creates.
                raise Refused(
                    "invalid_request", "%s: Lanvex serves DHCP only on VLAN interfaces it manages." % iface
                )
            if iface in served:
                raise Refused("invalid_request", "%s has more than one DHCP pool." % iface)
            served.add(iface)
            nets = [ipaddress.ip_interface(a) for a in by_name[iface]["addresses"]]
            start, end = (
                check_ipv4(p.get("start"), "%s pool start" % iface),
                check_ipv4(p.get("end"), "%s pool end" % iface),
            )
            net = next((n.network for n in nets if start in n.network), None)
            if net is None or end not in net:
                raise Refused(
                    "invalid_request",
                    "%s: the pool %s-%s is not inside one network of the interface (%s)."
                    % (iface, start, end, ", ".join(str(n.network) for n in nets)),
                )
            if start > end:
                raise Refused(
                    "invalid_request", "%s: the pool starts (%s) after it ends (%s)." % (iface, start, end)
                )
            if start in (net.network_address, net.broadcast_address) or end in (
                net.network_address,
                net.broadcast_address,
            ):
                raise Refused(
                    "invalid_request", "%s: the pool includes the network or broadcast address." % iface
                )
            own = [n.ip for n in nets if n.network == net]
            if any(start <= ip <= end for ip in own):
                raise Refused(
                    "invalid_request",
                    "%s: the server's own address %s is inside the pool and could be given to a device."
                    % (iface, next(ip for ip in own if start <= ip <= end)),
                )
            router = check_ipv4(p.get("router") or str(own[0]), "%s router" % iface)
            if router not in net:
                raise Refused("invalid_request", "%s: the router %s is not in %s." % (iface, router, net))
            dns = [str(check_ipv4(x, "%s DNS server" % iface)) for x in (p.get("dns") or [str(own[0])])]
            if len(dns) > 4:
                raise Refused("invalid_request", "%s: at most 4 DNS servers." % iface)
            lease = p.get("lease") or "12h"
            if not isinstance(lease, str) or not LEASE_RE.match(lease):
                raise Refused(
                    "invalid_request",
                    "%s: lease time %r is not like 12h, 30m, 2d or infinite." % (iface, lease),
                )
            raw_res = p.get("reservations") or []
            if not isinstance(raw_res, list) or len(raw_res) > 1000:
                raise Refused("invalid_request", "%s: at most 1000 fixed addresses." % iface)
            reservations, macs, ips = [], set(), set()
            for r in raw_res:
                mac = str((r or {}).get("mac") or "").lower().replace("-", ":")
                if not MAC_RE.match(mac):
                    raise Refused(
                        "invalid_request",
                        "%s: %r is not a MAC address like aa:bb:cc:dd:ee:ff." % (iface, r.get("mac")),
                    )
                ip = check_ipv4(r.get("ip"), "%s fixed address" % iface)
                if ip not in net or ip in (net.network_address, net.broadcast_address) or ip in own:
                    raise Refused(
                        "invalid_request",
                        "%s: the fixed address %s is not a free address in %s." % (iface, ip, net),
                    )
                name = r.get("name") or ""
                if name and (not isinstance(name, str) or not HOSTNAME_RE.match(name)):
                    raise Refused("invalid_request", "%s: %r is not a host name." % (iface, name))
                if mac in macs or str(ip) in ips:
                    raise Refused(
                        "invalid_request", "%s: %s or %s has two fixed addresses." % (iface, mac, ip)
                    )
                macs.add(mac)
                ips.add(str(ip))
                reservations.append({"mac": mac, "ip": str(ip), "name": name})
            pools.append(
                {
                    "interface": iface,
                    "network": str(net),
                    "start": str(start),
                    "end": str(end),
                    "router": str(router),
                    "dns": dns,
                    "lease": lease,
                    "reservations": sorted(reservations, key=lambda x: ipaddress.ip_address(x["ip"])),
                }
            )
        dhcp = {"pools": sorted(pools, key=lambda x: x["interface"])}
    access = validate_access(req.get("access"), {v["name"]: v for v in vlans})
    takeover = req.get("takeover")
    if takeover not in (None, "netzverwaltung"):
        raise Refused("invalid_request", 'takeover is null or "netzverwaltung".')
    return {
        "kind": "networks",
        "vlans": sorted(vlans, key=lambda x: x["name"]),
        "dhcp": dhcp,
        "access": access,
        "takeover": takeover,
    }


def check_port(value: object, field: str) -> str:
    """'22/tcp' or '8000-8100/udp'."""
    m = PORT_RE.match(value) if isinstance(value, str) else None
    if not m:
        raise Refused(
            "invalid_request", "%s: %r is not a port like 22/tcp or 8000-8100/udp." % (field, value)
        )
    first, last = int(m.group(1)), int(m.group(2) or m.group(1))
    if not 1 <= first <= last <= 65535:
        raise Refused("invalid_request", "%s: %r is not a port between 1 and 65535." % (field, value))
    return "%d%s/%s" % (first, "-%d" % last if last != first else "", m.group(3))


def validate_access(access: object, vlans: dict) -> dict | None:
    """What devices on each Lanvex network may reach. None: as last applied."""
    if access is None:
        return None
    if not isinstance(access, dict) or not isinstance(access.get("rules"), list) or len(access["rules"]) > 64:
        raise Refused("invalid_request", 'access must be null or {"rules": [...]}, at most 64 rules.')
    rules = {}
    for r in access["rules"]:
        if not isinstance(r, dict) or r.get("interface") not in vlans:
            raise Refused(
                "invalid_request", "Access rules apply to Lanvex's VLAN interfaces in this request only."
            )
        name = r["interface"]
        if name in rules:
            raise Refused("invalid_request", "%s has more than one access rule." % name)
        if not isinstance(r.get("internet", False), bool):
            raise Refused("invalid_request", "%s: internet must be true or false." % name)
        dests, services = r.get("destinations") or [], r.get("services") or []
        if (
            not isinstance(dests, list)
            or len(dests) > 64
            or not isinstance(services, list)
            or len(services) > 32
        ):
            raise Refused("invalid_request", "%s: at most 64 destinations and 32 services." % name)
        own = ipaddress.ip_interface(vlans[name]["addresses"][0]).network
        out = []
        for d in dests:
            if not isinstance(d, dict):
                raise Refused("invalid_request", "%s: a destination is {network, ports}." % name)
            try:
                net = ipaddress.IPv4Network(str(d.get("network")), strict=False)
            except ValueError as e:
                raise Refused(
                    "invalid_request", "%s: %r is not an IPv4 network." % (name, d.get("network"))
                ) from e
            if net.prefixlen == 0:
                raise Refused(
                    "invalid_request", "%s: allow the Internet with internet, not 0.0.0.0/0." % name
                )
            if net.overlaps(own):
                raise Refused("invalid_request", "%s: %s is this network itself." % (name, net))
            ports = d.get("ports") or []
            if not isinstance(ports, list) or len(ports) > 16:
                raise Refused("invalid_request", "%s: at most 16 ports per destination." % name)
            out.append(
                {"network": str(net), "ports": sorted({check_port(x, name) for x in ports}, key=_port_key)}
            )
        rules[name] = {
            "interface": name,
            "internet": bool(r.get("internet", False)),
            "destinations": sorted(out, key=lambda x: (ipaddress.ip_network(x["network"]), x["ports"])),
            "services": sorted({check_port(x, name) for x in services}, key=_port_key),
        }
        f = r.get("dns_filter")
        if f is not None:
            if not isinstance(f, dict):
                raise Refused(
                    "invalid_request", '%s: dns_filter is null or {"block": [...], "allow": [...]}.' % name
                )
            block, allow = f.get("block") or [], f.get("allow") or []
            if (
                not isinstance(block, list)
                or len(block) > 5000
                or not isinstance(allow, list)
                or len(allow) > 1000
            ):
                raise Refused(
                    "invalid_request", "%s: at most 5000 blocked domains and 1000 exceptions." % name
                )
            rules[name]["dns_filter"] = {
                "block": sorted({check_domain_name(d, name) for d in block}),
                "allow": sorted({check_domain_name(d, name) for d in allow}),
            }
    return {"rules": [rules[k] for k in sorted(rules)]}


def check_domain_name(value: object, field: str) -> str:
    """A domain for the DNS filter: it and every name below it."""
    d = value.strip().lower().lstrip("*").strip(".") if isinstance(value, str) else ""
    if not FILTER_DOMAIN_RE.match(d):
        raise Refused("invalid_request", "%s: %r is not a domain like example.com." % (field, value))
    return d


def _port_key(port: str) -> tuple:
    number, proto = port.split("/")
    return (int(number.split("-")[0]), number, proto)


def default_rule(name: str) -> dict:
    """A Lanvex network without access rules: DHCP and DNS on the server, nothing else."""
    return {"interface": name, "internet": False, "destinations": [], "services": []}


def render_vlan_keyfile(v: dict, uuid_: str, zone: str | None) -> str:
    lines = [
        "# " + MANAGED,
        "[connection]",
        "id=" + LANVEX_PROFILE + v["name"],
        "uuid=" + uuid_,
        "type=vlan",
        "interface-name=" + v["name"],
    ]
    if zone:
        lines.append("zone=" + zone)
    lines += [
        "",
        "[vlan]",
        "flags=1",
        "id=%d" % v["id"],
        "parent=" + v["parent"],
        "",
        "[ipv4]",
        "method=manual",
    ]
    lines += ["address%d=%s" % (i, a) for i, a in enumerate(v["addresses"], 1)]
    lines += ["", "[ipv6]", "method=disabled", ""]
    return "\n".join(lines)


def render_zone(v: dict, services: list, bind: bool = False, ports: list | None = None) -> str:
    """bind: the zone names its interface itself (netplan, ifupdown); NetworkManager sets it per profile."""
    # No double hyphen in the comment: XML forbids it there, and firewalld then rejects the file.
    out = [
        '<?xml version="1.0" encoding="utf-8"?>',
        "<!-- %s -->" % MANAGED,
        "<zone>",
        "  <short>Lanvex: %s</short>" % v["name"],
        "  <description>Devices on %s (VLAN %d) may use these services on this server.</description>"
        % (v["name"], v["id"]),
    ]
    if bind:
        out.append('  <interface name="%s"/>' % v["name"])
    out += ['  <service name="%s"/>' % s for s in services]
    out += ['  <port port="%s" protocol="%s"/>' % pp for pp in _fw_ports(ports or [])]
    out.append("</zone>")
    return "\n".join(out) + "\n"


def render_dnsmasq(
    pools: list, interfaces: bool = True, bind_dynamic: bool = False, filters: dict | None = None
) -> str:
    """interfaces: list each interface (dnsmasq then answers only on the listed ones). Left out when
    another configuration has dnsmasq answer on every interface, which the lines would end.
    bind_dynamic: bind only those interfaces' addresses, so dnsmasq starts next to systemd-resolved."""
    lines = ["# " + MANAGED, "# One block per network Lanvex serves DHCP on.", ""]
    if bind_dynamic:
        lines += [
            "# Bind only the interfaces dnsmasq serves (systemd-resolved holds 127.0.0.53:53).",
            "bind-dynamic",
            "",
        ]
    if filters:
        lines += [
            "# DNS filters answer on Lanvex's link %s, which the main dnsmasq leaves to them." % DNS_LINK,
            "except-interface=" + DNS_LINK,
            "",
        ]
    for p in pools:
        tag = zone_name(p["interface"])
        mask = str(ipaddress.ip_network(p["network"]).netmask)
        lines += ["# %s: %s" % (p["interface"], p["network"])]
        if interfaces:
            lines.append("interface=" + p["interface"])
        lines += [
            "dhcp-range=set:%s,%s,%s,%s,%s" % (tag, p["start"], p["end"], mask, p["lease"]),
            "dhcp-option=tag:%s,option:router,%s" % (tag, p["router"]),
            "dhcp-option=tag:%s,option:dns-server,%s"
            % (tag, (filters or {}).get(p["interface"]) or ",".join(p["dns"])),
        ]
        for r in p["reservations"]:
            lines.append(
                "dhcp-host=%s,set:%s,%s%s" % (r["mac"], tag, r["ip"], "," + r["name"] if r["name"] else "")
            )
        lines.append("")
    return "\n".join(lines)


def render_netplan_vlans(vlans: list, links: dict) -> str:
    """Lanvex's netplan file: its VLANs only. links: parent interface -> its netplan ID."""
    doc = {
        "network": {
            "version": 2,
            "vlans": {
                v["name"]: {"id": v["id"], "link": links[v["parent"]], "addresses": list(v["addresses"])}
                for v in vlans
            },
        }
    }
    return "# " + MANAGED + "\n" + dump_netplan(doc)


def render_ifupdown_vlans(vlans: list) -> str:
    """Lanvex's ifupdown file. The VLAN device is created and deleted by the stanza itself, so any name
    works and Debian's vlan package is not needed."""
    lines = ["# " + MANAGED, ""]
    for v in vlans:
        name = v["name"]
        lines += [
            "auto %s" % name,
            "iface %s inet static" % name,
            # Skipped when ifupdown created it already (it does for names like eth0.50).
            "    pre-up [ -e /sys/class/net/%s ] || ip link add link %s name %s type vlan id %d"
            % (name, v["parent"], name, v["id"]),
            "    address %s" % v["addresses"][0],
        ]
        lines += ["    up ip address add %s dev %s" % (a, name) for a in v["addresses"][1:]]
        lines += ["    post-down ip link delete %s" % name, ""]
    return "\n".join(lines)


def ifupdown_stanzas(text: str) -> dict:
    """iface name -> its stanza's text, for files in the form render_ifupdown_vlans writes."""
    out, name = {}, None
    for line in text.splitlines():
        words = line.split()
        if words[:1] == ["iface"] and len(words) > 1:
            name = words[1]
            out.setdefault(name, [])
        elif words[:1] in (["auto"], ["allow-hotplug"]) or not line.startswith((" ", "\t")):
            name = None if not line.startswith("#") else name
        if name is not None:
            out[name].append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def ifupdown_sources_dir() -> bool:
    """Does /etc/network/interfaces read the files in /etc/network/interfaces.d (Debian's default)?"""
    try:
        text = read_text(IFUPDOWN_MAIN)
    except OSError:
        return False
    for line in text.splitlines():
        words = line.split()
        if len(words) == 2 and words[0] == "source" and words[1].startswith(IFUPDOWN_DIR + "/"):
            return True
        if len(words) == 2 and words[0] == "source-directory" and words[1].rstrip("/") == IFUPDOWN_DIR:
            return True
    return False


def ifupdown_defined_elsewhere(name: str) -> str | None:
    """Another ifupdown file that defines `name`."""
    files = [IFUPDOWN_MAIN]
    if os.path.isdir(IFUPDOWN_DIR):
        files += sorted(os.path.join(IFUPDOWN_DIR, f) for f in os.listdir(IFUPDOWN_DIR))
    for path in files:
        if path == IFUPDOWN_LANVEX or not os.path.isfile(path):
            continue
        try:
            for line in read_text(path).splitlines():
                words = line.split()
                if len(words) > 1 and words[0] == "iface" and words[1] == name:
                    return path
        except OSError:
            continue
    return None


def netplan_docs(exclude: str | None = None) -> dict:
    yaml = _yaml()
    docs = {}
    if os.path.isdir(NETPLAN_DIR):
        for f in sorted(os.listdir(NETPLAN_DIR)):
            path = os.path.join(NETPLAN_DIR, f)
            if f.endswith(".yaml") and path != exclude:
                try:
                    docs[path] = yaml.safe_load(read_text(path))
                except yaml.YAMLError as e:
                    raise Refused("unsupported", "%s is not valid YAML: %s" % (path, e)) from e
    return docs


def nm_device_states() -> dict:
    r = run(["nmcli", "-t", "-f", "DEVICE,STATE", "device"], timeout=15)
    out = {}
    for line in r.stdout.splitlines():
        parts = nm_split(line)
        if len(parts) >= 2:
            out[parts[0]] = parts[1]
    return out


def read_manifest() -> dict:
    try:
        data = json.loads(read_text(MANIFEST))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def render_manifest(
    backend: str,
    names: list,
    dnsmasq_before: dict | None = None,
    access: list | None = None,
    forward_before: str | None = None,
    dhcp_server: str | None = None,
) -> str:
    doc = {"managed_by": "Lanvex", "backend": backend, "vlans": sorted(names)}
    if dhcp_server == "kea":
        doc["dhcp_server"] = "kea"  # Lanvex's pools are in Kea, not dnsmasq
    if dnsmasq_before is not None:
        # How dnsmasq ran before Lanvex served DHCP here, so it is left that way once Lanvex stops.
        doc["dnsmasq_before"] = dnsmasq_before
    if access:
        doc["access"] = access  # the access rules last applied, for Lanvex to compare
    if forward_before is not None:
        doc["forward_before"] = forward_before  # IP forwarding before Lanvex switched it on
    return json.dumps(doc, indent=2) + "\n"


def choose_backend(change: dict) -> str:
    """How this server's VLANs are configured: by whoever manages their parent interfaces. Once Lanvex
    has VLANs here, it keeps using the same way."""
    known = read_manifest().get("backend")
    if known in ("networkmanager", "netplan", "ifupdown") and read_manifest().get("vlans"):
        return known
    parents = sorted({v["parent"] for v in change["vlans"]})
    if nm_running():
        states = nm_device_states()
        if not parents or all(
            states.get(p, "unmanaged") not in ("unmanaged", "unavailable") for p in parents
        ):
            return "networkmanager"
    if which("netplan") and os.path.isdir(NETPLAN_DIR):
        docs = netplan_docs(exclude=NETPLAN_LANVEX)
        if not parents or all(netplan_definitions(p, docs) for p in parents):
            return "netplan"
    if which("ifup") and os.path.exists(IFUPDOWN_MAIN):
        return "ifupdown"
    raise Refused(
        "unsupported",
        "Lanvex creates VLAN interfaces where NetworkManager, netplan or ifupdown manages their parent "
        "interface (%s); none of them does here." % ", ".join(parents),
    )


# ---------------------------------------------------------------------------------------------------------
# Kea


def kea_unit() -> str | None:
    for unit in KEA_UNITS:
        r = run(["systemctl", "show", "-p", "LoadState", "--value", unit], timeout=15)
        if r.stdout.strip() == "loaded":
            return unit
    return None


def strip_kea_comments(text: str) -> str:
    """Kea's JSON with its comments (//, #, /* */) removed and its includes read as empty lists."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\":
                out.append(text[i + 1 : i + 2])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i) or c == "#":
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif text.startswith("<?include", i):
            end = text.find("?>", i)
            out.append("[]")
            i = n if end < 0 else end + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def kea_setup() -> dict:
    """Whether Kea serves DHCP here for Lanvex: installed, with Lanvex's file included, and on which
    interfaces it listens ("*" for all)."""
    unit = kea_unit()
    try:
        text = read_text(KEA_CONF)
    except OSError:
        return {"unit": unit, "included": False, "interfaces": None}
    interfaces = None
    try:
        dhcp4 = json.loads(strip_kea_comments(text)).get("Dhcp4") or {}
        interfaces = (dhcp4.get("interfaces-config") or {}).get("interfaces")
    except ValueError:
        pass
    return {"unit": unit, "included": bool(KEA_INCLUDE_RE.search(text)), "interfaces": interfaces}


def dhcp_server() -> str:
    """Which DHCP server serves Lanvex's pools here: Kea once kea-dhcp4.conf includes Lanvex's file,
    otherwise dnsmasq. Kept for as long as Lanvex serves DHCP with it."""
    if read_manifest().get("dhcp_server") == "kea" and os.path.exists(KEA_FILE):
        return "kea"
    if os.path.exists(DNSMASQ_FILE):
        return "dnsmasq"  # Lanvex's pools are in dnsmasq: they stay there, never served twice
    setup = kea_setup()
    return "kea" if setup["unit"] and setup["included"] else "dnsmasq"


def lease_seconds(lease: str) -> int:
    if lease == "infinite":
        return 4294967295
    return int(lease[:-1]) * {"m": 60, "h": 3600, "d": 86400}[lease[-1]]


def kea_subnet_id(iface: str) -> int:
    """Stable per interface, and far from the small numbers people give their own subnets."""
    return 0x40000000 | (int(hashlib.sha256(iface.encode()).hexdigest()[:8], 16) & 0x3FFFFFFF)


def render_kea(pools: list) -> str:
    """Lanvex's shared networks for Kea: one per network, with its subnet, pool and fixed addresses."""
    nets = []
    for p in pools:
        subnet = {
            "id": kea_subnet_id(p["interface"]),
            "subnet": p["network"],
            "pools": [{"pool": "%s - %s" % (p["start"], p["end"])}],
            "valid-lifetime": lease_seconds(p["lease"]),
            "option-data": [
                {"name": "routers", "data": p["router"]},
                {"name": "domain-name-servers", "data": ", ".join(p["dns"])},
            ],
            "reservations": [
                dict(
                    {"hw-address": r["mac"], "ip-address": r["ip"]},
                    **({"hostname": r["name"]} if r["name"] else {}),
                )
                for r in p["reservations"]
            ],
        }
        # No "interface" key: kea-dhcp4 -t refuses one that does not exist yet, as a new VLAN does not
        # before the change. Kea picks the subnet by the address of the interface a request comes in on.
        nets.append(
            {
                "name": "lanvex-" + p["interface"],
                "user-context": {"lanvex": MANAGED, "interface": p["interface"]},
                "subnet4": [subnet],
            }
        )
    return json.dumps(nets, indent=2) + "\n"


def kea_pools(text: str) -> set:
    """The interfaces Lanvex's Kea file serves."""
    try:
        return {
            (n.get("user-context") or {}).get("interface") for n in json.loads(text) if isinstance(n, dict)
        } - {None}
    except ValueError:
        return set()


def kea_validate(text: str) -> None:
    """kea-dhcp4 -t on the configuration as it would be, with the candidate in place of Lanvex's file."""
    binary = which("kea-dhcp4")
    if not binary:
        raise Refused("missing_tool", "kea-dhcp4 is not installed.")
    root = tempfile.mkdtemp(prefix="lanvex-kea-")
    try:
        candidate = os.path.join(root, "lanvex-shared-networks.json")
        with open(candidate, "w", encoding="utf-8") as f:
            f.write(text)
        main = os.path.join(root, "kea-dhcp4.conf")
        with open(main, "w", encoding="utf-8") as f:
            f.write(KEA_INCLUDE_RE.sub('<?include "%s"?>' % candidate, read_text(KEA_CONF)))
        r = run([binary, "-t", main], timeout=60)
        if r.returncode != 0:
            raise Refused(
                "rejected", "Kea rejected the new configuration: %s" % (r.stderr or r.stdout).strip()[-800:]
            )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def kea_command(command: str) -> dict | None:
    """A command over Kea's control socket, where there is one; None without."""
    import socket  # noqa: PLC0415 - only here

    for path in KEA_SOCKETS:
        if not os.path.exists(path):
            continue
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(10)
                s.connect(path)
                s.sendall(json.dumps({"command": command}).encode())
                data = b""
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                    try:
                        return json.loads(data.decode())
                    except ValueError:
                        continue
        except OSError:
            continue
    return None


def kea_check(n: dict) -> tuple:
    """Kea runs, and (asked over its control socket, where it has one) serves Lanvex's networks."""
    k = n["kea"]
    active = run(["systemctl", "is-active", k["unit"]], timeout=15).stdout.strip()
    if not k["wanted"]:
        return (True, "Kea serves no Lanvex network.")
    if active != "active":
        return (False, "%s is %s." % (k["unit"], active or "not running"))
    answer = kea_command("config-get")
    if answer is None:
        return (True, "%s is running with the new configuration (checked with kea-dhcp4 -t)." % k["unit"])
    nets = {
        (x.get("user-context") or {}).get("interface")
        for x in ((answer.get("arguments") or {}).get("Dhcp4") or {}).get("shared-networks") or []
        if isinstance(x, dict) and (x.get("user-context") or {}).get("lanvex")
    }
    missing = sorted(set(k["serves"]) - nets)
    if missing:
        return (False, "Kea runs, but does not serve %s yet." % ", ".join(missing))
    return (True, "Kea serves %s." % ", ".join(sorted(k["serves"])))


# What package managers leave next to a configuration file; dnsmasq's conf-dir skips these (Fedora and
# Debian name them in conf-dir / CONFIG_DIR), and so does Lanvex when it looks at other configuration.
DNSMASQ_SKIPPED = (".rpmnew", ".rpmsave", ".rpmorig", ".dpkg-dist", ".dpkg-old", ".dpkg-new", "~")


# Files the change being planned takes over from another tool (they go with the change): not "other".
TAKING_OVER: set = set()


def dnsmasq_other_files(now: bool = False) -> list:
    """dnsmasq's configuration files other than Lanvex's: the main file and every file in its directory.
    Without the files a takeover replaces, unless `now` (what dnsmasq reads at this moment)."""
    files = [DNSMASQ_CONF]
    if os.path.isdir(DNSMASQ_DIR):
        files += sorted(
            os.path.join(DNSMASQ_DIR, f)
            for f in os.listdir(DNSMASQ_DIR)
            if not f.startswith(".") and not f.endswith(DNSMASQ_SKIPPED)
        )
    return [f for f in files if f != DNSMASQ_FILE and (now or f not in TAKING_OVER) and os.path.isfile(f)]


def dnsmasq_lines(path: str) -> list:
    """(key, value, line) of each setting in a dnsmasq file, without comments."""
    try:
        text = read_text(path)
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            key, _, value = line.partition("=")
            out.append((key.strip(), value.strip(), line))
    return out


def dnsmasq_serves_others() -> list:
    """Other configuration files that have dnsmasq serve DHCP (a restart interrupts that too)."""
    return [
        path for path in dnsmasq_other_files() if any(k == "dhcp-range" for k, _, _ in dnsmasq_lines(path))
    ]


def dnsmasq_others(now: bool = False) -> dict:
    """What other dnsmasq configuration already decides: whether it restricts the interfaces dnsmasq
    answers on, and whether it chooses how dnsmasq binds. `now`: with the files a takeover replaces."""
    listed = local_host = bind = False
    for path in dnsmasq_other_files(now):
        for key, value, _ in dnsmasq_lines(path):
            listed = listed or key in ("interface", "listen-address", "except-interface")
            # local-service=host (Fedora's and RHEL's default) answers on loopback alone and binds its
            # addresses, until an interface= or listen-address= line anywhere ends it.
            local_host = local_host or (key == "local-service" and value == "host")
            bind = bind or key in ("bind-interfaces", "bind-dynamic")
    return {"restricted": listed or local_host, "bind": bind, "loopback_only": local_host and not listed}


def dnsmasq_mode(active: bool) -> tuple:
    """(list interfaces, bind-dynamic) for Lanvex's file. Kept as it is once Lanvex wrote the file, so
    the next change does not flip it; decided the first time from what dnsmasq does already."""
    if os.path.exists(DNSMASQ_FILE):
        lines = [x.strip() for x in read_text(DNSMASQ_FILE).splitlines()]
        return any(x.startswith("interface=") for x in lines), "bind-dynamic" in lines
    others, now = dnsmasq_others(), dnsmasq_others(now=True)
    if active and not now["restricted"]:
        # dnsmasq already answers on every interface for someone else: listing ours would end that.
        # (Taking over: whether the configuration replaced restricted it decides, as dnsmasq runs now.)
        return False, False
    # dnsmasq binds the wildcard address unless told otherwise. Where it runs and binds it already, that
    # stays; where it does not run yet, or binds loopback alone (local-service=host, which Lanvex's
    # interface lines end), it binds only its interfaces' addresses, so it starts next to
    # systemd-resolved.
    wildcard_now = active and not now["bind"] and not now["loopback_only"]
    return True, not others["bind"] and not wildcard_now


def ufw_wanted_rules(iface: str) -> list:
    """The ufw rules that let devices on `iface` use DHCP and DNS on this server, as `ufw show added`
    prints them."""
    return [
        "ufw allow in on %s to any port %s proto %s comment '%s%s'" % (iface, port, proto, UFW_MARK, iface)
        for port, proto in (("67", "udp"), ("53", "udp"), ("53", "tcp"))
    ]


def ufw_state() -> dict | None:
    """None without ufw. Otherwise whether it is active and the rules Lanvex added."""
    if not which("ufw"):
        return None
    status = run(["ufw", "status"], timeout=30)
    added = run(["ufw", "show", "added"], timeout=30)
    ours = [
        line.strip()
        for line in added.stdout.splitlines()
        if line.startswith("ufw ") and ("comment '%s" % UFW_MARK) in line
    ]
    return {"active": status.stdout.splitlines()[:1] == ["Status: active"], "ours": ours}


def nft_drops_input() -> str | None:
    """A table whose input chain drops what no rule allows (a hand-written nftables firewall)."""
    if not which("nft"):
        return None
    r = run(["nft", "-j", "list", "chains"], timeout=15)
    try:
        items = json.loads(r.stdout or "{}").get("nftables") or []
    except ValueError:
        return None
    for item in items:
        chain = item.get("chain") if isinstance(item, dict) else None
        if isinstance(chain, dict) and chain.get("hook") == "input" and chain.get("policy") == "drop":
            return "%s %s" % (chain.get("family"), chain.get("table"))
    return None


# ---------------------------------------------------------------------------------------------------------
# Access rules


def policy_name(iface: str, zone: str) -> str:
    """lxf-<interface>-<4 hex of the egress zone>: unique, and short enough for any firewalld version."""
    return "%s%s-%s" % (POLICY_PREFIX, iface[:8], hashlib.sha256(zone.encode()).hexdigest()[:4])


def default_route_dev() -> str | None:
    r = run(["ip", "-j", "-4", "route", "show", "default"], timeout=15)
    try:
        routes = json.loads(r.stdout or "[]")
    except ValueError:
        return None
    return next((x.get("dev") for x in routes if x.get("dev")), None)


def route_dev(address: str) -> str | None:
    r = run(["ip", "-j", "-4", "route", "get", address], timeout=15)
    try:
        routes = json.loads(r.stdout or "[]")
    except ValueError:
        return None
    return routes[0].get("dev") if routes else None


def firewalld_zone_of(dev: str) -> str:
    r = run(["firewall-cmd", "--get-zone-of-interface=" + dev], timeout=15)
    zone = r.stdout.strip()
    if r.returncode == 0 and zone:
        return zone
    return run(["firewall-cmd", "--get-default-zone"], timeout=15).stdout.strip() or "public"


def forwarding_needed(rules: list) -> bool:
    return any(r["internet"] or r["destinations"] for r in rules)


def egress_of(rules: list, vlans: dict, uplink: str | None) -> dict:
    """For each network's destinations, the interface they leave the server through:
    {interface: [(destination, egress interface)]}. Lanvex networks are known from the request."""
    out = {}
    for r in rules:
        pairs = []
        for d in r["destinations"]:
            net = ipaddress.ip_network(d["network"])
            dev = next(
                (
                    name
                    for name, v in vlans.items()
                    if net.subnet_of(ipaddress.ip_interface(v["addresses"][0]).network)
                ),
                None,
            ) or route_dev(str(net.network_address if net.prefixlen < 32 else net.network_address))
            if dev is None:
                raise Refused("invalid_request", "%s: the server has no route to %s." % (r["interface"], net))
            if dev == "lo":
                raise Refused(
                    "invalid_request",
                    "%s: %s is this server itself; allow it as a service on the server instead."
                    % (r["interface"], net),
                )
            pairs.append((d, dev))
        out[r["interface"]] = pairs
        if r["internet"] and uplink is None:
            raise Refused(
                "invalid_request",
                "%s may reach the Internet, but this server has no default route to send it through."
                % r["interface"],
            )
    return out


def _fw_ports(ports: list) -> list:
    return [(p.split("/")[0], p.split("/")[1]) for p in ports]


def render_policy(
    iface: str, ingress: str, egress: str, rule: dict, dests: list, internet: bool, nat: bool
) -> str:
    """One firewalld policy: what network `iface` (zone `ingress`) may reach in zone `egress`."""
    out = [
        '<?xml version="1.0" encoding="utf-8"?>',
        "<!-- %s -->" % MANAGED,
        '<policy target="%s">' % ("ACCEPT" if internet else "CONTINUE"),
        "  <short>Lanvex: %s to %s</short>" % (iface, egress),
        "  <description>What devices on %s may reach in zone %s.</description>" % (iface, egress),
    ]
    if nat:
        out.append("  <masquerade/>")
    for d in dests:
        if not d["ports"]:
            out += [
                '  <rule family="ipv4" priority="-100">',
                '    <destination address="%s"/>' % d["network"],
                "    <accept/>",
                "  </rule>",
            ]
        for port, proto in _fw_ports(d["ports"]):
            out += [
                '  <rule family="ipv4" priority="-100">',
                '    <destination address="%s"/>' % d["network"],
                '    <port port="%s" protocol="%s"/>' % (port, proto),
                "    <accept/>",
                "  </rule>",
            ]
    if internet:
        # The Internet, not every private network behind the uplink: those only as allowed above.
        for net in PRIVATE4:
            out += [
                '  <rule family="ipv4" priority="0">',
                '    <destination address="%s"/>' % net,
                "    <reject/>",
                "  </rule>",
            ]
    out += ['  <ingress-zone name="%s"/>' % ingress, '  <egress-zone name="%s"/>' % egress, "</policy>"]
    return "\n".join(out) + "\n"


def ufw_access_rules(rule: dict, served: bool, pairs: list, uplink: str | None) -> list:
    """Lanvex's ufw rules for one network, in the order ufw must see them (first match wins)."""
    iface, mark = rule["interface"], "comment '%s%s'" % (UFW_MARK, rule["interface"])
    out = ufw_wanted_rules(iface) if served else []
    for p in rule["services"]:
        port, proto = p.split("/")
        out.append(
            "ufw allow in on %s to any port %s proto %s %s" % (iface, port.replace("-", ":"), proto, mark)
        )
    for d, dev in pairs:
        # As `ufw show added` prints it: one address without /32.
        to = "to %s" % (d["network"][:-3] if d["network"].endswith("/32") else d["network"])
        if not d["ports"]:
            out.append("ufw route allow in on %s out on %s %s %s" % (iface, dev, to, mark))
        for p in d["ports"]:
            port, proto = p.split("/")
            out.append(
                "ufw route allow in on %s out on %s %s port %s proto %s %s"
                % (iface, dev, to, port.replace("-", ":"), proto, mark)
            )
    if rule["internet"]:
        out += ["ufw route deny in on %s out on %s to %s %s" % (iface, uplink, net, mark) for net in PRIVATE4]
        out.append("ufw route allow in on %s out on %s %s" % (iface, uplink, mark))
    return out


def ufw_rule_argv(rule: str, delete: bool = False) -> list:
    words = shlex.split(rule)[1:]  # without the leading "ufw"
    if not delete:
        return ["ufw"] + words
    words = words[: words.index("comment")]
    if words[0] == "route":
        return ["ufw", "route", "delete"] + words[1:]
    return ["ufw", "delete"] + words


def _nft_ports(ports: list, proto: str) -> str:
    chosen = [p.split("/")[0] for p in ports if p.endswith("/" + proto)]
    return "{ %s }" % ", ".join(chosen) if chosen else ""


def render_nft(
    rules: list, pairs: dict, uplink: str | None, served: set, lanvex_ifaces: list, filtering: bool
) -> str:
    """Lanvex's nftables table. filtering: input and forwarding for Lanvex's networks (servers without
    firewalld or ufw); otherwise only masquerading (next to ufw)."""
    family, table = NFT_TABLE
    out = ["# " + MANAGED, "table %s %s" % NFT_TABLE, "delete table %s %s" % NFT_TABLE, ""]
    out.append("table %s %s {" % (family, table))
    out += [
        "  set private4 {",
        "    type ipv4_addr",
        "    flags interval",
        "    elements = { %s }" % ", ".join(PRIVATE4),
        "  }",
        "",
    ]
    if filtering:
        out += ["  chain input {", "    type filter hook input priority filter; policy accept;"]
        out += [
            '    iifname "%s" jump in_%s' % (r["interface"], r["interface"].replace("-", "_")) for r in rules
        ]
        out += ["  }", ""]
        for r in rules:
            name = r["interface"]
            out += [
                "  chain in_%s {" % name.replace("-", "_"),
                "    ct state established,related accept",
                "    meta l4proto icmp accept",
            ]
            if name in served:
                out += ["    udp dport { 53, 67 } accept", "    tcp dport 53 accept"]
            for proto in ("tcp", "udp"):
                ports = _nft_ports(r["services"], proto)
                if ports:
                    out.append("    %s dport %s accept" % (proto, ports))
            out += ["    drop", "  }", ""]
        out += [
            "  chain forward {",
            "    type filter hook forward priority filter; policy accept;",
            "    ct state established,related accept",
        ]
        out += [
            '    iifname "%s" jump fwd_%s' % (r["interface"], r["interface"].replace("-", "_")) for r in rules
        ]
        if lanvex_ifaces:
            # New connections into a Lanvex network only as another Lanvex network's rules allow (above).
            out.append("    oifname { %s } drop" % ", ".join('"%s"' % i for i in lanvex_ifaces))
        out += ["  }", ""]
        for r in rules:
            name = r["interface"]
            out.append("  chain fwd_%s {" % name.replace("-", "_"))
            for d, dev in pairs.get(name, []):
                if not d["ports"]:
                    out.append('    oifname "%s" ip daddr %s accept' % (dev, d["network"]))
                for proto in ("tcp", "udp"):
                    ports = _nft_ports(d["ports"], proto)
                    if ports:
                        out.append(
                            '    oifname "%s" ip daddr %s %s dport %s accept'
                            % (dev, d["network"], proto, ports)
                        )
            if r["internet"]:
                out += ["    ip daddr @private4 drop", '    oifname "%s" accept' % uplink]
            out += ["    drop", "  }", ""]
    nat = nat_pairs(rules, pairs, uplink, lanvex_ifaces)
    if nat:
        out += ["  chain postrouting {", "    type nat hook postrouting priority srcnat; policy accept;"]
        out += ['    iifname "%s" oifname "%s" masquerade' % pair for pair in nat]
        out += ["  }"]
    out.append("}")
    return "\n".join(out) + "\n"


def nat_pairs(rules: list, pairs: dict, uplink: str | None, lanvex_ifaces: list) -> list:
    """(from, out) interface pairs Lanvex masquerades: traffic from its networks leaving towards a network
    that does not route back through this server (anything but another Lanvex network)."""
    found = set()
    for r in rules:
        for _, dev in pairs.get(r["interface"], []):
            if dev not in lanvex_ifaces:
                found.add((r["interface"], dev))
        if r["internet"]:
            found.add((r["interface"], uplink))
    return sorted(found)


def render_nft_unit() -> str:
    nft = which("nft") or "/usr/sbin/nft"
    family, table = NFT_TABLE
    return "\n".join(
        [
            "# " + MANAGED,
            "[Unit]",
            "Description=Lanvex network rules (nftables table %s %s)" % NFT_TABLE,
            "Wants=network-pre.target",
            "Before=network-pre.target",
            "",
            "[Service]",
            "Type=oneshot",
            "RemainAfterExit=yes",
            "ExecStart=%s -f %s" % (nft, NFT_FILE),
            "ExecReload=%s -f %s" % (nft, NFT_FILE),
            "ExecStop=-%s delete table %s %s" % (nft, family, table),
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )


def render_sysctl() -> str:
    return "# %s\n# Devices on Lanvex networks reach beyond this server.\nnet.ipv4.ip_forward = 1\n" % MANAGED


def dns_addresses(rules: list) -> dict:
    """The DNS filter address of each filtered network: 10.99.53.1, .2, … in the order of their names."""
    base = ipaddress.ip_network(DNS_NET).network_address
    names = sorted(r["interface"] for r in rules if r.get("dns_filter") is not None)
    if len(names) > 250:
        raise Refused("invalid_request", "Lanvex filters DNS on at most 250 networks per server.")
    return {name: str(base + i + 1) for i, name in enumerate(names)}


def render_dns_filter(name: str, address: str, upstream: str, f: dict) -> str:
    out = [
        "# " + MANAGED,
        "# DNS filter for the devices on %s; started by lanvex-dns@%s.service. Names it does not block go"
        % (name, name),
        "# to the main dnsmasq on %s, so local names keep working." % upstream,
        "bind-interfaces",
        "listen-address=" + address,
        "port=53",
        "no-dhcp-interface=",
        "no-hosts",
        "no-resolv",
        "server=" + upstream,
        "cache-size=1000",
        "",
    ]
    if f["allow"]:
        out.append("# Exceptions: answered as usual, also below a blocked domain.")
        out += ["server=/%s/%s" % (d, upstream) for d in f["allow"]]
        out.append("")
    if f["block"]:
        out.append("# Blocked, with every name below it.")
        out += ["address=/%s/" % d for d in f["block"]]
        out.append("")
    return "\n".join(out)


def render_dns_env(address: str) -> str:
    return "# %s\nLANVEX_DNS_ADDRESS=%s\n" % (MANAGED, address)


def render_dns_unit() -> str:
    ip, dnsmasq = which("ip") or "/usr/sbin/ip", which("dnsmasq") or "/usr/sbin/dnsmasq"
    return "\n".join(
        [
            "# " + MANAGED,
            "[Unit]",
            "Description=Lanvex DNS filter for %i",
            # Not network-online.target: it brings up its own address, and waiting for every link to
            # be online can take minutes (a bridge without carrier never is).
            "After=network.target dnsmasq.service",
            "ConditionPathExists=%s/%%i.conf" % DNS_DIR,
            "",
            "[Service]",
            "Type=exec",
            "EnvironmentFile=%s/%%i.env" % DNS_DIR,
            # Its address lives on Lanvex's dummy link, which the main dnsmasq leaves alone.
            "ExecStartPre=-%s link add %s type dummy" % (ip, DNS_LINK),
            "ExecStartPre=%s link set %s up" % (ip, DNS_LINK),
            "ExecStartPre=%s address replace ${LANVEX_DNS_ADDRESS}/32 dev %s" % (ip, DNS_LINK),
            "ExecStartPre=%s --test --conf-file=%s/%%i.conf" % (dnsmasq, DNS_DIR),
            "ExecStart=%s -k --conf-file=%s/%%i.conf --pid-file=" % (dnsmasq, DNS_DIR),
            "ExecStopPost=-%s address del ${LANVEX_DNS_ADDRESS}/32 dev %s" % (ip, DNS_LINK),
            "Restart=on-failure",
            "RestartSec=3",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )


def dns_net_clash() -> str | None:
    """An address or route of the server inside Lanvex's DNS filter range (other than its own link)."""
    net = ipaddress.ip_network(DNS_NET)
    r = run(["ip", "-j", "-4", "address", "show"], timeout=15)
    for link in json.loads(r.stdout or "[]"):
        for a in link.get("addr_info") or []:
            if link.get("ifname") != DNS_LINK and ipaddress.ip_address(a["local"]) in net:
                return "%s/%s on %s" % (a["local"], a["prefixlen"], link.get("ifname"))
    r = run(["ip", "-j", "-4", "route", "show"], timeout=15)
    for route in json.loads(r.stdout or "[]"):
        dst = route.get("dst")
        if dst and dst != "default" and route.get("dev") != DNS_LINK:
            try:
                if ipaddress.ip_network(dst if "/" in dst else dst + "/32", strict=False).overlaps(net):
                    return "the route to %s" % dst
            except ValueError:
                continue
    return None


def our_dns_filters() -> list:
    if not os.path.isdir(DNS_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(DNS_DIR) if f.endswith(".conf"))


def dns_query(address: str, name: str, timeout: float = 3.0) -> tuple:
    """(rcode, answers) for an A query, or (None, 0) without an answer."""
    import socket  # noqa: PLC0415 - only for the check
    import struct  # noqa: PLC0415

    tid = struct.unpack(">H", os.urandom(2))[0]
    question = b"".join(bytes([len(p)]) + p.encode("ascii") for p in name.split(".") if p) + b"\0"
    packet = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0) + question + struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(packet, (address, 53))
        data, _ = s.recvfrom(4096)
    except OSError:
        return (None, 0)
    finally:
        s.close()
    if len(data) < 12 or struct.unpack(">H", data[:2])[0] != tid:
        return (None, 0)
    return (struct.unpack(">H", data[2:4])[0] & 0xF, struct.unpack(">H", data[6:8])[0])


def nv_state() -> dict | None:
    """Netzverwaltung's netz.yaml, or None where it is not installed."""
    if not os.path.exists(NV_STATE):
        return None
    try:
        data = _yaml().safe_load(read_text(NV_STATE)) or {}
    except Exception as e:  # noqa: BLE001 - a broken file is reported, not raised through
        raise Refused("unsupported", "%s cannot be read: %s" % (NV_STATE, e)) from e
    if not isinstance(data, dict) or not isinstance(data.get("vlans") or [], list):
        raise Refused("unsupported", "%s is not in the form Netzverwaltung writes." % NV_STATE)
    return data


def nv_artifacts(names: list) -> dict:
    """What Netzverwaltung generated for these networks: {"files": [...], "profiles": [...]}. Its
    hand-kept files (policies without nv-, dnsmasq files without nv-) are never part of it."""
    files = []
    for f in sorted(os.listdir(DNSMASQ_DIR)) if os.path.isdir(DNSMASQ_DIR) else []:
        if f.startswith(NV_PREFIX):
            files.append(os.path.join(DNSMASQ_DIR, f))
    for name in names:
        zone = os.path.join(ZONE_DIR, name + ".xml")
        if os.path.exists(zone):
            files.append(zone)
    for f in sorted(os.listdir(POLICY_DIR)) if os.path.isdir(POLICY_DIR) else []:
        if f.startswith(NV_PREFIX) and f.endswith(".xml"):
            files.append(os.path.join(POLICY_DIR, f))
    profiles = []
    if nm_running():
        for p in nm_profiles():
            if p["name"].startswith(NV_PROFILE) and p["name"][len(NV_PROFILE) :] in names:
                profiles.append(p)
    return {"files": files, "profiles": profiles}


def cmd_inspect(req: dict) -> dict:
    """What another tool has set up for its networks here, for Lanvex to document and take over.
    Changes nothing."""
    if req.get("kind") != "netzverwaltung":
        raise Refused("invalid_request", "Lanvex can take over from: netzverwaltung.")
    state = nv_state()
    if state is None:
        return {"ok": True, "found": False}
    vlans = [v for v in state.get("vlans") or [] if isinstance(v, dict) and isinstance(v.get("name"), str)]
    names = [v["name"] for v in vlans]
    art = nv_artifacts(names)
    policies = {}
    for path in art["files"]:
        if path.startswith(POLICY_DIR):
            policies[os.path.basename(path)[:-4]] = read_text(path)
    return {
        "ok": True,
        "found": True,
        "vlans": vlans,
        "policies": policies,  # the destinations it allowed, with their ports, are in these
        "files": art["files"],
        "profiles": [p["name"] for p in art["profiles"]],
        "units": {u: service_state(u) for u in NV_UNITS},
    }


def our_policies() -> list:
    if not os.path.isdir(POLICY_DIR):
        return []
    return sorted(
        os.path.join(POLICY_DIR, f)
        for f in os.listdir(POLICY_DIR)
        if f.startswith(POLICY_PREFIX) and f.endswith(".xml")
    )


def nm_profiles() -> list:
    """Every stored NetworkManager profile: [{"uuid", "name", "type", "file", "device"}]."""
    r = run(
        ["nmcli", "-t", "-f", "UUID,TYPE,DEVICE,FILENAME,NAME", "connection", "show"], timeout=15, check=True
    )
    out = []
    for line in r.stdout.splitlines():
        parts = nm_split(line)
        if len(parts) >= 5:
            out.append(
                {
                    "uuid": parts[0],
                    "type": parts[1],
                    "device": parts[2],
                    "file": parts[3],
                    "name": ":".join(parts[4:]),
                }
            )
    return out


def nm_interface_name(path: str) -> str | None:
    """interface-name= of a keyfile, if readable."""
    try:
        for line in read_text(path).splitlines():
            if line.strip().startswith("interface-name="):
                return line.split("=", 1)[1].strip()
    except OSError:
        return None
    return None


def dnsmasq_service() -> bool:
    """Is the dnsmasq service installed? NetworkManager depends on the dnsmasq program alone (Debian's and
    Ubuntu's dnsmasq-base), which serves nothing by itself."""
    return bool(which("dnsmasq")) and run(["systemctl", "cat", "dnsmasq.service"], timeout=15).returncode == 0


def service_state(unit: str) -> dict:
    active = run(["systemctl", "is-active", unit], timeout=15).stdout.strip()
    enabled = run(["systemctl", "is-enabled", unit], timeout=15).stdout.strip()
    return {"active": active == "active", "enabled": enabled == "enabled"}


def dnsmasq_reads_dir() -> bool:
    """Does dnsmasq read /etc/dnsmasq.d? Fedora's and Debian's packages both set it up by default."""
    try:
        text = read_text(DNSMASQ_CONF)
    except OSError:
        text = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("conf-dir="):
            first = line.split("=", 1)[1].split(",")[0].strip().rstrip("/")
            if first == DNSMASQ_DIR:
                return True
    try:  # Debian: /etc/default/dnsmasq, CONFIG_DIR=/etc/dnsmasq.d,.dpkg-dist,...
        for line in read_text("/etc/default/dnsmasq").splitlines():
            if line.strip().startswith("CONFIG_DIR=") and DNSMASQ_DIR in line:
                return True
    except OSError:
        pass
    return False


def dnsmasq_conflicts(pools: list) -> list:
    """Other dnsmasq configuration serving the same interface or network: (file, line)."""
    found = []
    ifaces = {p["interface"] for p in pools}
    nets = [ipaddress.ip_network(p["network"]) for p in pools]
    for path in dnsmasq_other_files():
        for key, value, line in dnsmasq_lines(path):
            if key == "interface" and value in ifaces:
                found.append((path, line))
            elif key == "dhcp-range":
                for item in value.split(","):
                    ip = _ip_or_none(item.strip())
                    if ip is not None and any(ip in n for n in nets):
                        found.append((path, line))
                        break
    return found


def nv_takeover(wanted: dict, backend: str) -> dict:
    """Netzverwaltung's networks, files, profiles and services this change replaces."""
    state = nv_state()
    if state is None:
        raise Refused(
            "not_found", "Netzverwaltung is not installed on this server (%s is missing)." % NV_STATE
        )
    names = sorted(v["name"] for v in state.get("vlans") or [] if isinstance(v, dict) and v.get("name"))
    missing = [n for n in names if n not in wanted]
    if missing:
        raise Refused(
            "invalid_request",
            "Take over all of Netzverwaltung's networks in one change; this one lacks %s."
            % ", ".join(missing),
        )
    if backend != "networkmanager":
        raise Refused(
            "unsupported",
            "Netzverwaltung's networks are NetworkManager profiles; this server uses %s." % backend,
        )
    art = nv_artifacts(names)
    # A hand-kept policy that names one of Netzverwaltung's zones would stop working when the zone goes.
    for f in sorted(os.listdir(POLICY_DIR)) if os.path.isdir(POLICY_DIR) else []:
        path = os.path.join(POLICY_DIR, f)
        if not f.endswith(".xml") or f.startswith((NV_PREFIX, POLICY_PREFIX)):
            continue
        for zone in re.findall(r'<(?:ingress|egress)-zone\s+name="([^"]+)"', read_text(path)):
            if zone in names:
                raise Refused(
                    "conflict",
                    "The firewalld policy %s refers to zone %s, which Netzverwaltung made for its network %s. "
                    "Lanvex puts %s in zone %s instead: change that policy to use %s, then take over."
                    % (f[:-4], zone, zone, zone, zone_name(zone), zone_name(zone)),
                )
    units = {u: service_state(u) for u in NV_UNITS}
    return {"names": names, "files": art["files"], "profiles": art["profiles"], "units": units}


def networks_plan(change: dict) -> dict:
    backend = choose_backend(change)
    wanted = {v["name"]: v for v in change["vlans"]}
    takeover = nv_takeover(wanted, backend) if change.get("takeover") == "netzverwaltung" else None
    taken = set(takeover["names"]) if takeover else set()
    TAKING_OVER.clear()
    if takeover:
        TAKING_OVER.update(takeover["files"])
    nm_ours, profiles = {}, []
    if backend == "networkmanager":
        profiles = nm_profiles()
        for p in profiles:
            if p["name"].startswith(LANVEX_PROFILE) and p["file"].startswith(NM_KEYFILE_DIR):
                nm_ours[nm_interface_name(p["file"]) or p["name"][len(LANVEX_PROFILE) :]] = p
        ours = {name: {"active": bool(p["device"])} for name, p in nm_ours.items()}
    elif backend == "netplan":
        own = (netplan_docs().get(NETPLAN_LANVEX) or {}) if os.path.exists(NETPLAN_LANVEX) else {}
        ours = {name: {} for name in ((own.get("network") or {}).get("vlans") or {})}
    else:
        ours = (
            {name: {} for name in ifupdown_stanzas(read_text(IFUPDOWN_LANVEX))}
            if os.path.exists(IFUPDOWN_LANVEX)
            else {}
        )

    links = {}  # parent -> netplan ID
    docs = netplan_docs(exclude=NETPLAN_LANVEX) if backend == "netplan" else {}
    for name, v in wanted.items():
        if not os.path.exists("/sys/class/net/%s" % v["parent"]):
            raise Refused(
                "invalid_request", "%s: there is no interface %s on this server." % (name, v["parent"])
            )
        if v["parent"] in ours:
            raise Refused(
                "unsupported", "%s: its parent %s is a Lanvex VLAN interface." % (name, v["parent"])
            )
        if backend == "netplan":
            found = netplan_definitions(v["parent"], docs)
            if len(found) != 1:
                raise Refused(
                    "unsupported",
                    "%s: Lanvex adds VLANs only on interfaces netplan defines once; %s is defined %d times in %s."
                    % (name, v["parent"], len(found), NETPLAN_DIR),
                )
            links[v["parent"]] = found[0][2]
        if name in ours or name in taken:
            continue
        if backend == "networkmanager":
            other = next(
                (
                    p
                    for p in profiles
                    if p["device"] == name or (p["file"] and nm_interface_name(p["file"]) == name)
                ),
                None,
            )
            if other is not None:
                raise Refused(
                    "conflict",
                    "%s is managed by the NetworkManager profile '%s', which Lanvex did not create. Lanvex does not "
                    "change it; choose another name, or remove that profile first." % (name, other["name"]),
                )
        if backend == "netplan" and netplan_definitions(name, docs):
            raise Refused(
                "conflict", "%s is defined in another netplan file. Lanvex does not change it." % name
            )
        if backend == "ifupdown":
            elsewhere = ifupdown_defined_elsewhere(name)
            if elsewhere:
                raise Refused(
                    "conflict", "%s is defined in %s. Lanvex does not change it." % (name, elsewhere)
                )
        if os.path.exists("/sys/class/net/%s" % name):
            raise Refused(
                "conflict", "%s already exists on this server and was not created by Lanvex." % name
            )
    if backend == "ifupdown" and wanted and not ifupdown_sources_dir():
        raise Refused(
            "unsupported",
            "%s does not read the files in %s. Add the line 'source %s/*' to it first (Debian's default)."
            % (IFUPDOWN_MAIN, IFUPDOWN_DIR, IFUPDOWN_DIR),
        )

    firewalld = service_state("firewalld")["active"] and bool(which("firewall-cmd"))
    # None: DHCP stays as it is. A list (possibly empty): exactly these pools.
    pools = None if change["dhcp"] is None else change["dhcp"]["pools"]
    server = dhcp_server()
    if pools is not None:
        served = {p["interface"] for p in pools}
    elif server == "kea":
        served = kea_pools(read_text(KEA_FILE)) if os.path.exists(KEA_FILE) else set()
    elif os.path.exists(DNSMASQ_FILE):
        served = {
            x.split("=", 1)[1].strip()
            for x in read_text(DNSMASQ_FILE).splitlines()
            if x.startswith("interface=")
        }
    else:
        served = set()

    files, warnings, touched = [], [], []  # touched: VLANs to (re)activate, created or changed

    def add(path: str, label: str, after: str | None) -> str | None:
        exists = os.path.exists(path)
        before = read_text(path) if exists else ""
        if (after is None and not exists) or (exists and after is not None and before == after):
            return None
        action = "delete" if after is None else ("modify" if exists else "create")
        files.append(
            {
                "path": path,
                "label": label,
                "before": before,
                "after": after or "",
                "sha256": sha256_file(path),
                "action": action,
            }
        )
        return action

    vlans = [wanted[n] for n in sorted(wanted)]
    if backend == "networkmanager":
        import uuid as uuidlib  # noqa: PLC0415 - only needed here

        for v in vlans:
            name = v["name"]
            uuid_ = (
                nm_ours[name]["uuid"]
                if name in nm_ours
                else str(uuidlib.uuid5(uuidlib.NAMESPACE_URL, "lanvex:vlan:" + name))
            )
            path = (
                nm_ours[name]["file"]
                if name in nm_ours
                else os.path.join(NM_KEYFILE_DIR, LANVEX_PROFILE + name + ".nmconnection")
            )
            if add(
                path,
                "NetworkManager profile for %s (VLAN %d on %s)" % (name, v["id"], v["parent"]),
                render_vlan_keyfile(v, uuid_, zone_name(name) if firewalld else None),
            ):
                touched.append(name)
        for name, p in sorted(nm_ours.items()):
            if name not in wanted:
                add(p["file"], "NetworkManager profile for %s (removed)" % name, None)
    elif backend == "netplan":
        old = (
            ((netplan_docs().get(NETPLAN_LANVEX) or {}).get("network") or {}).get("vlans") or {}
            if os.path.exists(NETPLAN_LANVEX)
            else {}
        )
        touched = [
            v["name"]
            for v in vlans
            if old.get(v["name"])
            != {"id": v["id"], "link": links[v["parent"]], "addresses": list(v["addresses"])}
        ]
        add(
            NETPLAN_LANVEX,
            "netplan file with Lanvex's VLAN interfaces",
            render_netplan_vlans(vlans, links) if vlans else None,
        )
    else:
        old_stanzas = ifupdown_stanzas(read_text(IFUPDOWN_LANVEX)) if os.path.exists(IFUPDOWN_LANVEX) else {}
        new_stanzas = ifupdown_stanzas(render_ifupdown_vlans(vlans))
        touched = [v["name"] for v in vlans if old_stanzas.get(v["name"]) != new_stanzas.get(v["name"])]
        add(
            IFUPDOWN_LANVEX,
            "ifupdown file with Lanvex's VLAN interfaces",
            render_ifupdown_vlans(vlans) if vlans else None,
        )
    removed = sorted(name for name in ours if name not in wanted)
    created = sorted(name for name in wanted if name not in ours)
    if takeover:
        # Netzverwaltung's own files for these networks go with this change (a rollback brings them back).
        for p in takeover["profiles"]:
            add(p["file"], "Netzverwaltung's NetworkManager profile %s (taken over)" % p["name"], None)
        for path in takeover["files"]:
            add(path, "Netzverwaltung's %s (taken over)" % os.path.basename(path), None)
    # Access rules: as requested, or as last applied (access is null), for every Lanvex network.
    manifest_now = read_manifest()
    if change.get("access") is None:
        applied = {r.get("interface"): r for r in manifest_now.get("access") or [] if isinstance(r, dict)}
    else:
        applied = {r["interface"]: r for r in change["access"]["rules"]}
    rules = [applied.get(n) or default_rule(n) for n in sorted(wanted)]
    by_name = {r["interface"]: r for r in rules}
    uplink = default_route_dev()
    pairs = egress_of(rules, wanted, uplink)
    forwarding = forwarding_needed(rules)
    if firewalld:
        for v in vlans:
            add(
                os.path.join(ZONE_DIR, zone_name(v["name"]) + ".xml"),
                "firewalld zone %s" % zone_name(v["name"]),
                render_zone(
                    v,
                    ["dhcp", "dns"] if v["name"] in served else [],
                    bind=backend != "networkmanager",
                    ports=by_name[v["name"]]["services"],
                ),
            )
        for name in removed:
            add(
                os.path.join(ZONE_DIR, zone_name(name) + ".xml"),
                "firewalld zone %s (removed)" % zone_name(name),
                None,
            )
    known_before = read_manifest().get("dnsmasq_before")
    if not isinstance(known_before, dict) or not os.path.exists(DNSMASQ_FILE):
        known_before = None
    dnsmasq_before = known_before

    # DNS filters: one dnsmasq per filtered network, on Lanvex's dummy link.
    filters = dns_addresses(rules)
    filters_before = our_dns_filters()
    if server == "kea" and (filters or takeover):
        raise Refused(
            "unsupported",
            "Kea serves DHCP here; DNS filters and taking over from Netzverwaltung need dnsmasq.",
        )
    if filters:
        clash = dns_net_clash()
        if clash:
            raise Refused(
                "conflict",
                "Lanvex's DNS filters use %s on this server, and %s is in it. Remove that first."
                % (DNS_NET, clash),
            )
        unserved = sorted(n for n in filters if n not in served)
        if unserved:
            raise Refused(
                "invalid_request",
                "A DNS filter needs Lanvex's DHCP on the network (devices learn the filter as their DNS server "
                "from it): %s has no DHCP pool." % ", ".join(unserved),
            )
    if pools is None and (filters or filters_before):
        # The filters' addresses go out with DHCP: a change to them needs the pools to write again.
        if sorted(filters) != filters_before or any(
            read_text(os.path.join(DNS_DIR, n + ".env")).strip().split("=")[-1] != a
            for n, a in filters.items()
            if os.path.exists(os.path.join(DNS_DIR, n + ".env"))
        ):
            raise Refused("invalid_request", "Send the DHCP pools along with a change to the DNS filters.")
    for name, address in sorted(filters.items()):
        upstream = str(ipaddress.ip_interface(wanted[name]["addresses"][0]).ip)
        add(
            os.path.join(DNS_DIR, name + ".conf"),
            "DNS filter for %s" % name,
            render_dns_filter(name, address, upstream, by_name[name]["dns_filter"]),
        )
        add(os.path.join(DNS_DIR, name + ".env"), "its address (%s)" % address, render_dns_env(address))
    for name in filters_before:
        if name not in filters:
            add(os.path.join(DNS_DIR, name + ".conf"), "DNS filter for %s (removed)" % name, None)
            add(os.path.join(DNS_DIR, name + ".env"), "its address (removed)", None)
    add(DNS_UNIT, "systemd unit for the DNS filters", render_dns_unit() if filters else None)
    dns_start = sorted(
        n for n in filters if any(f["path"].startswith(os.path.join(DNS_DIR, n + ".")) for f in files)
    )
    dns_stop = sorted(n for n in filters_before if n not in filters)

    dnsmasq, kea = None, None
    if pools is not None and server == "kea":
        setup = kea_setup()
        if not setup["unit"]:
            raise Refused("missing_tool", "Kea (kea-dhcp4) is not installed.")
        listens = setup["interfaces"] or []
        deaf = sorted(p["interface"] for p in pools if "*" not in listens and p["interface"] not in listens)
        if deaf:
            raise Refused(
                "unsupported",
                'Kea does not listen on %s. Add %s (or "*") to interfaces-config in %s first.'
                % (", ".join(deaf), "it" if len(deaf) == 1 else "them", KEA_CONF),
            )
        state = service_state(setup["unit"])
        kea = dict(state, unit=setup["unit"], wanted=bool(pools), serves=sorted(served))
        add(KEA_FILE, "Kea: DHCP for the Lanvex networks", render_kea(pools))
        own_dns = sorted({d for p in pools for d in p["dns"] if d == p["router"]})
        if own_dns:
            warnings.append(
                "Kea serves DHCP only: devices get %s as their DNS server, so a DNS server must answer there."
                % ", ".join(own_dns)
            )
    elif pools is not None:
        if pools:
            if not dnsmasq_service():
                raise Refused(
                    "missing_tool",
                    "The dnsmasq service is not installed (at most the dnsmasq program NetworkManager uses). Install "
                    "the package dnsmasq (dnf install dnsmasq, or apt install dnsmasq), then prepare the change again.",
                )
            if not dnsmasq_reads_dir():
                raise Refused(
                    "unsupported",
                    "dnsmasq does not read %s. Add the line conf-dir=%s to %s first."
                    % (DNSMASQ_DIR, DNSMASQ_DIR, DNSMASQ_CONF),
                )
            clash = dnsmasq_conflicts(pools)
            if clash:
                raise Refused(
                    "conflict",
                    "Another dnsmasq configuration already serves this: %s (%s). Lanvex does not change it."
                    % (clash[0][0], clash[0][1]),
                )
        state = service_state("dnsmasq") if dnsmasq_service() else None
        if pools and not os.path.exists(DNSMASQ_FILE) and state is not None:
            dnsmasq_before = {"active": state["active"], "enabled": state["enabled"]}
        elif not pools:
            dnsmasq_before = None
        listed, bind_dynamic = dnsmasq_mode(bool(state and state["active"]))
        if filters and not bind_dynamic and not dnsmasq_others()["bind"]:
            # dnsmasq binds the wildcard address unless told otherwise, which would keep the filters'
            # addresses busy; bind-dynamic binds each interface's addresses instead, the same interfaces.
            bind_dynamic = True
        if pools and not listed:
            warnings.append(
                "dnsmasq answers on every interface of this server for another configuration; Lanvex adds its pools "
                "without restricting that."
            )
        add(
            DNSMASQ_FILE,
            "dnsmasq: DHCP for the Lanvex networks",
            render_dnsmasq(pools, listed, bind_dynamic, filters) if pools else None,
        )
        if state is not None:
            # Lanvex stops serving DHCP here: dnsmasq goes back to how it ran before (unknown for
            # pools Lanvex 0.7 added: then it keeps running).
            put_back = known_before if not pools and os.path.exists(DNSMASQ_FILE) else None
            dnsmasq = dict(state, wanted=bool(pools), put_back=put_back)

    # Firewall: firewalld policies; or ufw rules (marked lanvex:<interface>) with masquerading in Lanvex's
    # own nftables table; or, without either, that table filters Lanvex's networks itself.
    ufw = ufw_state()
    ufw_on = ufw is not None and (ufw["active"] or bool(ufw["ours"]))
    fw = "firewalld" if firewalld else ("ufw" if ufw_on else "nftables")
    policies = {}
    if firewalld:
        uplink_zone = firewalld_zone_of(uplink) if uplink else None
        for r in rules:
            name, zones = r["interface"], {}
            for d, dev in pairs[name]:
                zones.setdefault(zone_name(dev) if dev in wanted else firewalld_zone_of(dev), []).append(d)
            if r["internet"]:
                zones.setdefault(uplink_zone, [])
            for zone, dests in sorted(zones.items()):
                lanvex_zone = zone.startswith(ZONE_PREFIX) and any(zone == zone_name(n) for n in wanted)
                policies[os.path.join(POLICY_DIR, policy_name(name, zone) + ".xml")] = render_policy(
                    name,
                    zone_name(name),
                    zone,
                    r,
                    dests,
                    internet=r["internet"] and zone == uplink_zone,
                    nat=not lanvex_zone,
                )
        for path, text in sorted(policies.items()):
            add(path, "firewalld policy %s" % os.path.basename(path)[:-4], text)
        for path in our_policies():
            if path not in policies:
                add(path, "firewalld policy %s (removed)" % os.path.basename(path)[:-4], None)

    ufw_wanted, ufw_add, ufw_delete = {}, [], []
    if fw == "ufw":
        for r in rules:
            ufw_wanted[r["interface"]] = ufw_access_rules(
                r, r["interface"] in served, pairs[r["interface"]], uplink
            )
        current = {}
        for rule in ufw["ours"]:
            current.setdefault(rule.rsplit("'", 2)[-2][len(UFW_MARK) :], []).append(rule)
        for name in sorted(set(current) | set(ufw_wanted)):
            if current.get(name, []) != ufw_wanted.get(name, []):
                # ufw takes the first rule that matches: replace the network's rules in their order.
                ufw_delete += current.get(name, [])
                ufw_add += ufw_wanted.get(name, [])

    nat = nat_pairs(rules, pairs, uplink, sorted(wanted))
    nft_text = None
    if fw == "nftables" and wanted:
        if which("nft"):
            nft_text = render_nft(rules, pairs, uplink, served, sorted(wanted), filtering=True)
        elif forwarding:
            raise Refused(
                "missing_tool",
                "Without firewalld or ufw, Lanvex keeps its networks' rules in its own nftables table, and nft is "
                "not installed. Install the package nftables, then prepare the change again.",
            )
        else:
            warnings.append(
                "This server has no firewall Lanvex can use (firewalld, ufw, or nftables): devices on Lanvex "
                "networks reach every service on it. Install nftables so Lanvex can limit that."
            )
        hand = nft_drops_input()
        if hand and served:
            warnings.append(
                "nftables on this server drops incoming traffic no rule allows (table %s). Lanvex's own table "
                "cannot override that: allow DHCP (UDP 67) and DNS (port 53) on Lanvex's networks there too."
                % hand
            )
    elif fw == "ufw" and nat:
        if not which("nft"):
            raise Refused(
                "missing_tool",
                "ufw cannot masquerade by command, so Lanvex does it in its own nftables table, and nft is not "
                "installed. Install the package nftables, then prepare the change again.",
            )
        nft_text = render_nft(rules, pairs, uplink, served, sorted(wanted), filtering=False)
    nft_before = os.path.exists(NFT_FILE)
    add(NFT_FILE, "Lanvex's nftables table (%s %s)" % NFT_TABLE, nft_text)
    add(NFT_UNIT, "systemd unit that loads it at boot", render_nft_unit() if nft_text else None)

    forward_now = read_text(FORWARD_KEY).strip() if os.path.exists(FORWARD_KEY) else "0"
    forward_before = manifest_now.get("forward_before") if os.path.exists(SYSCTL_FILE) else None
    if forwarding and forward_before is None:
        forward_before = forward_now
    add(SYSCTL_FILE, "IP forwarding for Lanvex's networks", render_sysctl() if forwarding else None)
    set_forward = None
    if forwarding and forward_now != "1":
        set_forward = "1"
    elif not forwarding and os.path.exists(SYSCTL_FILE):
        if manifest_now.get("forward_before") == "0" and forward_now == "1":
            set_forward = "0"  # back to how it was before Lanvex switched it on
    if not forwarding:
        forward_before = None

    # Removed with the last VLAN interface.
    # Only the rules that differ from the default (DHCP and DNS on the server, nothing else).
    access_record = [by_name[n] for n in sorted(wanted) if by_name[n] != default_rule(n)] or None
    manifest = (
        render_manifest(backend, list(wanted), dnsmasq_before, access_record, forward_before, server)
        if wanted
        else None
    )
    add(MANIFEST, "Lanvex's list of the VLAN interfaces it manages here", manifest)

    commands = []
    if any(f["path"].startswith((ZONE_DIR, POLICY_DIR)) for f in files):
        commands += ["firewall-cmd --check-config", "firewall-cmd --reload"]
    vlan_changed = bool(touched or removed)
    if backend == "networkmanager" and vlan_changed:
        commands.append("nmcli connection reload")
        commands += ["nmcli connection up id %s%s" % (LANVEX_PROFILE, name) for name in touched]
    elif backend == "netplan" and vlan_changed:
        commands += ["ip link delete %s" % n for n in removed]
        commands += ["netplan generate", "netplan apply"]
    elif backend == "ifupdown" and vlan_changed:
        commands = ["ifdown %s" % n for n in sorted(set(removed) | (set(touched) - set(created)))] + commands
        commands += ["ifup %s" % n for n in touched]
    dnsmasq_file_changed = any(
        f["path"] == DNSMASQ_FILE or (f["path"].startswith(DNSMASQ_DIR + "/") and f["path"] in TAKING_OVER)
        for f in files
    )
    nv_stop = [
        u for u, st in sorted((takeover or {}).get("units", {}).items()) if st["active"] or st["enabled"]
    ]
    commands = ["systemctl disable --now " + u for u in nv_stop] + commands
    put_back = (dnsmasq or {}).get("put_back") or {}
    stop_dnsmasq = bool(put_back) and not put_back.get("active") and dnsmasq["active"]
    disable_dnsmasq = bool(put_back) and not put_back.get("enabled") and dnsmasq["enabled"]
    restart_dnsmasq = (
        dnsmasq is not None
        and (dnsmasq_file_changed or (vlan_changed and bool(served)))
        and (dnsmasq["wanted"] or dnsmasq["active"])
        and not stop_dnsmasq
    )
    if kea is None and server == "kea" and vlan_changed and served:
        # Pools unchanged, VLANs changed: Kea finds the interfaces again when it reloads.
        unit = kea_unit()
        if unit:
            kea = dict(service_state(unit), unit=unit, wanted=True, serves=sorted(served))
    reload_kea = kea is not None and (
        any(f["path"] == KEA_FILE for f in files) or (vlan_changed and bool(served))
    )

    commands += [" ".join(shlex.quote(w) for w in ufw_rule_argv(r, delete=True)) for r in ufw_delete]
    commands += [" ".join(shlex.quote(w) for w in ufw_rule_argv(r)) for r in ufw_add]
    nft_changed = any(f["path"] == NFT_FILE for f in files)
    unit_changed = any(f["path"] in (NFT_UNIT, DNS_UNIT) for f in files)
    if unit_changed:
        commands.append("systemctl daemon-reload")
    if nft_changed and nft_text:
        commands += ["nft -f " + NFT_FILE, "systemctl enable " + NFT_UNIT_NAME]
    elif nft_changed:
        commands += ["nft delete table %s %s" % NFT_TABLE, "systemctl disable " + NFT_UNIT_NAME]
    if set_forward is not None:
        commands.append("sysctl -w net.ipv4.ip_forward=" + set_forward)
    if dnsmasq is not None and dnsmasq["wanted"] and not dnsmasq["enabled"]:
        commands.append("systemctl enable dnsmasq")
        warnings.append("dnsmasq is not enabled on this server yet; Lanvex enables and starts it.")
    if restart_dnsmasq or (dnsmasq and dnsmasq["wanted"] and not dnsmasq["active"]):
        commands.append("systemctl restart dnsmasq")
        others = dnsmasq_serves_others() if dnsmasq["active"] else []
        if others:
            warnings.append(
                "dnsmasq also serves DHCP for other configuration here (%s). Restarting it interrupts DHCP and "
                "DNS for those networks too, for a moment; devices keep their addresses." % ", ".join(others)
            )
    if kea is not None and kea["wanted"] and not kea["enabled"]:
        commands.append("systemctl enable %s" % kea["unit"])
    if reload_kea or (kea is not None and kea["wanted"] and not kea["active"]):
        commands.insert(0, "kea-dhcp4 -t %s" % KEA_CONF)
        commands.append("systemctl reload-or-restart %s" % kea["unit"])
    if disable_dnsmasq:
        commands.append("systemctl disable dnsmasq")
    if stop_dnsmasq:
        commands.append("systemctl stop dnsmasq")
    # Filters that go stop first: the main dnsmasq may go back to binding every address, theirs too.
    commands = ["systemctl disable --now lanvex-dns@%s" % n for n in dns_stop] + commands
    for n in dns_start:
        commands += ["systemctl enable lanvex-dns@%s" % n, "systemctl restart lanvex-dns@%s" % n]
    if stop_dnsmasq or disable_dnsmasq:
        warnings.append(
            "Lanvex no longer serves DHCP on this server: dnsmasq is %s, as it was before Lanvex's first pool."
            % " and ".join(w for w, on in (("stopped", stop_dnsmasq), ("disabled", disable_dnsmasq)) if on)
        )
    if pools and not firewalld and service_state("firewalld")["enabled"]:
        warnings.append("firewalld is enabled but not running; Lanvex writes no zones.")
    if removed:
        warnings.append(
            "Removing a VLAN interface disconnects every device on that network from this server."
        )
    return {
        "backend": backend,
        "files": files,
        "commands": commands,
        "warnings": warnings,
        "_write": {f["path"]: (f["after"] if f["action"] != "delete" else None) for f in files},
        "_changed": bool(files or ufw_add or ufw_delete or set_forward)
        or (dnsmasq is not None and dnsmasq["wanted"] and not (dnsmasq["active"] and dnsmasq["enabled"]))
        or (kea is not None and kea["wanted"] and not (kea["active"] and kea["enabled"])),
        # Files another tool writes that the change alters through it; backed up for a rollback.
        "_backup": [f for f in UFW_FILES if os.path.exists(f)] if (ufw_add or ufw_delete) else [],
        "_networks": {
            "backend": backend,
            "firewalld": firewalld,
            "dnsmasq": dnsmasq,
            "stop_dnsmasq": stop_dnsmasq,
            "disable_dnsmasq": disable_dnsmasq,
            "restart_dnsmasq": restart_dnsmasq
            or bool(dnsmasq and dnsmasq["wanted"] and not dnsmasq["active"]),
            "kea": kea,
            "reload_kea": reload_kea or bool(kea and kea["wanted"] and not kea["active"]),
            "up": touched,
            "created": created,
            "removed": removed,
            "ours_before": sorted(ours),
            "active_before": [p["uuid"] for p in nm_ours.values() if p["device"]]
            + [p["uuid"] for p in (takeover or {}).get("profiles", []) if p["device"]],
            "nv_units": {u: (takeover or {})["units"][u] for u in nv_stop},
            "ufw": {"add": ufw_add, "delete": ufw_delete} if (ufw_add or ufw_delete) else None,
            "ufw_check": ufw_wanted if ufw is not None and ufw["active"] else {},
            "fw": fw,
            "policies": sorted(os.path.basename(p)[:-4] for p in policies),
            "nft": ("load" if nft_text else "delete") if nft_changed else None,
            "nft_expected": bool(nft_text),
            "nft_before": nft_before,
            "unit_changed": unit_changed,
            "unit_enabled_before": service_state(NFT_UNIT_NAME)["enabled"] if unit_changed else None,
            "dns": {
                "filters": filters,
                "start": dns_start,
                "stop": dns_stop,
                "before": filters_before,
                # A blocked domain per filter, for the check after applying.
                "probes": {n: (by_name[n]["dns_filter"]["block"] or [None])[0] for n in filters},
            },
            "forwarding": forwarding,
            "forward_now": forward_now,
            "set_forward": set_forward,
        },
    }


def nft_validate(text: str) -> None:
    """nft -c: the kernel checks the whole table without loading it."""
    fd, path = tempfile.mkstemp(prefix="lanvex-nft-", suffix=".nft")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        r = run([which("nft") or "nft", "-c", "-f", path], timeout=60)
        if r.returncode != 0:
            raise Refused(
                "rejected", "nft rejected Lanvex's table: %s" % (r.stderr or r.stdout).strip()[:800]
            )
    finally:
        os.unlink(path)


def dnsmasq_validate(text: str) -> None:
    """dnsmasq --test against the new file on its own: catches syntax errors before anything changes."""
    if not text:
        return
    fd, tmp = tempfile.mkstemp(prefix="lanvex-dnsmasq-", suffix=".conf")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        r = run([which("dnsmasq") or "dnsmasq", "--test", "--conf-file=" + tmp], timeout=30)
        if r.returncode != 0:
            raise Refused(
                "rejected",
                "dnsmasq rejected the new configuration: %s" % (r.stderr or r.stdout).strip()[:800],
            )
    finally:
        os.unlink(tmp)


def _write_files(write: dict) -> None:
    for path, text in write.items():
        if text is None:
            if os.path.exists(path):
                os.unlink(path)
            continue
        if path.startswith(NM_KEYFILE_DIR) or path.startswith(NETPLAN_DIR):
            mode = 0o600  # NetworkManager and netplan refuse configuration files others can read
        else:
            mode = 0o644
        os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
        atomic_write(path, text.encode("utf-8"), mode, 0, 0)


def _delete_links(names: list) -> None:
    for name in names:
        if os.path.exists("/sys/class/net/%s" % name):
            run(["ip", "link", "delete", name], timeout=30)


def networks_apply(plan: dict) -> None:
    n = plan["_networks"]
    backend = n["backend"]
    for unit in n.get("nv_units") or {}:
        # Netzverwaltung stops before its files go (taking over from it).
        run(["systemctl", "disable", "--now", unit], timeout=60, check=True)
    for name in (n.get("dns") or {}).get("stop") or []:
        # Before its unit file goes, and before the main dnsmasq may bind every address again.
        run(["systemctl", "disable", "--now", "lanvex-dns@" + name], timeout=60)
    if backend == "ifupdown":
        # Down with the configuration it came up with, before the file changes.
        for name in sorted(set(n["removed"]) | (set(n["up"]) - set(n["created"]))):
            run(["ifdown", name], timeout=60)
        _delete_links(n["removed"])
    _write_files(plan["_write"])
    if any(p.startswith((ZONE_DIR, POLICY_DIR)) for p in plan["_write"]):
        # A configuration firewalld rejects would leave the server without a firewall: check it first.
        run(["firewall-cmd", "--check-config"], timeout=60, check=True)
        run(["firewall-cmd", "--reload"], timeout=60, check=True)
    if backend == "networkmanager" and (n["up"] or n["removed"]):
        run(["nmcli", "connection", "reload"], timeout=30, check=True)
        for name in n["up"]:
            run(["nmcli", "connection", "up", "id", LANVEX_PROFILE + name], timeout=90, check=True)
    elif backend == "netplan" and (n["up"] or n["removed"]):
        # netplan apply leaves VLAN devices it no longer defines, and cannot change a VLAN's ID or parent.
        _delete_links(sorted(set(n["removed"]) | (set(n["up"]) - set(n["created"]))))
        netplan_apply()
    elif backend == "ifupdown":
        for name in n["up"]:
            run(["ifup", name], timeout=90, check=True)
    u = n.get("ufw")
    if u is not None:
        for rule in u["delete"]:
            run(ufw_rule_argv(rule, delete=True), timeout=60, check=True)
        for rule in u["add"]:
            run(ufw_rule_argv(rule), timeout=60, check=True)
    if n.get("unit_changed"):
        run(["systemctl", "daemon-reload"], timeout=60, check=True)
    if n.get("nft") == "load":
        run([which("nft") or "nft", "-f", NFT_FILE], timeout=60, check=True)
        run(["systemctl", "enable", NFT_UNIT_NAME], timeout=30, check=True)
    elif n.get("nft") == "delete":
        run([which("nft") or "nft", "delete", "table"] + list(NFT_TABLE), timeout=60)
        run(["systemctl", "disable", NFT_UNIT_NAME], timeout=30)
    if n.get("set_forward") is not None:
        # Last, once the rules for what may be forwarded are in place.
        run(["sysctl", "-w", "net.ipv4.ip_forward=" + n["set_forward"]], timeout=15, check=True)
    d = n["dnsmasq"]
    if d is not None and d["wanted"] and not d["enabled"]:
        run(["systemctl", "enable", "dnsmasq"], timeout=30, check=True)
    if n["restart_dnsmasq"]:
        run(["systemctl", "restart", "dnsmasq"], timeout=60, check=True)
    if n.get("disable_dnsmasq"):
        run(["systemctl", "disable", "dnsmasq"], timeout=30, check=True)
    if n.get("stop_dnsmasq"):
        run(["systemctl", "stop", "dnsmasq"], timeout=60, check=True)
    k = n.get("kea")
    if k is not None and k["wanted"] and not k["enabled"]:
        run(["systemctl", "enable", k["unit"]], timeout=30, check=True)
    if n.get("reload_kea"):
        run(["systemctl", "reload-or-restart", k["unit"]], timeout=60, check=True)
    dns = n.get("dns") or {}
    for name in dns.get("start") or []:
        run(["systemctl", "enable", "lanvex-dns@" + name], timeout=30, check=True)
        run(["systemctl", "restart", "lanvex-dns@" + name], timeout=60, check=True)
    if not dns.get("filters") and dns.get("before") and os.path.exists("/sys/class/net/" + DNS_LINK):
        run(["ip", "link", "delete", DNS_LINK], timeout=15)


def networks_before_restore(state: dict) -> None:
    """Before the old files come back: take down what the change brought up, with the configuration
    it brought it up with (ifupdown needs that)."""
    n = state["networks"]
    for name in (n.get("dns") or {}).get("start") or []:
        # Started or changed by this change: stopped before the main dnsmasq goes back to how it was.
        run(["systemctl", "stop", "lanvex-dns@" + name], timeout=60)
    if n["backend"] == "ifupdown":
        for name in n["up"]:
            run(["ifdown", name], timeout=60)
        _delete_links(n["up"])


def networks_restore(state: dict) -> None:
    """After the files are back: make NetworkManager, netplan or ifupdown, firewalld and dnsmasq use them."""
    n = state["networks"]
    backend = n.get("backend", "networkmanager")
    paths = [r["path"] for r in state.get("files") or []]
    if n.get("set_forward") is not None:
        # First, before the rules that limited forwarding go.
        run(["sysctl", "-w", "net.ipv4.ip_forward=" + n["forward_now"]], timeout=15)
    if any(p.startswith((ZONE_DIR, POLICY_DIR)) for p in paths) and which("firewall-cmd"):
        run(["firewall-cmd", "--reload"], timeout=60)
    if n.get("unit_changed"):
        run(["systemctl", "daemon-reload"], timeout=60)
        run(["systemctl", "enable" if n.get("unit_enabled_before") else "disable", NFT_UNIT_NAME], timeout=30)
    if n.get("nft") is not None and which("nft"):
        if os.path.exists(NFT_FILE):
            run([which("nft") or "nft", "-f", NFT_FILE], timeout=60)
        else:
            run([which("nft") or "nft", "delete", "table"] + list(NFT_TABLE), timeout=60)
    if backend == "networkmanager" and any(p.startswith(NM_KEYFILE_DIR) for p in paths):
        run(["nmcli", "connection", "reload"], timeout=30)
        for uuid_ in n.get("active_before") or []:
            run(["nmcli", "connection", "up", uuid_], timeout=90)
    elif backend == "netplan" and NETPLAN_LANVEX in paths:
        _delete_links(n.get("up") or [])  # created or changed by the change; netplan re-creates the old ones
        try:
            netplan_apply()
        except Refused:
            pass
    elif backend == "ifupdown":
        for name in n.get("ours_before") or []:
            if name in (n.get("up") or []) or name in (n.get("removed") or []):
                run(["ifup", name], timeout=90)
    if n.get("ufw") is not None and which("ufw"):
        run(["ufw", "reload"], timeout=60)  # the restored rule files
    d = n.get("dnsmasq")
    if d is not None:
        # Both ways: the change may have enabled it (first pool) or disabled it (last pool removed).
        run(["systemctl", "enable" if d["enabled"] else "disable", "dnsmasq"], timeout=30)
        if d["active"]:
            run(["systemctl", "restart", "dnsmasq"], timeout=60)
        else:
            run(["systemctl", "stop", "dnsmasq"], timeout=60)
    k = n.get("kea")
    if k is not None:
        run(["systemctl", "enable" if k["enabled"] else "disable", k["unit"]], timeout=30)
        run(["systemctl", "reload-or-restart" if k["active"] else "stop", k["unit"]], timeout=60)
    dns = n.get("dns") or {}
    for name in sorted(set(dns.get("filters") or {}) | set(dns.get("before") or [])):
        if name not in (dns.get("start") or []) and name not in (dns.get("stop") or []):
            continue  # untouched by the change
        if os.path.exists(os.path.join(DNS_DIR, name + ".conf")):
            run(["systemctl", "enable", "lanvex-dns@" + name], timeout=30)
            run(["systemctl", "restart", "lanvex-dns@" + name], timeout=60)
        else:
            run(["systemctl", "disable", "--now", "lanvex-dns@" + name], timeout=60)
    for unit, st in sorted((n.get("nv_units") or {}).items()):
        # Netzverwaltung as it ran before the takeover.
        if st["enabled"]:
            run(["systemctl", "enable", unit], timeout=30)
        if st["active"]:
            run(["systemctl", "start", unit], timeout=60)
    if not our_dns_filters() and os.path.exists("/sys/class/net/" + DNS_LINK):
        run(["ip", "link", "delete", DNS_LINK], timeout=15)


def link_info(name: str) -> dict | None:
    r = run(["ip", "-details", "-json", "link", "show", "dev", name], timeout=15)
    if r.returncode != 0:
        return None
    links = json.loads(r.stdout or "[]")
    return links[0] if links else None


def networks_verify(st: dict) -> list:
    change, n = st["change"], st["networks"]
    checks = []
    for v in change["vlans"]:
        name = v["name"]

        def present(v=v, name=name):
            link = link_info(name)
            if link is None:
                return (False, "%s does not exist." % name)
            data = (link.get("linkinfo") or {}).get("info_data") or {}
            if data.get("id") != v["id"] or link.get("link") != v["parent"]:
                return (
                    False,
                    "%s is VLAN %s on %s, not VLAN %d on %s."
                    % (name, data.get("id"), link.get("link"), v["id"], v["parent"]),
                )
            got = sorted(a["address"] for a in ipv4_addresses(name) if not a["dynamic"])
            want = sorted(v["addresses"])
            return (
                got == want,
                "VLAN %d on %s, addresses %s." % (v["id"], v["parent"], ", ".join(got) or "none"),
            )

        ok, detail = _wait_for(present, 20)
        checks.append(
            {
                "id": "vlan:" + name,
                "label": "%s (VLAN %d on %s)" % (name, v["id"], v["parent"]),
                "ok": ok,
                "detail": detail,
            }
        )
        if n.get("firewalld"):
            zone = run(["firewall-cmd", "--get-zone-of-interface=" + name], timeout=15).stdout.strip()
            checks.append(
                {
                    "id": "zone:" + name,
                    "label": "firewalld zone of %s" % name,
                    "ok": zone == zone_name(name),
                    "detail": "In zone %s." % (zone or "none"),
                }
            )
    for name in n.get("removed") or []:
        gone = link_info(name) is None
        checks.append(
            {
                "id": "removed:" + name,
                "label": "%s removed" % name,
                "ok": gone,
                "detail": "Gone." if gone else "Still present.",
            }
        )
    if n.get("ufw_check"):
        added = run(["ufw", "show", "added"], timeout=30).stdout.splitlines()
        for name, wanted_rules in sorted(n["ufw_check"].items()):
            if not wanted_rules:
                continue
            missing = [r for r in wanted_rules if r not in added]
            checks.append(
                {
                    "id": "ufw:" + name,
                    "label": "ufw rules for %s" % name,
                    "ok": not missing,
                    "detail": "%d rules present." % len(wanted_rules)
                    if not missing
                    else "Missing: %s" % "; ".join(missing),
                }
            )
    if n.get("policies"):
        loaded = set(run(["firewall-cmd", "--get-policies"], timeout=15).stdout.split())
        missing = [p for p in n["policies"] if p not in loaded]
        checks.append(
            {
                "id": "policies",
                "label": "firewalld policies for what Lanvex's networks may reach",
                "ok": not missing,
                "detail": "Loaded: %s." % ", ".join(n["policies"])
                if not missing
                else "Not loaded: %s." % ", ".join(missing),
            }
        )
    if n.get("nft_expected"):
        r = run([which("nft") or "nft", "list", "table"] + list(NFT_TABLE), timeout=15)
        checks.append(
            {
                "id": "nftables",
                "label": "Lanvex's nftables table (%s %s)" % NFT_TABLE,
                "ok": r.returncode == 0,
                "detail": "Loaded." if r.returncode == 0 else (r.stderr or "Not loaded.").strip()[:300],
            }
        )
    for name, address in sorted(((n.get("dns") or {}).get("filters") or {}).items()):
        probe = ((n.get("dns") or {}).get("probes") or {}).get(name)

        def filtering(address=address, probe=probe, name=name):
            active = run(["systemctl", "is-active", "lanvex-dns@%s" % name], timeout=15).stdout.strip()
            if active != "active":
                return (False, "Its dnsmasq is %s." % (active or "not running"))
            rcode, _ = dns_query(address, "localhost")
            if rcode is None:
                return (False, "No answer at %s." % address)
            if probe is not None:
                blocked, _ = dns_query(address, probe)
                if blocked != 3:
                    return (False, "%s is not blocked at %s (answer code %s)." % (probe, address, blocked))
                return (True, "Answers at %s and blocks %s." % (address, probe))
            return (True, "Answers at %s." % address)

        ok, detail = _wait_for(filtering, 15)
        checks.append({"id": "dns:" + name, "label": "DNS filter for %s" % name, "ok": ok, "detail": detail})
    if n.get("forwarding"):
        value = read_text(FORWARD_KEY).strip() if os.path.exists(FORWARD_KEY) else "?"
        checks.append(
            {
                "id": "forwarding",
                "label": "IP forwarding",
                "ok": value == "1",
                "detail": "On." if value == "1" else "net.ipv4.ip_forward is %s." % value,
            }
        )
    d = n.get("dnsmasq")
    if d is not None and d["wanted"]:
        pools = (change.get("dhcp") or {}).get("pools") or []
        dns_off = any(k == "port" and v == "0" for f in dnsmasq_other_files() for k, v, _ in dnsmasq_lines(f))

        def serving():
            active = run(["systemctl", "is-active", "dnsmasq"], timeout=15).stdout.strip()
            if active != "active":
                return (False, "dnsmasq is %s." % (active or "not running"))
            r = run(["ss", "-H", "-u", "-l", "-n", "sport", "=", ":67"], timeout=15)
            if not (r.returncode == 0 and r.stdout.strip()):
                return (False, "dnsmasq is running, but nothing listens on UDP port 67.")
            # DNS on the server's address in each network (the devices' DNS server, and the filters'
            # upstream): absent when dnsmasq answers on loopback alone, as with local-service=host.
            r = run(["ss", "-H", "-u", "-l", "-n", "sport", "=", ":53"], timeout=15)
            bound = set()
            for line in r.stdout.splitlines():
                cols = line.split()
                if len(cols) >= 4:
                    bound.add(cols[3].rsplit(":", 1)[0].split("%")[0].strip("[]"))
            deaf = [
                "%s (%s)" % (p["interface"], p["router"])
                for p in pools
                if not dns_off and not bound & {"0.0.0.0", "*", p["router"]}  # noqa: S104 - read, not bound
            ]
            if deaf:
                return (
                    False,
                    "dnsmasq answers DHCP (UDP port 67), but does not answer on %s: no DNS on UDP port 53 there."
                    % ", ".join(deaf),
                )
            return (True, "dnsmasq is running and answers DHCP (UDP port 67) and DNS.")

        ok, detail = _wait_for(serving, 15)
        checks.append({"id": "dhcp", "label": "DHCP service (dnsmasq)", "ok": ok, "detail": detail})
    if n.get("kea") is not None:
        ok, detail = _wait_for(lambda: kea_check(n), 20)
        checks.append({"id": "dhcp", "label": "DHCP service (Kea)", "ok": ok, "detail": detail})
    return checks


# ---------------------------------------------------------------------------------------------------------
# Planning (shared by plan and apply)


def detect_and_plan(change: dict) -> dict:
    iface = change["interface"]
    if not os.path.exists("/sys/class/net/%s" % iface):
        raise Refused("invalid_request", "There is no interface %s on this server." % iface)
    if os.path.exists("/sys/class/net/%s/master" % iface):
        raise Refused(
            "unsupported", "%s is part of a bridge or bond; change the bridge or bond instead." % iface
        )
    if iface in (read_manifest().get("vlans") or []):
        raise Refused("unsupported", "%s is one of Lanvex's VLAN interfaces; change it as a network." % iface)
    if change.get("ipv6") is not None and not os.path.isdir("/proc/sys/net/ipv6"):
        raise Refused("unsupported", "This server's kernel has no IPv6.")
    if nm_running():
        profile = nm_active_profile(iface)
        if profile is not None and profile["name"].startswith(LANVEX_PROFILE):
            raise Refused(
                "unsupported", "%s is one of Lanvex's VLAN interfaces; change it as a network." % iface
            )
        if profile is not None:
            if profile["file"].startswith(NM_NETPLAN_PREFIX):
                return netplan_plan(iface, change)  # NetworkManager renders what netplan defines
            return nm_plan(iface, profile, change)
    if which("netplan") and os.path.isdir(NETPLAN_DIR):
        return netplan_plan(iface, change)
    raise Refused(
        "unsupported",
        "%s is not managed by netplan or by a stored NetworkManager profile. Lanvex cannot change it yet."
        % iface,
    )


def public_plan(plan: dict) -> dict:
    """What Lanvex may see and store: no internal keys, and file contents with secrets redacted."""
    out = {k: v for k, v in plan.items() if not k.startswith("_")}
    out["files"] = [dict(f, before=redact(f["before"]), after=redact(f["after"])) for f in plan["files"]]
    return out


# ---------------------------------------------------------------------------------------------------------
# Change state on disk


def change_dir(cid: str) -> str:
    return os.path.join(STATE_DIR, cid)


def load_state(cid: str) -> dict:
    try:
        with open(os.path.join(change_dir(cid), "state.json"), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError as e:
        raise Refused("unknown_change", "This server has no record of change %s." % cid) from e


def save_state(cid: str, state: dict) -> None:
    atomic_write(os.path.join(change_dir(cid), "state.json"), json.dumps(state, indent=2).encode(), 0o600)


def event(state: dict, what: str, detail: str = "") -> None:
    state.setdefault("history", []).append({"at": now(), "event": what, "detail": detail[:500]})


def pending_changes() -> list:
    out = []
    if not os.path.isdir(STATE_DIR):
        return out
    for cid in os.listdir(STATE_DIR):
        if ID_RE.match(cid):
            try:
                st = load_state(cid)
            except (Refused, ValueError):
                continue
            if st.get("state") in ("arming", "armed", "applied"):
                out.append(cid)
    return out


def prune() -> None:
    if not os.path.isdir(STATE_DIR):
        return
    dirs = sorted(
        (d for d in os.listdir(STATE_DIR) if ID_RE.match(d)),
        key=lambda d: os.path.getmtime(change_dir(d)),
        reverse=True,
    )
    for d in dirs[KEEP_CHANGES:]:
        try:
            if load_state(d).get("state") in ("confirmed", "rolled_back", "failed"):
                shutil.rmtree(change_dir(d), ignore_errors=True)
        except (Refused, ValueError):
            pass


def timer_unit(cid: str) -> str:
    return "lanvex-rollback-%s" % cid


def arm_timer(cid: str, seconds: int) -> None:
    if not which("systemd-run"):
        raise Refused("missing_tool", "systemd-run is needed for the automatic rollback timer.")
    run(
        [
            "systemd-run",
            "--unit",
            timer_unit(cid),
            "--description",
            "Lanvex automatic rollback of change %s" % cid,
            "--on-active=%ds" % seconds,
            "--timer-property=AccuracySec=1s",
            sys.executable,
            "-I",
            os.path.abspath(__file__),
            "timer-rollback",
            cid,
        ],
        timeout=30,
        check=True,
    )


def disarm_timer(cid: str) -> None:
    run(["systemctl", "stop", timer_unit(cid) + ".timer"], timeout=30)


def backup(cid: str, paths: list) -> list:
    records = []
    for i, path in enumerate(paths):
        rec = {"path": path, "existed": os.path.exists(path)}
        if rec["existed"]:
            st = os.stat(path)
            rec.update(backup="file-%d" % i, mode=st.st_mode & 0o7777, uid=st.st_uid, gid=st.st_gid)
            shutil.copy2(path, os.path.join(change_dir(cid), rec["backup"]))
        records.append(rec)
    return records


def restore_files(cid: str, records: list) -> None:
    for rec in records:
        if rec["existed"]:
            with open(os.path.join(change_dir(cid), rec["backup"]), "rb") as f:
                atomic_write(rec["path"], f.read(), rec["mode"], rec["uid"], rec["gid"])
        elif os.path.exists(rec["path"]):
            os.unlink(rec["path"])


def reactivate(state: dict) -> None:
    if state.get("kind") == "networks":
        networks_restore(state)
    elif state.get("kind") == "link":
        link_restore(state)
    elif state["backend"] == "netplan":
        netplan_apply()
    else:
        run(["nmcli", "connection", "load", state["nm"]["file"]], timeout=30, check=True)
        nm_activate(state["interface"], state["nm"]["uuid"])


# ---------------------------------------------------------------------------------------------------------
# Commands


def cmd_version(_req: dict) -> dict:
    return {
        "ok": True,
        "helper": "lanvex-netapply",
        "version": HELPER_VERSION,
        # Lets Lanvex tell an older or modified build of the same version from the file it ships.
        "sha256": sha256_file(os.path.realpath(__file__)),
        "python": "%d.%d.%d" % sys.version_info[:3],
        "tools": {
            "netplan": bool(which("netplan")),
            "nmcli": bool(which("nmcli")),
            "networkmanager_running": nm_running(),
            "systemd_run": bool(which("systemd-run")),
            "ping": bool(which("ping")),
            "dnsmasq": dnsmasq_service(),
            "kea": bool(which("kea-dhcp4")),
            "firewall_cmd": bool(which("firewall-cmd")),
            "ifup": bool(which("ifup")),
            "ufw": bool(which("ufw")),
        },
        "kinds": ["interface_ipv4", "networks", "link"],
        "pending": pending_changes(),
    }


def plan_for(req: dict) -> tuple:
    """(kind, normalised change, plan) for either kind of request."""
    kind = req.get("kind") or "interface_ipv4" if isinstance(req, dict) else None
    if kind == "networks":
        change = validate_networks(req)
        return kind, change, networks_plan(change)
    if kind == "link":
        change = validate_link(req)
        return kind, change, link_plan(change)
    if kind != "interface_ipv4":
        raise Refused("invalid_request", "Unknown kind of change: %r." % kind)
    change = validate_change(req)
    return kind, change, detect_and_plan(change)


def validate_plan(kind: str, plan: dict) -> None:
    if kind == "networks":
        dnsmasq_validate(plan["_write"].get(DNSMASQ_FILE) or "")
        if plan["_write"].get(KEA_FILE):
            kea_validate(plan["_write"][KEA_FILE])
        if plan["backend"] == "netplan" and plan["_write"].get(NETPLAN_LANVEX):
            netplan_validate({NETPLAN_LANVEX: plan["_write"][NETPLAN_LANVEX]})
        if plan["_write"].get(NFT_FILE):
            nft_validate(plan["_write"][NFT_FILE])
    elif kind == "link":
        if plan["backend"] == "netplan":
            candidates = {p: t for p, t in plan["_write"].items() if p.startswith(NETPLAN_DIR + "/")}
            candidates.update({p: None for p in plan["_delete"] if p.startswith(NETPLAN_DIR + "/")})
            netplan_validate(candidates)
    elif plan["backend"] == "netplan":
        netplan_validate(plan["_write"])


def cmd_plan(req: dict) -> dict:
    kind, change, plan = plan_for(req)
    if not plan["_changed"]:
        if kind == "networks":
            raise Refused("no_change", "The server already has exactly these VLAN interfaces and DHCP pools.")
        raise Refused("no_change", "%s already has these settings." % plan["files"][0]["path"])
    validate_plan(kind, plan)
    return dict(public_plan(plan), ok=True, change=change)


def cmd_apply(req: dict) -> dict:
    cid = check_id(req.get("id"))
    seconds = req.get("rollback_seconds")
    if not isinstance(seconds, int) or not 30 <= seconds <= 3600:
        raise Refused("invalid_request", "rollback_seconds must be between 30 and 3600.")
    expect = req.get("expect") or {}
    if os.path.exists(change_dir(cid)):
        raise Refused("duplicate", "Change %s was already applied on this server." % cid)
    others = pending_changes()
    if others:
        raise Refused("busy", "Change %s is still waiting for confirmation on this server." % others[0])

    kind, change, plan = plan_for(req)
    if plan["backend"] != expect.get("backend"):
        raise Refused(
            "drift",
            "The interface is now managed by %s, not %s. Prepare the change again."
            % (plan["backend"], expect.get("backend")),
        )
    expected = expect.get("files") or {}
    for f in plan["files"]:
        if f["path"] not in expected or f["sha256"] != expected[f["path"]]:
            raise Refused(
                "drift",
                "%s changed after the change was prepared. Nothing was changed; prepare the change again."
                % f["path"],
            )
    if kind in ("networks", "link") and set(expected) != {f["path"] for f in plan["files"]}:
        raise Refused(
            "drift",
            "The server's configuration changed after the change was prepared. Nothing was changed; prepare it again.",
        )
    validate_plan(kind, plan)

    os.makedirs(change_dir(cid), mode=0o700)
    state = {
        "id": cid,
        "state": "arming",
        "kind": kind,
        "backend": plan["backend"],
        "interface": change.get("interface") or change.get("name") or "networks",
        "change": change,
        "rollback_seconds": seconds,
        "nm": plan.get("_nm"),
        "networks": plan.get("_networks"),
        "removed_routes": plan.get("_removed_routes") or [],
        "link": plan.get("_link"),
    }
    event(state, "received")
    state["files"] = backup(cid, [f["path"] for f in plan["files"]] + list(plan.get("_backup") or []))
    save_state(cid, state)

    try:
        arm_timer(cid, seconds)
    except Refused as e:
        state["state"] = "failed"
        event(state, "failed", "Rollback timer could not be armed: %s" % e.message)
        save_state(cid, state)
        raise
    state["state"] = "armed"
    state["deadline"] = now() + seconds
    event(state, "armed", "Automatic rollback in %ds." % seconds)
    save_state(cid, state)

    try:
        if kind == "networks":
            networks_apply(plan)
        elif kind == "link":
            link_apply(plan)
        else:
            for path, text in plan["_write"].items():
                # netplan and NetworkManager both refuse configuration files others can read
                atomic_write(path, text.encode("utf-8"), 0o600, 0, 0)
            if plan["backend"] == "netplan":
                netplan_apply()
            else:
                run(["nmcli", "connection", "load", plan["_nm"]["file"]], timeout=30, check=True)
                nm_activate(change["interface"], plan["_nm"]["uuid"])
    except Exception as e:  # anything at all: put the old configuration back before reporting
        message = e.message if isinstance(e, Refused) else repr(e)
        do_rollback(cid, state, "The change could not be applied: %s" % message)
        raise Refused("apply_failed", "The change could not be applied and was undone: %s" % message) from e

    state["state"] = "applied"
    event(state, "applied")
    save_state(cid, state)
    prune()
    return {"ok": True, "state": "applied", "deadline": state["deadline"], "backend": plan["backend"]}


def do_rollback(cid: str, state: dict, reason: str) -> None:
    if state.get("kind") == "networks":
        try:
            networks_before_restore(state)
        except Refused:
            pass  # restore the files whatever happens
    restore_files(cid, state.get("files") or [])
    error = None
    try:
        reactivate(state)
    except Refused as e:
        error = e.message
    state["state"] = "rolled_back"
    state["rollback_reason"] = reason
    event(state, "rolled_back", reason + ("" if error is None else " (re-applying reported: %s)" % error))
    save_state(cid, state)
    disarm_timer(cid)


def _wait_for(predicate, seconds: float):
    deadline = now() + seconds
    result = predicate()
    while not result[0] and now() < deadline:
        time.sleep(1)
        result = predicate()
    return result


def ipv4_addresses(iface: str) -> list:
    r = run(["ip", "-json", "-4", "address", "show", "dev", iface], timeout=15, check=True)
    out = []
    for link in json.loads(r.stdout or "[]"):
        for a in link.get("addr_info", []):
            if a.get("family") == "inet":
                out.append(
                    {"address": "%s/%s" % (a["local"], a["prefixlen"]), "dynamic": bool(a.get("dynamic"))}
                )
    return out


def ipv6_addresses(iface: str) -> list:
    """Global IPv6 addresses; `dynamic` for those from router advertisements or DHCPv6."""
    r = run(["ip", "-json", "-6", "address", "show", "dev", iface, "scope", "global"], timeout=15, check=True)
    out = []
    for link in json.loads(r.stdout or "[]"):
        for a in link.get("addr_info", []):
            if a.get("family") == "inet6":
                out.append(
                    {
                        "address": str(ipaddress.ip_interface("%s/%s" % (a["local"], a["prefixlen"]))),
                        "dynamic": bool(a.get("dynamic")),
                    }
                )
    return out


def default_routes(iface: str, family: int = 4) -> list:
    r = run(["ip", "-json", "-%d" % family, "route", "show", "default"], timeout=15, check=True)
    return [x.get("gateway") for x in json.loads(r.stdout or "[]") if x.get("dev") == iface]


def interface_routes(iface: str, family: int) -> list:
    """Routes through iface in the main table, as {to, via, metric}."""
    r = run(["ip", "-json", "-%d" % family, "route", "show", "dev", iface], timeout=15, check=True)
    out = []
    for x in json.loads(r.stdout or "[]"):
        dst = x.get("dst") or ""
        if dst in ("", "default"):
            continue
        try:
            net = ipaddress.ip_network(
                dst if "/" in dst else "%s/%d" % (dst, 32 if family == 4 else 128), strict=False
            )
        except ValueError:
            continue
        via = _ip_or_none(x.get("gateway")) if x.get("gateway") else None
        out.append({"to": str(net), "via": str(via) if via else None, "metric": x.get("metric")})
    return out


def routes_check(iface: str, wanted: list, removed: list) -> tuple:
    """Every requested route there, through its next hop (and metric when given); removed ones gone."""
    have = interface_routes(iface, 4) + interface_routes(iface, 6)
    keys = {(h["to"], str(h["via"])) for h in have if h["via"]}
    missing = []
    for w in wanted:
        match = [h for h in have if (h["to"], str(h["via"])) == route_key(w)]
        if not match or (w.get("metric") is not None and not any(h["metric"] == w["metric"] for h in match)):
            missing.append("%s via %s" % (w["to"], w["via"]))
    left = ["%s via %s" % (r["to"], r["via"]) for r in removed if route_key(r) in keys]
    if missing or left:
        return (
            False,
            " ".join(
                (["Missing: %s." % ", ".join(missing)] if missing else [])
                + (["Still there: %s." % ", ".join(left)] if left else [])
            ),
        )
    return (True, "%d static route%s in place." % (len(wanted), "" if len(wanted) == 1 else "s"))


def neighbour(iface: str, address: str) -> dict | None:
    """The kernel's neighbour (ARP or NDP) entry for address on iface, if it resolved to a link-layer address."""
    family = ipaddress.ip_address(address).version
    r = run(["ip", "-json", "-%d" % family, "neighbour", "show", address, "dev", iface], timeout=15)
    if r.returncode != 0:
        return None
    for n in json.loads(r.stdout or "[]"):
        if n.get("lladdr") and not ({"FAILED", "INCOMPLETE"} & set(n.get("state") or [])):
            return n
    return None


def gateway_check(iface: str, gateway: str) -> dict:
    """Is the gateway there? Ping first; a gateway that drops ping must still answer ARP.

    A mistyped or absent gateway answers neither, fails the check, and the change is rolled back.
    """
    check = {"id": "gateway", "label": "Gateway %s answers" % gateway}
    detail = ""
    if which("ping"):
        r = run(["ping", "-c", "3", "-W", "1", "-I", iface, gateway], timeout=15)
        detail = (r.stdout.strip().splitlines() or [""])[-1][:200]
        if r.returncode == 0:
            return dict(check, ok=True, detail=detail)
    n = neighbour(iface, gateway)
    if n:
        how = "Does not answer ping, but is" if detail else "Is"
        return dict(check, ok=True, detail="%s reachable on the link (%s)." % (how, n["lladdr"]))
    if not detail:
        return dict(
            check, ok=None, detail="ping is not installed and the gateway is not in the neighbour table."
        )
    return dict(check, ok=False, detail=detail)


def dns_servers(iface: str) -> list | None:
    if which("resolvectl"):
        r = run(["resolvectl", "dns", iface], timeout=15)
        if r.returncode == 0:
            return re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", r.stdout.split(":", 1)[-1])
    if nm_running():
        r = run(["nmcli", "-g", "IP4.DNS", "device", "show", iface], timeout=15)
        if r.returncode == 0:
            return [s.strip() for s in r.stdout.replace("|", "\n").splitlines() if s.strip()]
    return None


def cmd_verify(req: dict) -> dict:
    cid = check_id(req.get("id"))
    st = load_state(cid)
    if st["state"] != "applied":
        raise Refused("wrong_state", "Change %s is %s, not waiting for verification." % (cid, st["state"]))
    if st.get("kind") == "networks":
        checks = networks_verify(st)
        return _verified(cid, checks)
    if st.get("kind") == "link":
        return _verified(cid, link_verify(st))
    change, iface = st["change"], st["interface"]
    dns_name = req.get("dns_name")
    if dns_name not in (None, ""):
        dns_name = check_domain(dns_name, "dns_name")
    checks = []

    def addresses_ok():
        have = ipv4_addresses(iface)
        if change["method"] == "static":
            want = sorted(change["addresses"])
            got = sorted(a["address"] for a in have if not a["dynamic"])
            return (got == want, "Expected %s, found %s." % (", ".join(want), ", ".join(got) or "none"))
        got = [a["address"] for a in have if a["dynamic"]]
        return (bool(got), "DHCP address: %s." % (", ".join(got) or "none yet"))

    ok, detail = _wait_for(addresses_ok, 30 if change["method"] == "dhcp" else 15)
    checks.append({"id": "addresses", "label": "IPv4 addresses on %s" % iface, "ok": ok, "detail": detail})

    if change["method"] == "static":

        def route_ok():
            gws = default_routes(iface)
            if change["gateway"]:
                return (
                    change["gateway"] in gws,
                    "Default routes via %s: %s." % (iface, ", ".join(g or "direct" for g in gws) or "none"),
                )
            return (
                not gws,
                "No default route via %s expected; found %s."
                % (iface, ", ".join(g or "direct" for g in gws) or "none"),
            )

        ok, detail = _wait_for(route_ok, 10)
        checks.append({"id": "default_route", "label": "Default route", "ok": ok, "detail": detail})
        if change["gateway"]:
            checks.append(gateway_check(iface, change["gateway"]))

    ipv6 = change.get("ipv6")
    if ipv6 is not None:
        checks += ipv6_checks(iface, ipv6)
    if change.get("routes") is not None:
        ok, detail = _wait_for(
            lambda: routes_check(iface, change["routes"], st.get("removed_routes") or []), 10
        )
        checks.append({"id": "routes", "label": "Static routes on %s" % iface, "ok": ok, "detail": detail})

    if change["dns_servers"]:
        servers = dns_servers(iface)
        if servers is None:
            checks.append(
                {
                    "id": "dns",
                    "label": "DNS servers",
                    "ok": None,
                    "detail": "Could not read the DNS configuration.",
                }
            )
        else:
            missing = [s for s in change["dns_servers"] if s not in servers]
            checks.append(
                {
                    "id": "dns",
                    "label": "DNS servers on %s" % iface,
                    "ok": not missing,
                    "detail": "Configured: %s." % (", ".join(servers) or "none")
                    + (" Missing: %s." % ", ".join(missing) if missing else ""),
                }
            )
    if dns_name:
        r = run(["getent", "ahostsv4", dns_name], timeout=15)
        first = (r.stdout.split() or [""])[0]
        checks.append(
            {
                "id": "resolve",
                "label": "%s resolves" % dns_name,
                "ok": r.returncode == 0,
                "detail": ("Resolved to %s." % first) if r.returncode == 0 else "Could not be resolved.",
            }
        )

    return _verified(cid, checks)


def ipv6_checks(iface: str, ipv6: dict) -> list:
    checks = []
    if ipv6["method"] == "static":
        want = sorted(ipv6["addresses"])

        def addresses_ok():
            got = sorted(a["address"] for a in ipv6_addresses(iface) if not a["dynamic"])
            return (
                set(want) <= set(got) and len(got) == len(want),
                "Expected %s, found %s." % (", ".join(want), ", ".join(got) or "none"),
            )

        ok, detail = _wait_for(addresses_ok, 15)
        checks.append(
            {"id": "ipv6_addresses", "label": "IPv6 addresses on %s" % iface, "ok": ok, "detail": detail}
        )

        def route_ok():
            gws = default_routes(iface, 6)
            if ipv6["gateway"]:
                return (
                    ipv6["gateway"] in gws,
                    "IPv6 default routes via %s: %s."
                    % (iface, ", ".join(g or "direct" for g in gws) or "none"),
                )
            return (
                not gws,
                "No IPv6 default route via %s expected; found %s."
                % (iface, ", ".join(g or "direct" for g in gws) or "none"),
            )

        ok, detail = _wait_for(route_ok, 10)
        checks.append({"id": "ipv6_default_route", "label": "IPv6 default route", "ok": ok, "detail": detail})
        if ipv6["gateway"]:
            checks.append(dict(gateway_check(iface, ipv6["gateway"]), id="ipv6_gateway"))
    elif ipv6["method"] == "auto":
        got = [a["address"] for a in ipv6_addresses(iface) if a["dynamic"]]
        checks.append(
            {
                "id": "ipv6_addresses",
                "label": "IPv6 addresses on %s" % iface,
                # No router advertisement yet is not a failure: the network may announce none.
                "ok": True if got else None,
                "detail": "From router advertisements or DHCPv6: %s." % (", ".join(got) or "none yet"),
            }
        )
    else:
        got = [a["address"] for a in ipv6_addresses(iface) if not a["dynamic"]]
        checks.append(
            {
                "id": "ipv6_addresses",
                "label": "Only a link-local IPv6 address on %s" % iface,
                "ok": not got,
                "detail": "Global addresses: %s." % (", ".join(got) or "none"),
            }
        )
    return checks


def _verified(cid: str, checks: list) -> dict:
    st = load_state(cid)  # the timer may have fired meanwhile
    event(st, "verified", "; ".join("%s=%s" % (c["id"], c["ok"]) for c in checks))
    save_state(cid, st)
    return {
        "ok": True,
        "state": st["state"],
        "checks": checks,
        "passed": all(c["ok"] is not False for c in checks),
    }


def cmd_confirm(req: dict) -> dict:
    cid = check_id(req.get("id"))
    st = load_state(cid)
    if st["state"] == "confirmed":
        return {"ok": True, "state": "confirmed"}
    if st["state"] != "applied":
        raise Refused(
            "wrong_state",
            "Change %s was %s: %s" % (cid, st["state"].replace("_", " "), st.get("rollback_reason", "")),
        )
    disarm_timer(cid)
    st["state"] = "confirmed"
    event(st, "confirmed")
    save_state(cid, st)
    return {"ok": True, "state": "confirmed"}


def cmd_rollback(req: dict, reason: str = "Rolled back at Lanvex's request.") -> dict:
    cid = check_id(req.get("id"))
    st = load_state(cid)
    if st["state"] == "rolled_back":
        return {"ok": True, "state": "rolled_back", "reason": st.get("rollback_reason")}
    if st["state"] not in ("armed", "applied"):
        raise Refused("wrong_state", "Change %s is %s and cannot be rolled back." % (cid, st["state"]))
    do_rollback(cid, st, reason)
    return {"ok": True, "state": "rolled_back", "reason": reason}


def cmd_status(req: dict) -> dict:
    cid = check_id(req.get("id"))
    st = load_state(cid)
    return {
        "ok": True,
        "state": st["state"],
        "deadline": st.get("deadline"),
        "reason": st.get("rollback_reason"),
        "history": st.get("history", []),
    }


COMMANDS = {
    "version": cmd_version,
    "plan": cmd_plan,
    "apply": cmd_apply,
    "verify": cmd_verify,
    "confirm": cmd_confirm,
    "rollback": cmd_rollback,
    "status": cmd_status,
    "inspect": cmd_inspect,
}
LOCKED = {"apply", "verify", "confirm", "rollback", "timer-rollback"}


def main(argv: list) -> int:
    if len(argv) < 2 or argv[1] not in set(COMMANDS) | {"timer-rollback"}:
        sys.stdout.write(
            json.dumps(
                {
                    "ok": False,
                    "error": {"code": "usage", "message": "usage: lanvex-netapply %s" % "|".join(COMMANDS)},
                }
            )
            + "\n"
        )
        return 2
    if os.geteuid() != 0:
        sys.stdout.write(
            json.dumps({"ok": False, "error": {"code": "not_root", "message": "Run through sudo."}}) + "\n"
        )
        return 1
    signal.signal(signal.SIGHUP, signal.SIG_IGN)  # a dropped SSH session must not stop a change half-way
    os.umask(0o077)
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    name = argv[1]
    lock = open(LOCK_FILE, "w")  # noqa: SIM115 - held for the whole run
    try:
        if name in LOCKED:
            fcntl.flock(lock, fcntl.LOCK_EX)
        if name == "timer-rollback":
            result = cmd_rollback(
                {"id": argv[2] if len(argv) > 2 else ""}, "No confirmation from Lanvex before the deadline."
            )
        else:
            raw = sys.stdin.read(1_000_000) if name != "version" else "{}"
            try:
                req = json.loads(raw or "{}")
            except ValueError as e:
                raise Refused("invalid_request", "stdin is not JSON: %s" % e) from e
            result = COMMANDS[name](req)
        code = 0
    except Refused as e:
        result, code = {"ok": False, "error": {"code": e.code, "message": e.message}}, 1
    except Exception as e:  # report, never a bare traceback: Lanvex reads stdout as JSON
        result, code = {"ok": False, "error": {"code": "internal", "message": repr(e)[:800]}}, 1
    finally:
        lock.close()
    sys.stdout.write(json.dumps(result) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
