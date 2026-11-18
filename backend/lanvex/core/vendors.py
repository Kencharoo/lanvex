"""Who made a network interface, from its MAC address: the IEEE registry as netaddr ships it.

Phones and laptops often use a random MAC address per network instead of their own; those are
locally administered addresses, which name no maker. A few such prefixes belong to virtualization
and are named as such.
"""

import re
from functools import lru_cache

from netaddr import EUI
from netaddr.core import AddrFormatError, NotRegisteredError

RANDOMIZED = "Randomized address"
LOCAL = {"52:54:00": "QEMU/KVM virtual machine", "02:42": "Docker container", "0a:00:27": "VirtualBox"}
_SUFFIX = re.compile(
    r"[,.\s]+(inc|incorporated|corporation|corporate|corp|co|company|ltd|limited|llc|gmbh|ag|sa|bv|ab|oy|"
    r"s\.a|b\.v|co\.,?\s*ltd|pte|plc|kg|spa|srl)\.?$",
    re.IGNORECASE,
)


def tidy(org: str) -> str:
    """'Apple, Inc.' -> 'Apple', 'Shenzhen Example Co.,Ltd' -> 'Shenzhen Example'."""
    name = " ".join(org.split())
    while True:
        shorter = _SUFFIX.sub("", name).strip(" ,.")
        if shorter == name or not shorter:
            return name
        name = shorter


@lru_cache(maxsize=8192)
def vendor(mac: str | None) -> str | None:
    if not mac:
        return None
    mac = mac.lower().replace("-", ":")
    try:
        first = int(mac.split(":")[0], 16)
    except ValueError:
        return None
    for prefix, name in LOCAL.items():
        if mac.startswith(prefix):
            return name
    if first & 0x01:  # multicast
        return None
    if first & 0x02:
        return RANDOMIZED
    try:
        return tidy(EUI(mac).oui.registration().org)
    except (NotRegisteredError, AddrFormatError, IndexError, ValueError):
        return None
