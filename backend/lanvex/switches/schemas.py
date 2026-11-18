import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator

from lanvex.integrations.snmp.client import AuthProtocol, PrivProtocol, SnmpCredentials
from lanvex.integrations.snmp.switch import SwitchFacts, SwitchSystem
from lanvex.schemas import ApiModel
from lanvex.servers.schemas import Port, valid_address

Interval = Annotated[int, Field(ge=0, le=10080)]


def _strip(v: object) -> object:
    return v.strip() if isinstance(v, str) else v


class SwitchTestRequest(ApiModel):
    address: str
    port: Port = 161
    credentials: SnmpCredentials

    _addr = field_validator("address")(lambda cls, v: valid_address(v))


class SwitchTestResult(ApiModel):
    status: str  # ConnectionStatus values
    message: str
    system: SwitchSystem | None = None


class SwitchCreate(ApiModel):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    description: Annotated[str, Field(max_length=200)] = ""
    address: str
    port: Port = 161
    credentials: SnmpCredentials
    netbox_device_id: int | None = Field(default=None, ge=1)
    read_interval_minutes: Interval | None = None

    _addr = field_validator("address")(lambda cls, v: valid_address(v))
    _names = field_validator("name", "description", mode="before")(lambda cls, v: _strip(v))


class SwitchUpdate(ApiModel):
    """Fields left out stay as they are. read_interval_minutes: null = the default, 0 = no schedule."""

    name: Annotated[str | None, Field(min_length=1, max_length=100)] = None
    description: Annotated[str | None, Field(max_length=200)] = None
    address: str | None = None
    port: Port | None = None
    credentials: SnmpCredentials | None = None
    netbox_device_id: int | None = Field(default=None, ge=1)  # null unlinks
    read_interval_minutes: Interval | None = None

    _addr = field_validator("address")(lambda cls, v: v if v is None else valid_address(v))
    _names = field_validator("name", "description", mode="before")(lambda cls, v: _strip(v))


class SnmpSummary(ApiModel):
    version: Literal["2c", "3"]
    username: str | None = None
    auth_protocol: AuthProtocol | None = None
    priv_protocol: PrivProtocol | None = None


class SwitchNetBox(ApiModel):
    id: int
    name: str | None = None
    url: str | None = None


class SwitchOut(ApiModel):
    id: uuid.UUID
    name: str
    description: str
    address: str
    port: int
    snmp: SnmpSummary
    netbox: SwitchNetBox | None
    read_interval_minutes: int | None
    read_interval: int  # effective, in minutes; 0 = not on a schedule
    next_read_at: datetime | None
    last_status: str | None
    last_status_message: str | None
    last_checked_at: datetime | None
    last_read_at: datetime | None
    sys_name: str | None
    sys_description: str | None
    ports_total: int | None
    ports_up: int | None
    mac_count: int | None
    created_at: datetime


# ---- what is connected to each port --------------------------------------------------------------------

EndpointKind = Literal["server", "device", "vm", "switch", "unknown"]


class Endpoint(ApiModel):
    """Something seen on a switch port: over LLDP, or by its MAC address in the address table."""

    via: Literal["lldp", "mac"]
    kind: EndpointKind
    name: str | None = None  # server, device or VM name; the LLDP system name
    interface: str | None = None  # its interface, when known
    mac: str | None = None
    vendor: str | None = None  # who made it, from its MAC address
    vlan: int | None = None
    addresses: list[str] = Field(default_factory=list)
    server_id: uuid.UUID | None = None
    switch_id: uuid.UUID | None = None
    netbox_url: str | None = None
    last_seen: datetime
    current: bool  # in the switch's latest read


PortStatus = Literal[
    "documented",  # the cable recorded on this port leads to what is seen there
    "different",  # a cable is recorded, to something else
    "undocumented",  # what is seen is known, no cable is recorded
    "unidentified",  # something is connected that the documentation does not know
    "shared",  # several documented devices on one port: something unmanaged sits in between
    "virtual",  # only virtual machines seen
    "not_linked",  # the switch is not linked to its device in the documentation
    "no_port",  # the switch's device has no interface of this name
    "not_checked",  # the documentation is not connected
]


class PortFinding(ApiModel):
    key: str
    switch_id: uuid.UUID
    switch_name: str
    if_index: int
    port: str
    alias: str | None = None
    uplink: bool = False  # leads to another switch Lanvex reads
    endpoints: list[Endpoint]
    status: PortStatus
    documented: str | None = None  # where the cable recorded on this port leads
    peer: str | None = None  # the device / interface a cable would be recorded to
    problem: str | None = None  # why a cable cannot be recorded
    device_id: int | None = None  # the switch's NetBox device
    peer_device_id: int | None = None
    peer_port: str | None = None


class PortFindings(ApiModel):
    ports: list[PortFinding]
    switches_read: int
    netbox_checked: bool


class SwitchDetail(SwitchOut):
    facts: SwitchFacts | None = None
    findings: list[PortFinding] = Field(default_factory=list)
    findings_error: str | None = None  # the documentation could not be compared
