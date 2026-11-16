import ipaddress
import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from lanvex.changes.models import ChangeStatus
from lanvex.schemas import ApiModel

IFACE_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,15}$")
DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)([A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9])?)*\.?$"
)

HelperStateName = Literal["unknown", "ok", "outdated", "missing", "no_sudo", "no_python", "timeout", "error"]


class ChangeRequest(ApiModel):
    """The IPv4 settings an interface should have. Everything else on the interface stays as it is."""

    interface: str
    method: Literal["static", "dhcp"]
    addresses: list[str] = Field(default_factory=list, max_length=32)
    gateway: str | None = None
    # None for both keeps the interface's DNS settings exactly as they are.
    dns_servers: list[str] | None = Field(default=None, max_length=8)
    dns_search: list[str] | None = Field(default=None, max_length=16)
    verify_dns_name: str | None = Field(
        default=None, description="A name that must resolve after the change."
    )

    @field_validator("interface")
    @classmethod
    def _iface(cls, v: str) -> str:
        if not IFACE_RE.match(v) or v == "lo":
            raise ValueError("Not an interface Lanvex can change.")
        return v

    @field_validator("addresses")
    @classmethod
    def _addresses(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for text in v:
            text = text.strip()
            try:
                a = ipaddress.ip_interface(text)
            except ValueError:
                raise ValueError(
                    f"{text} is not an address with prefix length, e.g. 192.0.2.10/24."
                ) from None
            if not isinstance(a, ipaddress.IPv4Interface):
                raise ValueError("Only IPv4 addresses can be changed so far.")
            if "/" not in text or a.network.prefixlen == 0:
                raise ValueError(f"{text} needs a prefix length, e.g. /24.")
            if (
                a.ip.is_loopback
                or a.ip.is_multicast
                or a.ip.is_link_local
                or a.ip.is_unspecified
                or a.ip.is_reserved
            ):
                raise ValueError(f"{a.ip} cannot be assigned to an interface.")
            if a.network.prefixlen < 31 and a.ip in (a.network.network_address, a.network.broadcast_address):
                raise ValueError(f"{a.ip} is the network or broadcast address of {a.network}.")
            out.append(str(a))
        if len({a.split("/")[0] for a in out}) != len(out):
            raise ValueError("The same address is listed twice.")
        return out

    @field_validator("gateway")
    @classmethod
    def _gateway(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        try:
            ip = ipaddress.ip_address(v.strip())
        except ValueError:
            raise ValueError(f"{v} is not an IPv4 address.") from None
        if not isinstance(ip, ipaddress.IPv4Address) or ip.is_unspecified or ip.is_multicast:
            raise ValueError(f"{v} is not a usable IPv4 gateway.")
        return str(ip)

    @field_validator("dns_servers")
    @classmethod
    def _dns(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        out = []
        for text in v:
            try:
                ip = ipaddress.ip_address(text.strip())
            except ValueError:
                raise ValueError(f"{text} is not an IPv4 address.") from None
            if not isinstance(ip, ipaddress.IPv4Address):
                raise ValueError("Only IPv4 DNS servers can be set so far.")
            out.append(str(ip))
        return out

    @field_validator("dns_search")
    @classmethod
    def _search(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        for d in v:
            if not DOMAIN_RE.match(d):
                raise ValueError(f"{d} is not a valid domain name.")
        return [d.rstrip(".") for d in v]

    @field_validator("verify_dns_name")
    @classmethod
    def _verify_name(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        if not DOMAIN_RE.match(v.strip()):
            raise ValueError(f"{v} is not a valid host name.")
        return v.strip().rstrip(".")

    @model_validator(mode="after")
    def _consistent(self) -> "ChangeRequest":
        if self.method == "static":
            if not self.addresses:
                raise ValueError("A static configuration needs at least one address.")
            if self.gateway:
                gw = ipaddress.ip_address(self.gateway)
                nets = [ipaddress.ip_interface(a) for a in self.addresses]
                if not any(gw in n.network for n in nets):
                    raise ValueError(f"The gateway {gw} is not inside any of the interface's networks.")
                if any(gw == n.ip for n in nets):
                    raise ValueError("The gateway cannot be one of the interface's own addresses.")
        elif self.addresses or self.gateway:
            raise ValueError("With DHCP, addresses and the gateway come from the DHCP server.")
        if (self.dns_servers is None) != (self.dns_search is None):
            # Changing DNS means stating both; send [] for "none".
            self.dns_servers, self.dns_search = self.dns_servers or [], self.dns_search or []
        return self

    @property
    def changes_dns(self) -> bool:
        return self.dns_servers is not None

    def helper_payload(self) -> dict[str, Any]:
        return {
            "interface": self.interface,
            "ipv4": {"method": self.method, "addresses": self.addresses, "gateway": self.gateway},
            "dns": {"servers": self.dns_servers, "search": self.dns_search} if self.changes_dns else None,
        }


class EnabledUpdate(ApiModel):
    enabled: bool


class ApproveRequest(ApiModel):
    confirm_name: str = Field(min_length=1, max_length=100)


class HelperStatus(ApiModel):
    state: HelperStateName
    version: int | None
    expected_version: int
    message: str | None
    checked_at: datetime | None


class ChangeOut(ApiModel):
    id: uuid.UUID
    number: int
    server_id: uuid.UUID
    server_name: str
    interface: str
    summary: str
    status: ChangeStatus
    requested_by: str
    created_at: datetime
    approved_by: str | None
    approved_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    outcome: str | None


class ConnectionPath(ApiModel):
    """Which of the server's interfaces carry Lanvex's connection, as of its last read."""

    address: str  # where Lanvex connects
    interfaces: list[str]  # the server's interfaces with that address; empty = not one of its own
    verify_address: str | None
    verify_interfaces: list[str]


class Readiness(ApiModel):
    automation_enabled: bool
    changes_enabled: bool
    stack_key: str | None
    stack_label: str | None
    stack_supported: bool | None
    helper: HelperStatus
    helper_path: str
    helper_sha256: str
    ready: bool
    blockers: list[str]
    rollback_seconds: int
    second_admin_required: bool
    open_change: ChangeOut | None
    connection: ConnectionPath | None = None
    protected_interfaces: list[str] = []


class HelperInstall(ApiModel):
    path: str
    sha256: str
    commands: list[str]


class PreviewFile(ApiModel):
    path: str
    label: str
    diff: str
    before: str
    after: str


class Preview(ApiModel):
    backend: str
    files: list[PreviewFile]
    commands: list[str]
    warnings: list[str]


class Check(ApiModel):
    id: str
    label: str
    ok: bool | None
    detail: str


class LogEntry(ApiModel):
    at: datetime
    level: Literal["info", "warning", "error"]
    message: str


class ChangeDetail(ChangeOut):
    request: ChangeRequest
    before: dict[str, Any] | None
    preview: Preview | None
    rollback_seconds: int
    deadline_at: datetime | None
    checks: list[Check]
    log: list[LogEntry]
    can_approve: bool
    approve_blocker: str | None
    can_cancel: bool
    can_recheck: bool
