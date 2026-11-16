from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator

from lanvex.schemas import ApiModel, Choice, Meta, Ref, Tag


class PrefixStatus(StrEnum):
    CONTAINER = "container"
    ACTIVE = "active"
    RESERVED = "reserved"
    DEPRECATED = "deprecated"


class IPStatus(StrEnum):
    ACTIVE = "active"
    RESERVED = "reserved"
    DEPRECATED = "deprecated"
    DHCP = "dhcp"
    SLAAC = "slaac"


class IPRole(StrEnum):
    LOOPBACK = "loopback"
    SECONDARY = "secondary"
    ANYCAST = "anycast"
    VIP = "vip"
    VRRP = "vrrp"
    HSRP = "hsrp"
    GLBP = "glbp"
    CARP = "carp"


class VlanRef(ApiModel):
    id: int
    vid: int
    name: str


class UtilizationOut(ApiModel):
    percent: float
    used: int
    total: int
    basis: Literal["child_prefixes", "ip_addresses", "marked_utilized"]


class Gateway(ApiModel):
    ip_address_id: int
    address: str


class Prefix(ApiModel):
    id: int
    prefix: str
    family: int
    name: str = Field(description="Stored as the NetBox prefix description")
    status: Choice
    vrf: Ref | None
    vlan: VlanRef | None
    role: Ref | None
    tenant: Ref | None
    scope_type: str | None
    scope: Ref | None
    is_pool: bool
    mark_utilized: bool
    depth: int
    children: int
    comments: str
    tags: list[Tag]
    utilization: UtilizationOut | None = None
    gateway: Gateway | None = None
    meta: Meta


class _PrefixFields(ApiModel):
    name: str | None = Field(default=None, max_length=200)
    status: PrefixStatus | None = None
    vrf_id: int | None = None
    vlan_id: int | None = None
    role_id: int | None = None
    tenant_id: int | None = None
    # Where the prefix is used: a site (NetBox's scope). null on an update removes it.
    site_id: int | None = None
    is_pool: bool | None = None
    mark_utilized: bool | None = None
    comments: str | None = Field(default=None, max_length=10000)
    gateway: str | None = Field(default=None, description="Gateway address with mask, e.g. 10.0.0.1/24")

    @field_validator("name", "comments", "gateway", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v


class PrefixCreate(_PrefixFields):
    prefix: str = Field(max_length=64, examples=["10.20.0.0/24"])
    status: PrefixStatus = PrefixStatus.ACTIVE


class PrefixUpdate(_PrefixFields):
    """Metadata edits. The network itself is not editable here; create a new prefix instead."""

    clear_gateway: bool = False


class PrefixValidateRequest(ApiModel):
    prefix: str = Field(max_length=64)
    vrf_id: int | None = None
    gateway: str | None = None


class Finding(ApiModel):
    level: Literal["error", "warning", "info"]
    code: str
    message: str
    field: str | None = None


class RelatedPrefix(ApiModel):
    id: int
    prefix: str
    name: str
    status: str
    relation: Literal["duplicate", "parent", "child"]


class PrefixValidation(ApiModel):
    valid: bool
    normalized: str | None
    family: int | None
    size: int | None
    usable: int | None
    suggested_gateway: str | None
    findings: list[Finding]
    related: list[RelatedPrefix]


class AvailablePrefix(ApiModel):
    prefix: str
    family: int
    size: int


class AvailableIP(ApiModel):
    address: str
    family: int


# ---- IP addresses -----------------------------------------------------------


class AssignedInterface(ApiModel):
    interface_id: int
    interface_name: str
    device: Ref | None
    object_type: str


class IPAddress(ApiModel):
    id: int
    address: str
    family: int
    status: Choice
    role: Choice | None
    vrf: Ref | None
    tenant: Ref | None
    dns_name: str
    description: str
    assigned: AssignedInterface | None
    is_gateway: bool
    tags: list[Tag]
    meta: Meta


class IPAddressCreate(ApiModel):
    address: str = Field(max_length=64)
    vrf_id: int | None = None
    status: IPStatus = IPStatus.ACTIVE
    role: IPRole | None = None
    dns_name: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=200)
    interface_id: int | None = Field(default=None, description="dcim.interface id to assign to")


class IPAddressUpdate(ApiModel):
    status: IPStatus | None = None
    role: IPRole | Literal[""] | None = None
    dns_name: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=200)


class IPAssign(ApiModel):
    interface_id: int


class AllocateFromPrefix(ApiModel):
    status: IPStatus = IPStatus.ACTIVE
    description: str | None = Field(default=None, max_length=200)
    dns_name: str | None = Field(default=None, max_length=255)
    interface_id: int | None = None
