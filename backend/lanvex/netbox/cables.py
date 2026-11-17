"""Cables in NetBox: which interfaces can have one, and how to describe where one leads."""

from typing import Any

# NetBox refuses cables on these interface types (dcim.constants.VIRTUAL_IFACE_TYPES, and wireless).
NO_CABLE = {"virtual", "bridge", "lag"}


def iface_type(iface: dict[str, Any]) -> tuple[str, str]:
    t = iface.get("type") or {}
    return (
        (t.get("value") or "", t.get("label") or t.get("value") or "")
        if isinstance(t, dict)
        else (str(t), str(t))
    )


def cableable(iface: dict[str, Any]) -> bool:
    value, _ = iface_type(iface)
    return value not in NO_CABLE and not value.startswith("ieee802.11")


def not_cableable(where: str, iface: dict[str, Any], netbox_name: str) -> str:
    return (
        f"{where} has the type {iface_type(iface)[1]} in {netbox_name}, which cannot have a cable. "
        "If it is a physical port, change its type there."
    )


def far_ends(iface: dict[str, Any]) -> list[dict[str, Any]]:
    """What the interface's cable leads to (through patch panels, when NetBox knows the path)."""
    return list(iface.get("connected_endpoints") or iface.get("link_peers") or [])


def describe_ends(iface: dict[str, Any]) -> str | None:
    if not iface.get("cable"):
        return None
    ends = far_ends(iface)
    return (
        ", ".join(f"{(e.get('device') or {}).get('name')} / {e.get('name')}" for e in ends)
        or f"cable #{(iface.get('cable') or {}).get('id')}"
    )
