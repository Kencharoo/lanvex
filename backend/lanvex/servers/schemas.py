import re
import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from lanvex.integrations.linux.facts import HostFacts
from lanvex.schemas import ApiModel
from lanvex.servers.models import AuthMethod, ConnectionStatus

_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?$"
)
_USERNAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")

Port = Annotated[int, Field(ge=1, le=65535)]


def valid_address(v: str) -> str:
    import ipaddress

    v = v.strip()
    try:
        return str(ipaddress.ip_address(v.strip("[]")))
    except ValueError:
        pass
    if not _HOSTNAME.match(v):
        raise ValueError("Enter an IP address or a DNS name.")
    return v.rstrip(".").lower()


class ScanRequest(ApiModel):
    address: str
    port: Port = 22

    _addr = field_validator("address")(lambda cls, v: valid_address(v))


class HostKeyOut(ApiModel):
    algorithm: str
    fingerprint_sha256: str
    public_key: str
    resolved_addresses: list[str]


class Credentials(ApiModel):
    method: AuthMethod
    private_key: str | None = Field(default=None, max_length=20000)
    passphrase: str | None = Field(default=None, max_length=1024)
    password: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def _complete(self) -> "Credentials":
        if self.method == AuthMethod.PRIVATE_KEY and not (self.private_key or "").strip():
            raise ValueError("Paste the private key.")
        if self.method == AuthMethod.PASSWORD and not self.password:
            raise ValueError("Enter the password.")
        return self


class PinnedHostKey(ApiModel):
    public_key: str = Field(max_length=16384)
    fingerprint_sha256: str = Field(
        max_length=100, description="The fingerprint the administrator verified; must match public_key."
    )


class NetBoxLink(ApiModel):
    object_type: Literal["dcim.device", "virtualization.virtualmachine"]
    object_id: int


class ServerCreate(ApiModel):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    description: Annotated[str, Field(max_length=200)] = ""
    address: str
    port: Port = 22
    username: str
    host_key: PinnedHostKey
    credentials: Credentials
    netbox: NetBoxLink | None = None

    _addr = field_validator("address")(lambda cls, v: valid_address(v))

    @field_validator("username")
    @classmethod
    def _user(cls, v: str) -> str:
        if not _USERNAME.match(v):
            raise ValueError("Use a POSIX user name: letters, digits, '_', '.', '-'.")
        return v

    @field_validator("name", "description", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v


class ServerUpdate(ApiModel):
    name: Annotated[str | None, Field(min_length=1, max_length=100)] = None
    description: Annotated[str | None, Field(max_length=200)] = None
    address: str | None = None
    port: Port | None = None
    username: str | None = None
    host_key: PinnedHostKey | None = Field(
        default=None, description="Required when the address or port changes."
    )
    netbox: NetBoxLink | None = None
    unlink_netbox: bool = False

    @field_validator("address")
    @classmethod
    def _addr(cls, v: str | None) -> str | None:
        return valid_address(v) if v is not None else None

    @field_validator("username")
    @classmethod
    def _user(cls, v: str | None) -> str | None:
        if v is not None and not _USERNAME.match(v):
            raise ValueError("Use a POSIX user name: letters, digits, '_', '.', '-'.")
        return v


class NetBoxRef(ApiModel):
    object_type: str
    object_id: int
    name: str | None = None
    url: str | None = None


class ServerOut(ApiModel):
    id: uuid.UUID
    name: str
    description: str
    address: str
    port: int
    username: str
    adapter: str
    capabilities: list[str]
    auth_method: AuthMethod
    public_key: str | None
    authorized_keys_line: str | None
    host_key_fingerprint: str
    host_key_algorithm: str | None
    host_key_pinned_at: datetime
    netbox: NetBoxRef | None
    last_status: ConnectionStatus | None
    last_status_message: str | None
    last_checked_at: datetime | None
    last_collected_at: datetime | None
    os_pretty_name: str | None
    os_supported: bool | None
    network_stack: str | None
    created_at: datetime


class ServerCreated(ApiModel):
    server: ServerOut
    next_step: str


class TestResult(ApiModel):
    status: ConnectionStatus
    message: str
    remote_hostname: str | None = None


class SnapshotMeta(ApiModel):
    id: uuid.UUID
    taken_at: datetime
    status: ConnectionStatus
    message: str | None
    duration_ms: int | None
    requested_by: str | None


class SnapshotOut(SnapshotMeta):
    facts: HostFacts | None


class ComparisonRow(ApiModel):
    address: str
    status: Literal["match", "mask_differs", "interface_differs", "only_on_server", "only_in_netbox"]
    server_address: str | None = None
    server_interface: str | None = None
    netbox_address: str | None = None
    netbox_interface: str | None = None
    netbox_ip_id: int | None = None
    recorded_elsewhere: str | None = (
        None  # for server-only addresses: where NetBox has this host, if anywhere
    )


class Comparison(ApiModel):
    linked: bool
    netbox: NetBoxRef | None
    suggestions: list[NetBoxRef]
    snapshot_taken_at: datetime | None
    rows: list[ComparisonRow]
    counts: dict[str, int]
