"""The complete set of commands Lanvex may run on a Linux server.

Every command is a constant. Nothing from user input, NetBox, or a previous command's output is
ever interpolated into them. All are read-only and work as an unprivileged user.
"""

from enum import StrEnum

# Fixed environment: predictable (untranslated) output, and a PATH that includes sbin directories,
# which Debian omits for regular users.
_ENV = "env LC_ALL=C LANG=C PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin "


class Cmd(StrEnum):
    OS_RELEASE = "os_release"
    HOSTNAME = "hostname"
    KERNEL = "kernel"
    IP_VERSION = "ip_version"
    ADDRESSES = "addresses"
    ROUTES_V4 = "routes_v4"
    ROUTES_V6 = "routes_v6"
    RESOLV_CONF = "resolv_conf"
    UNIT_STATES = "unit_states"
    NETPLAN_FILES = "netplan_files"
    IFUPDOWN_CONFIG = "ifupdown_config"
    IFUPDOWN_INCLUDES = "ifupdown_includes"
    NMCLI_DEVICES = "nmcli_devices"
    NETWORKCTL_LIST = "networkctl_list"
    RESOLVECTL_DNS = "resolvectl_dns"
    RESOLVECTL_DOMAIN = "resolvectl_domain"


# Units whose state decides which network stack is in charge. Order matters: parsers read lines by position.
STACK_UNITS = ("NetworkManager", "systemd-networkd", "systemd-resolved", "networking")

COMMANDS: dict[Cmd, str] = {
    Cmd.OS_RELEASE: _ENV + "cat /etc/os-release 2>/dev/null || cat /usr/lib/os-release",
    Cmd.HOSTNAME: _ENV + "uname -n",
    Cmd.KERNEL: _ENV + "uname -srm",
    Cmd.IP_VERSION: _ENV + "ip -V",
    Cmd.ADDRESSES: _ENV + "ip -details -json address show",
    Cmd.ROUTES_V4: _ENV + "ip -json -4 route show table main",
    Cmd.ROUTES_V6: _ENV + "ip -json -6 route show table main",
    Cmd.RESOLV_CONF: _ENV + "cat /etc/resolv.conf",
    Cmd.UNIT_STATES: _ENV + "systemctl is-active " + " ".join(STACK_UNITS),
    Cmd.NETPLAN_FILES: _ENV + "ls -1 /etc/netplan",
    Cmd.IFUPDOWN_CONFIG: _ENV + "cat /etc/network/interfaces",
    # Debian and cloud images keep interface stanzas in interfaces.d (via a `source` line).
    Cmd.IFUPDOWN_INCLUDES: _ENV + "sh -c 'cat /etc/network/interfaces.d/* 2>/dev/null; true'",
    Cmd.NMCLI_DEVICES: _ENV + "nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status",
    Cmd.NETWORKCTL_LIST: _ENV + "networkctl list --no-legend --no-pager",
    Cmd.RESOLVECTL_DNS: _ENV + "resolvectl dns",
    Cmd.RESOLVECTL_DOMAIN: _ENV + "resolvectl domain",
}

# Always collected. The stack-specific ones below run only when that stack is active.
BASE_COMMANDS = (
    Cmd.OS_RELEASE,
    Cmd.HOSTNAME,
    Cmd.KERNEL,
    Cmd.IP_VERSION,
    Cmd.ADDRESSES,
    Cmd.ROUTES_V4,
    Cmd.ROUTES_V6,
    Cmd.RESOLV_CONF,
    Cmd.UNIT_STATES,
    Cmd.NETPLAN_FILES,
    Cmd.IFUPDOWN_CONFIG,
    Cmd.IFUPDOWN_INCLUDES,
)
NETWORKMANAGER_COMMANDS = (Cmd.NMCLI_DEVICES,)
NETWORKD_COMMANDS = (Cmd.NETWORKCTL_LIST,)
RESOLVED_COMMANDS = (Cmd.RESOLVECTL_DNS, Cmd.RESOLVECTL_DOMAIN)
