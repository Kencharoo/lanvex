"""Switches: stored with encrypted SNMP credentials, read on demand and on a schedule. Read-only."""

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lanvex.auth.models import User
from lanvex.core.config import Settings, get_settings
from lanvex.core.errors import Conflict, LanvexError, NotFound
from lanvex.core.logging import get_logger
from lanvex.core.secrets import Vault
from lanvex.integrations.snmp.client import SnmpCredentials, SnmpError, connect
from lanvex.integrations.snmp.switch import SYS_UPTIME, SwitchFacts, read_switch
from lanvex.netbox.client import NetBoxClient
from lanvex.switches.models import Switch, SwitchMac, SwitchRead
from lanvex.switches.ports import Move, moves, port_map
from lanvex.switches.schemas import (
    SnmpSummary,
    SwitchCreate,
    SwitchNetBox,
    SwitchOut,
    SwitchTestResult,
    SwitchUpdate,
)

log = get_logger("lanvex.switches")

READ_LIMIT_SECONDS = 300  # one read, however large the switch


def read_interval(sw: Switch, settings: Settings) -> int:
    """Minutes between scheduled reads; 0 = not on a schedule."""
    if not settings.switch_read_interval_minutes:
        return 0
    return (
        settings.switch_read_interval_minutes
        if sw.read_interval_minutes is None
        else sw.read_interval_minutes
    )


def snmp_summary(sw: Switch, settings: Settings, write: bool = False) -> SnmpSummary:
    """The SNMP settings that may be shown. Without the passwords' protocols if the stored credentials
    cannot be decrypted (the secret key changed): the switch's read says so."""
    cipher = sw.write_credential_ciphertext if write else sw.credential_ciphertext
    try:
        creds = SnmpCredentials.model_validate(Vault.from_settings(settings).decrypt(cipher or ""))
    except LanvexError:
        return SnmpSummary(
            version=sw.snmp_version,  # type: ignore[arg-type]
            username=sw.write_username if write else sw.snmp_username,
        )
    return SnmpSummary.model_validate(creds.summary())


def to_out(sw: Switch, netbox: SwitchNetBox | None = None) -> SwitchOut:
    settings = get_settings()
    minutes = read_interval(sw, settings)
    return SwitchOut(
        id=sw.id,
        name=sw.name,
        description=sw.description,
        address=sw.address,
        port=sw.port,
        snmp=snmp_summary(sw, settings),
        netbox=netbox or (SwitchNetBox(id=sw.netbox_device_id) if sw.netbox_device_id else None),
        read_interval_minutes=sw.read_interval_minutes,
        read_interval=minutes,
        next_read_at=sw.next_read_at if minutes else None,
        last_status=sw.last_status,
        last_status_message=sw.last_status_message,
        last_checked_at=sw.last_checked_at,
        last_read_at=sw.last_read_at,
        sys_name=sw.sys_name,
        sys_description=sw.sys_description,
        ports_total=sw.ports_total,
        ports_up=sw.ports_up,
        mac_count=sw.mac_count,
        changes_enabled=sw.changes_enabled,
        write_snmp=snmp_summary(sw, settings, write=True) if sw.write_credential_ciphertext else None,
        created_at=sw.created_at,
    )


class SwitchService:
    def __init__(self, db: AsyncSession, settings: Settings, nb: NetBoxClient | None = None) -> None:
        self.db, self.settings, self.nb = db, settings, nb
        self.moves: list[Move] = []  # devices the last read found on another port

    # ---- storage --------------------------------------------------------------------------------------

    async def all(self) -> list[Switch]:
        return list((await self.db.scalars(select(Switch).order_by(func.lower(Switch.name)))).all())

    async def get(self, switch_id: uuid.UUID) -> Switch:
        sw = await self.db.get(Switch, switch_id)
        if sw is None:
            raise NotFound("Switch not found.")
        return sw

    def credentials(self, sw: Switch) -> SnmpCredentials:
        return SnmpCredentials.model_validate(
            Vault.from_settings(self.settings).decrypt(sw.credential_ciphertext)
        )

    def _store_credentials(self, sw: Switch, creds: SnmpCredentials) -> None:
        sw.credential_ciphertext = Vault.from_settings(self.settings).encrypt(creds.model_dump())
        sw.snmp_version, sw.snmp_username = creds.version, creds.username

    async def _check_name(self, name: str, own: uuid.UUID | None = None) -> None:
        q = select(Switch.id).where(func.lower(Switch.name) == name.lower())
        if own is not None:
            q = q.where(Switch.id != own)
        if await self.db.scalar(q) is not None:
            raise Conflict("A switch with this name exists.", fields={"name": ["Already in use."]})

    async def _check_device(self, device_id: int | None) -> None:
        if device_id is None or self.nb is None:
            return
        try:
            await self.nb.get(f"dcim/devices/{device_id}/")
        except NotFound as e:
            raise NotFound(f"Device {device_id} is not in {self.nb.name}.") from e

    async def create(self, body: SwitchCreate, user: User | None) -> Switch:
        await self._check_name(body.name)
        await self._check_device(body.netbox_device_id)
        sw = Switch(
            name=body.name,
            description=body.description,
            address=body.address,
            port=body.port,
            netbox_device_id=body.netbox_device_id,
            read_interval_minutes=body.read_interval_minutes,
            created_by_id=user.id if user else None,
        )
        self._store_credentials(sw, body.credentials)
        minutes = read_interval(sw, self.settings)
        sw.next_read_at = datetime.now(UTC) + timedelta(minutes=minutes) if minutes else None
        self.db.add(sw)
        try:
            await self.db.flush()
        except IntegrityError as e:
            raise Conflict("A switch with this name exists.", fields={"name": ["Already in use."]}) from e
        return sw

    async def update(self, sw: Switch, body: SwitchUpdate) -> Switch:
        sent = body.model_fields_set
        if body.name is not None and body.name != sw.name:
            await self._check_name(body.name, sw.id)
            sw.name = body.name
        if body.description is not None:
            sw.description = body.description
        if body.address is not None:
            sw.address = body.address
        if body.port is not None:
            sw.port = body.port
        if body.credentials is not None:
            self._store_credentials(sw, body.credentials)
        if "write_credentials" in sent:
            creds = body.write_credentials
            sw.write_credential_ciphertext = (
                Vault.from_settings(self.settings).encrypt(creds.model_dump()) if creds else None
            )
            sw.write_username = creds.username if creds else None
        if body.changes_enabled is not None:
            sw.changes_enabled = body.changes_enabled
        if "netbox_device_id" in sent:
            await self._check_device(body.netbox_device_id)
            sw.netbox_device_id = body.netbox_device_id
        if "read_interval_minutes" in sent:
            sw.read_interval_minutes = body.read_interval_minutes
            minutes = read_interval(sw, self.settings)
            sw.next_read_at = datetime.now(UTC) + timedelta(minutes=minutes) if minutes else None
        await self.db.flush()
        return sw

    async def netbox_ref(self, sw: Switch) -> SwitchNetBox | None:
        if not sw.netbox_device_id:
            return None
        ref = SwitchNetBox(id=sw.netbox_device_id)
        if self.nb is None:
            return ref
        try:
            d = await self.nb.get(f"dcim/devices/{sw.netbox_device_id}/")
        except LanvexError:
            return ref
        return SwitchNetBox(
            id=d["id"], name=d.get("name"), url=self.settings.public_netbox_url(d.get("display_url"))
        )

    async def latest(self, sw: Switch) -> SwitchRead | None:
        return await self.db.get(SwitchRead, sw.id)

    # ---- talking to the switch ------------------------------------------------------------------------

    def _connect(self, address: str, port: int, creds: SnmpCredentials):  # type: ignore[no-untyped-def]
        return connect(
            address,
            port,
            creds,
            allowed=self.settings.server_allowed_networks,
            timeout=self.settings.snmp_timeout_seconds,
            retries=self.settings.snmp_retries,
        )

    async def test(self, address: str, port: int, creds: SnmpCredentials) -> SwitchTestResult:
        """Sign in and read the system group: what the switch says it is."""
        from lanvex.integrations.snmp.switch import SYS_DESCR, SYS_NAME, SwitchSystem, text

        try:
            async with asyncio.timeout(30), self._connect(address, port, creds) as s:
                got = await s.get(SYS_NAME, SYS_DESCR)
        except SnmpError as e:
            return SwitchTestResult(status=e.status, message=e.message)
        except TimeoutError:
            return SwitchTestResult(status="unreachable", message="No answer within 30 seconds.")
        system = SwitchSystem(name=text(got.get(SYS_NAME)), description=text(got.get(SYS_DESCR)))
        if system.name is None and system.description is None:
            return SwitchTestResult(
                status="error",
                message="The switch answers, but lets this sign-in see nothing (check its SNMP view).",
            )
        return SwitchTestResult(status="ok", message=f"Connected to {system.name or address}.", system=system)

    async def probe(self, sw: Switch) -> str | None:
        """None if the switch answers a GET of its uptime; otherwise why not."""
        try:
            async with asyncio.timeout(30), self._connect(sw.address, sw.port, self.credentials(sw)) as s:
                await s.get(SYS_UPTIME)
        except SnmpError as e:
            return e.message[:300]
        except TimeoutError:
            return "No answer (timed out)."
        except LanvexError as e:
            return e.message[:300]
        return None

    async def read(self, sw: Switch) -> SwitchFacts | None:
        """Read the switch's state over SNMP and keep it. Returns the facts, or None if the read failed
        (the outcome is in the switch's last_status and last_status_message)."""
        started = time.monotonic()
        sw.last_checked_at = datetime.now(UTC)
        facts: SwitchFacts | None = None
        try:
            creds = self.credentials(sw)
            async with asyncio.timeout(READ_LIMIT_SECONDS), self._connect(sw.address, sw.port, creds) as s:
                facts = await read_switch(s)
        except SnmpError as e:
            sw.last_status, sw.last_status_message = e.status, e.message[:500]
        except TimeoutError:
            sw.last_status = "error"
            sw.last_status_message = f"The read took longer than {READ_LIMIT_SECONDS} seconds."
        except LanvexError as e:
            sw.last_status, sw.last_status_message = "error", e.message[:500]
        except Exception as e:  # a malformed answer: the read failed, Lanvex goes on
            log.exception("switch_read_failed", switch=sw.name)
            sw.last_status, sw.last_status_message = "error", f"Reading failed: {type(e).__name__}."
        if facts is None:
            await self.db.flush()
            return None
        if not facts.ports and facts.system.name is None and facts.system.description is None:
            sw.last_status = "error"
            sw.last_status_message = (
                "The switch answers, but lets this sign-in see nothing (check its SNMP view)."
            )
            await self.db.flush()
            return None
        duration = int((time.monotonic() - started) * 1000)
        pm = await port_map(self.db)  # where each device was, as plain values: the rows change below
        before = {mac: (m.switch_id, m.if_index, m.port, pm.current(m)) for mac, m in pm.placed.items()}
        first = sw.first_read_at is None  # what a first read shows is new knowledge, not moves
        await self._keep(sw, facts, duration)
        self.moves = [] if first else moves(before, await port_map(self.db), {e.mac for e in facts.fdb})
        return facts

    async def _keep(self, sw: Switch, facts: SwitchFacts, duration_ms: int) -> None:
        now = facts.collected_at
        row = await self.db.get(SwitchRead, sw.id)
        data = facts.model_dump(mode="json")
        if row is None:
            self.db.add(SwitchRead(switch_id=sw.id, collected_at=now, duration_ms=duration_ms, facts=data))
        else:
            row.collected_at, row.duration_ms, row.facts = now, duration_ms, data
        physical = [p for p in facts.ports if p.kind == "ethernet"]
        sw.last_status = "ok"
        sw.last_status_message = None
        sw.last_read_at = now
        sw.first_read_at = sw.first_read_at or now
        sw.sys_name = (facts.system.name or "")[:255] or None
        sw.sys_description = (facts.system.description or "")[:500] or None
        sw.ports_total = len(physical)
        sw.ports_up = sum(1 for p in physical if p.oper_up)
        sw.mac_count = len({e.mac for e in facts.fdb})
        sw.probe_failures, sw.last_probe_error = 0, None

        names = {p.index: p.name[:64] for p in facts.ports}
        rows = {
            (e.mac, e.vlan or 0): {
                "id": uuid.uuid4(),
                "switch_id": sw.id,
                "mac": e.mac,
                "vlan": e.vlan or 0,
                "if_index": e.port,
                "port": names.get(e.port, str(e.port)),
                "first_seen": now,
                "last_seen": now,
            }
            for e in facts.fdb
        }
        values = list(rows.values())
        for i in range(0, len(values), 1000):
            stmt = insert(SwitchMac).values(values[i : i + 1000])
            await self.db.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_switch_macs_switch_mac_vlan",
                    set_={
                        "if_index": stmt.excluded.if_index,
                        "port": stmt.excluded.port,
                        "last_seen": stmt.excluded.last_seen,
                    },
                )
            )
        cutoff = now - timedelta(days=self.settings.discovery_retention_days)
        await self.db.execute(
            delete(SwitchMac).where(SwitchMac.switch_id == sw.id, SwitchMac.last_seen < cutoff)
        )
        await self.db.flush()
