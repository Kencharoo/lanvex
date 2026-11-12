#!/usr/bin/python3 -I
"""lanvex-netapply: applies a Lanvex network change on this server, with automatic rollback.

An administrator installs this file as /usr/local/libexec/lanvex/lanvex-netapply (root:root, 0755) and
allows the Lanvex SSH user to run it with sudo. Lanvex sends one JSON request on stdin and reads one JSON
result from stdout.

What it does, and nothing else:
  version   report the helper version and which network tools are present
  plan      show the exact change for one interface's IPv4 settings, without changing anything
  apply     back up the affected configuration, arm a rollback timer, then make the change
  verify    check the result (addresses, default route, gateway reachable, DNS)
  confirm   keep the change: stop the rollback timer
  rollback  restore the backed-up configuration now
  status    report what happened to a change

Safety rules:
  - It never runs a shell. Every program is called with a fixed argument list.
  - It never takes file paths, program names or option names from its input. It edits only the netplan
    file or NetworkManager profile that already defines the interface.
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
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HELPER_VERSION = 1
STATE_DIR = "/var/lib/lanvex/changes"
LOCK_FILE = "/run/lanvex-netapply.lock"
NETPLAN_DIR = "/etc/netplan"
NM_KEYFILE_DIR = "/etc/NetworkManager/system-connections/"
NM_NETPLAN_PREFIX = "/run/NetworkManager/system-connections/netplan-"
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
        raise Refused("timeout", "%s did not finish within %ss." % (" ".join(argv[:2]), timeout)) from e
    if check and r.returncode != 0:
        raise Refused(
            "command_failed", "%s failed: %s" % (" ".join(argv[:3]), (r.stderr or r.stdout).strip()[:500])
        )
    return r


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
    return {
        "interface": iface,
        "method": method,
        "addresses": [str(a) for a in addresses],
        "gateway": str(gw) if gw else None,
        "dns_servers": servers,
        "dns_search": search,
    }


# ---------------------------------------------------------------------------------------------------------
# netplan


def _is_v4(entry: object) -> bool:
    """A netplan address entry is either "a/len" or {"a/len": {options}}."""
    key = next(iter(entry)) if isinstance(entry, dict) and entry else entry
    try:
        return isinstance(ipaddress.ip_interface(str(key)), ipaddress.IPv4Interface)
    except ValueError:
        return False


def _is_v4_default_route(route: object) -> bool:
    if not isinstance(route, dict) or str(route.get("to")) not in ("default", "0.0.0.0/0"):
        return False
    try:
        return isinstance(ipaddress.ip_address(str(route.get("via"))), ipaddress.IPv4Address)
    except ValueError:
        return False


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
    """Return a copy of one netplan interface definition with the IPv4 settings replaced.

    IPv6 addresses, IPv6 routes, non-default IPv4 routes and every other key are kept as they are.
    """
    d = copy.deepcopy(defn) if isinstance(defn, dict) else {}
    d.pop("gateway4", None)
    nm = d.get("networkmanager")
    passthrough = nm.get("passthrough") if isinstance(nm, dict) else None
    if isinstance(passthrough, dict):
        keys = ["ipv4.method", "ipv4.addresses", "ipv4.gateway"]
        if change["dns_servers"] is not None:
            keys += ["ipv4.dns", "ipv4.dns-search"]
        for k in keys:
            passthrough.pop(k, None)
        if change["method"] == "static" and change["gateway"]:
            passthrough.pop("ipv4.never-default", None)
    kept_addresses = [a for a in (d.get("addresses") or []) if not _is_v4(a)]
    routes = [r for r in (d.get("routes") or []) if not _is_v4_default_route(r)]
    if change["method"] == "static":
        if d.get("dhcp4"):
            d["dhcp4"] = False
        d["addresses"] = list(change["addresses"]) + kept_addresses
        if change["gateway"]:
            routes.append({"to": "default", "via": change["gateway"]})
    else:
        d["dhcp4"] = True
        if kept_addresses:
            d["addresses"] = kept_addresses
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
        warnings.append("%s is a %s entry; only its IPv4 settings change." % (iface, section[:-1]))
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


def _key(line: str) -> str | None:
    """The key of a keyfile line, or None for headers, comments and blank lines."""
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", ";", "[")) or "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip()


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
    return lines  # with DNS unchanged (None), the existing dns lines stay where they are


def edit_keyfile(text: str, change: dict) -> str:
    """Rewrite the [ipv4] settings Lanvex manages in a NetworkManager keyfile; keep every other line.

    Replaced: method, addressN/addresses (an address's embedded gateway too), gateway, dns and dns-search
    (only when DNS changes), and never-default when a gateway is set (otherwise the gateway would never
    become the default route). The new lines go where the first replaced line was, so a preview shows
    only what really changes.
    """
    new = nm_ipv4_lines(change)
    dns_changes = change["dns_servers"] is not None

    def managed(key: str) -> bool:
        return bool(
            NM_IPV4_KEYS.match(key)
            or (dns_changes and NM_DNS_KEYS.match(key))
            or (key == "never-default" and change["gateway"])
        )

    def insert_at_section_end(out: list) -> None:
        pos = len(out)
        while pos > 0 and not out[pos - 1].strip():
            pos -= 1
        out[pos:pos] = new

    out, section, inserted = [], None, False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if section == "ipv4" and not inserted:
                insert_at_section_end(out)
                inserted = True
            section = stripped[1:-1]
            out.append(line)
            continue
        key = _key(line)
        if section == "ipv4" and key is not None and managed(key):
            if not inserted:
                out.extend(new)
                inserted = True
            continue
        out.append(line)
    if section == "ipv4" and not inserted:
        insert_at_section_end(out)
        inserted = True
    if not inserted:
        if out and out[-1].strip():
            out.append("")
        out.append("[ipv4]")
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
    }


def nm_activate(iface: str, uuid: str) -> None:
    r = run(["nmcli", "device", "reapply", iface], timeout=60)
    if r.returncode != 0:
        run(["nmcli", "connection", "up", uuid, "ifname", iface], timeout=90, check=True)


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
    if nm_running():
        profile = nm_active_profile(iface)
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
    if state["backend"] == "netplan":
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
        },
        "pending": pending_changes(),
    }


def cmd_plan(req: dict) -> dict:
    change = validate_change(req)
    plan = detect_and_plan(change)
    if not plan["_changed"]:
        raise Refused("no_change", "%s already has these settings." % plan["files"][0]["path"])
    if plan["backend"] == "netplan":
        netplan_validate(plan["_write"])
    return dict(public_plan(plan), ok=True, change=change)


def cmd_apply(req: dict) -> dict:
    cid = check_id(req.get("id"))
    seconds = req.get("rollback_seconds")
    if not isinstance(seconds, int) or not 30 <= seconds <= 3600:
        raise Refused("invalid_request", "rollback_seconds must be between 30 and 3600.")
    expect = req.get("expect") or {}
    change = validate_change(req)
    if os.path.exists(change_dir(cid)):
        raise Refused("duplicate", "Change %s was already applied on this server." % cid)
    others = pending_changes()
    if others:
        raise Refused("busy", "Change %s is still waiting for confirmation on this server." % others[0])

    plan = detect_and_plan(change)
    if plan["backend"] != expect.get("backend"):
        raise Refused(
            "drift",
            "The interface is now managed by %s, not %s. Prepare the change again."
            % (plan["backend"], expect.get("backend")),
        )
    for f in plan["files"]:
        if f["sha256"] != (expect.get("files") or {}).get(f["path"]):
            raise Refused(
                "drift",
                "%s changed after the change was prepared. Nothing was changed; prepare the change again."
                % f["path"],
            )
    if plan["backend"] == "netplan":
        netplan_validate(plan["_write"])

    os.makedirs(change_dir(cid), mode=0o700)
    state = {
        "id": cid,
        "state": "arming",
        "backend": plan["backend"],
        "interface": change["interface"],
        "change": change,
        "rollback_seconds": seconds,
        "nm": plan.get("_nm"),
    }
    event(state, "received")
    state["files"] = backup(cid, [f["path"] for f in plan["files"]])
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


def default_routes(iface: str) -> list:
    r = run(["ip", "-json", "-4", "route", "show", "default"], timeout=15, check=True)
    return [x.get("gateway") for x in json.loads(r.stdout or "[]") if x.get("dev") == iface]


def neighbour(iface: str, address: str) -> dict | None:
    """The kernel's neighbour (ARP) entry for address on iface, if it resolved to a link-layer address."""
    r = run(["ip", "-json", "-4", "neighbour", "show", address, "dev", iface], timeout=15)
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
        return dict(check, ok=None, detail="ping is not installed and the gateway is not in the ARP table.")
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
