"""Switches Lanvex reads over SNMP, their latest read, and the MAC addresses seen on their ports."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from lanvex.core.db import Base, TimestampMixin


class Switch(TimestampMixin, Base):
    __tablename__ = "switches"
    __table_args__ = (Index("uq_switches_name_lower", func.lower(text("name")), unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    address: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=161, nullable=False)

    # SNMP credentials, encrypted (core/secrets.py). The version and v3 user name are safe to show.
    snmp_version: Mapped[str] = mapped_column(String(4), nullable=False)
    snmp_username: Mapped[str | None] = mapped_column(String(64))
    credential_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)

    netbox_device_id: Mapped[int | None] = mapped_column(Integer)  # the switch's dcim.device
    # Scheduled reads: None = the installation's default interval, 0 = never on a schedule.
    read_interval_minutes: Mapped[int | None] = mapped_column(Integer)
    next_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    last_status: Mapped[str | None] = mapped_column(String(32))  # ConnectionStatus values
    last_status_message: Mapped[str | None] = mapped_column(String(500))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Summary of the latest successful read, for list views (the read itself is in switch_facts).
    sys_name: Mapped[str | None] = mapped_column(String(255))
    sys_description: Mapped[str | None] = mapped_column(String(500))
    ports_total: Mapped[int | None] = mapped_column(Integer)
    ports_up: Mapped[int | None] = mapped_column(Integer)
    mac_count: Mapped[int | None] = mapped_column(Integer)

    # The first successful read: MAC addresses seen then are the switch's starting point, not new devices.
    first_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Ports that led to another device (LLDP, or another switch Lanvex reads) when last up: their going
    # down is an alert. [{"index", "name", "peer", "uplink"}]
    watched_ports: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )

    # Monitoring: SNMP answers to a GET of sysUpTime (consecutive failures).
    probe_failures: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_probe_error: Mapped[str | None] = mapped_column(String(300))

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class SwitchRead(Base):
    """A switch's latest successful read (integrations/snmp/switch.py SwitchFacts). Actual state."""

    __tablename__ = "switch_facts"

    switch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("switches.id", ondelete="CASCADE"), primary_key=True
    )
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    facts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class SwitchMac(Base):
    """A MAC address in a switch's address table: on which port, first and last seen. Kept while it keeps
    being seen and for LANVEX_DISCOVERY_RETENTION_DAYS after, so devices that are off still show where
    they were connected."""

    __tablename__ = "switch_macs"
    __table_args__ = (
        UniqueConstraint("switch_id", "mac", "vlan", name="uq_switch_macs_switch_mac_vlan"),
        Index("ix_switch_macs_mac", "mac"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    switch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("switches.id", ondelete="CASCADE"), nullable=False
    )
    mac: Mapped[str] = mapped_column(String(17), nullable=False)
    vlan: Mapped[int] = mapped_column(Integer, nullable=False)  # 0 when the switch does not say
    if_index: Mapped[int] = mapped_column(Integer, nullable=False)
    port: Mapped[str] = mapped_column(String(64), nullable=False)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
