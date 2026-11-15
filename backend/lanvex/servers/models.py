import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lanvex.core.db import Base, TimestampMixin, utcnow


class AuthMethod(enum.StrEnum):
    GENERATED_KEY = "generated_key"  # Lanvex generated the key pair; only the public key ever leaves Lanvex
    PRIVATE_KEY = "private_key"  # an administrator supplied a private key
    PASSWORD = "password"  # noqa: S105 - an auth method name, not a secret


class ConnectionStatus(enum.StrEnum):
    OK = "ok"
    UNREACHABLE = "unreachable"
    AUTH_FAILED = "auth_failed"
    HOST_KEY_MISMATCH = "host_key_mismatch"
    NOT_ALLOWED = "not_allowed"
    ERROR = "error"


def _enum(e: type[enum.Enum], name: str) -> Enum:
    return Enum(e, name=name, values_callable=lambda x: [m.value for m in x])


class Server(TimestampMixin, Base):
    __tablename__ = "servers"
    __table_args__ = (Index("uq_servers_name_lower", func.lower(text("name")), unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    address: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=22, nullable=False)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    adapter: Mapped[str] = mapped_column(String(32), default="linux-ssh", nullable=False)

    auth_method: Mapped[AuthMethod] = mapped_column(_enum(AuthMethod, "server_auth_method"), nullable=False)
    credential_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    public_key: Mapped[str | None] = mapped_column(Text)  # safe to display; for key-based auth

    host_key: Mapped[str] = mapped_column(Text, nullable=False)
    host_key_fingerprint: Mapped[str] = mapped_column(String(100), nullable=False)
    host_key_pinned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    netbox_object_type: Mapped[str | None] = mapped_column(
        String(32)
    )  # dcim.device | virtualization.virtualmachine
    netbox_object_id: Mapped[int | None] = mapped_column(Integer)
    # Scheduled reads: None = the installation's default interval, 0 = never on a schedule.
    read_interval_minutes: Mapped[int | None] = mapped_column(Integer)
    next_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    last_status: Mapped[ConnectionStatus | None] = mapped_column(
        _enum(ConnectionStatus, "server_connection_status")
    )
    last_status_message: Mapped[str | None] = mapped_column(String(500))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Summary of the latest successful collection, for list views.
    os_pretty_name: Mapped[str | None] = mapped_column(String(200))
    os_supported: Mapped[bool | None] = mapped_column()
    network_stack: Mapped[str | None] = mapped_column(String(64))

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    # Configuration changes: off until an administrator opts this server in (and automation is on).
    changes_enabled: Mapped[bool] = mapped_column(default=False, server_default=text("false"), nullable=False)
    helper_state: Mapped[str | None] = mapped_column(
        String(32)
    )  # ok, outdated, missing, no_sudo, no_python, …
    helper_version: Mapped[int | None] = mapped_column(Integer)
    helper_message: Mapped[str | None] = mapped_column(String(500))
    helper_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    snapshots: Mapped[list["ServerSnapshot"]] = relationship(
        back_populates="server", cascade="all, delete-orphan", passive_deletes=True
    )


class ServerSnapshot(Base):
    """One read of a server's actual network state. Immutable once written."""

    __tablename__ = "server_snapshots"
    __table_args__ = (Index("ix_server_snapshots_server_taken", "server_id", "taken_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("servers.id", ondelete="CASCADE"), nullable=False
    )
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    status: Mapped[ConnectionStatus] = mapped_column(
        _enum(
            ConnectionStatus,
            "server_connection_status",
        ),
        nullable=False,
    )
    message: Mapped[str | None] = mapped_column(String(500))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    requested_by: Mapped[str | None] = mapped_column(String(64))
    facts: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # command outputs, for troubleshooting parsers
    # A read that finds the same state (same fingerprint, or the same error) is not stored again;
    # it moves confirmed_at forward. So every snapshot is a state that differed from the one before.
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reads: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"), nullable=False)

    server: Mapped[Server] = relationship(back_populates="snapshots")


class DiscoveredAddress(Base):
    """A host a server had in its neighbour table (ARP / IPv6 neighbour discovery) during a read.

    One row per server and address, kept while it keeps being seen and for
    LANVEX_DISCOVERY_RETENTION_DAYS after. Actual state, like snapshots: never written to NetBox
    unless an administrator records it.
    """

    __tablename__ = "discovered_addresses"
    __table_args__ = (
        UniqueConstraint("server_id", "address", name="uq_discovered_addresses_server_address"),
        Index("ix_discovered_addresses_address", "address"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("servers.id", ondelete="CASCADE"), nullable=False
    )
    address: Mapped[str] = mapped_column(String(64), nullable=False)
    family: Mapped[int] = mapped_column(Integer, nullable=False)
    interface: Mapped[str] = mapped_column(String(64), nullable=False)
    mac: Mapped[str | None] = mapped_column(String(64))
    router: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
