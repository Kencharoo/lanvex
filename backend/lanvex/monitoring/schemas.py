import ipaddress
import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from lanvex.schemas import ApiModel

Kind = Literal[
    "unreachable",
    "read_failed",
    "drift",
    "pool_full",
    "unknown_device",
    "change_failed",
    "port_down",
    "device_moved",
]
SeverityName = Literal["critical", "warning", "info"]
WebhookFormat = Literal["generic", "ntfy", "gotify", "slack", "discord", "teams"]

EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
PATTERN_RE = re.compile(r"^[A-Za-z0-9_.:@*?\[\]-]{1,32}$")
NETWORK_NAME_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,32}$")
VLAN_RE = re.compile(r"^vlan:(\d{1,4})$", re.IGNORECASE)

# Interfaces whose addresses come and go with containers, VMs and VPNs: not compared with the documentation.
DEFAULT_DRIFT_IGNORE = [
    "docker*",
    "br-*",
    "veth*",
    "virbr*",
    "vnet*",
    "podman*",
    "cni*",
    "flannel*",
    "cali*",
    "tailscale*",
    "zt*",
    "lxdns",
]


class MonitoringSettings(ApiModel):
    unreachable: bool = True
    read_failed: bool = True
    drift: bool = True
    pool_full: bool = True
    unknown_device: bool = True
    change_failed: bool = True
    port_down: bool = True  # a switch port that led to another device went down
    device_moved: bool = True  # a documented device is now on another switch port
    unreachable_after: int = Field(default=3, ge=1, le=60)  # failed probes in a row
    pool_threshold: int = Field(default=90, ge=50, le=100)  # percent of the pool leased
    drift_ignore_interfaces: list[str] = Field(
        default_factory=lambda: list(DEFAULT_DRIFT_IGNORE), max_length=50
    )
    unknown_device_ignore: list[str] = Field(
        default_factory=list, max_length=100
    )  # network names, prefixes, or vlan:<id> (on switch ports)
    email_enabled: bool = False
    email_to: list[str] = Field(default_factory=list, max_length=20)
    email_min_severity: SeverityName = "info"
    notify_resolved: bool = True

    @field_validator("drift_ignore_interfaces")
    @classmethod
    def _patterns(cls, v: list[str]) -> list[str]:
        out = [p.strip() for p in v if p.strip()]
        for p in out:
            if not PATTERN_RE.match(p):
                raise ValueError(f"{p!r} is not an interface name or pattern.")
        return list(dict.fromkeys(out))

    @field_validator("unknown_device_ignore")
    @classmethod
    def _networks(cls, v: list[str]) -> list[str]:
        out = [p.strip() for p in v if p.strip()]
        for p in out:
            if "/" in p:
                try:
                    ipaddress.ip_network(p, strict=False)
                except ValueError as e:
                    raise ValueError(f"{p!r} is not a prefix.") from e
            elif m := VLAN_RE.match(p):
                if not 1 <= int(m.group(1)) <= 4094:
                    raise ValueError(f"{p!r}: VLAN IDs go from 1 to 4094.")
            elif not NETWORK_NAME_RE.match(p):
                raise ValueError(f"{p!r} is not a network name, prefix or vlan:<id>.")
        return list(dict.fromkeys(out))

    @field_validator("email_to")
    @classmethod
    def _emails(cls, v: list[str]) -> list[str]:
        out = [e.strip() for e in v if e.strip()]
        for e in out:
            if not EMAIL_RE.match(e):
                raise ValueError(f"{e!r} is not an email address.")
        return list(dict.fromkeys(out))


class MonitoringOut(ApiModel):
    settings: MonitoringSettings
    smtp_configured: bool
    smtp_from: str | None
    probe_seconds: int
    enabled: bool
    updated_at: datetime | None
    updated_by: str | None


class AlertOut(ApiModel):
    id: uuid.UUID
    kind: Kind
    server_id: uuid.UUID | None
    switch_id: uuid.UUID | None = None
    server_name: str  # the server's or the switch's name
    key: str
    severity: SeverityName
    title: str
    detail: list[str]
    state: Literal["open", "resolved"]
    opened_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None
    acknowledged_at: datetime | None
    acknowledged_by: str | None


class AlertList(ApiModel):
    alerts: list[AlertOut]
    open: int  # open alerts
    unacknowledged: int  # open alerts nobody acknowledged


class AlertCount(ApiModel):
    open: int
    unacknowledged: int
    critical: int


class WebhookIn(ApiModel):
    name: str = Field(min_length=1, max_length=100)
    format: WebhookFormat = "generic"
    url: str | None = Field(default=None, max_length=2000)  # None on edit: keep the stored URL
    enabled: bool = True
    min_severity: SeverityName = "info"

    @field_validator("url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not re.match(r"^https?://[^\s/]+", v):
            raise ValueError("The URL must start with http:// or https://.")
        return v


class WebhookOut(ApiModel):
    id: uuid.UUID
    name: str
    format: WebhookFormat
    url_hint: str
    enabled: bool
    min_severity: SeverityName
    created_at: datetime


class DeliveryResult(ApiModel):
    channel: str
    ok: bool
    error: str | None = None
