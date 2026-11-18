"""Changes to a switch port's VLANs: prepared from a fresh read, previewed, approved by an administrator,
written over SNMP, verified by reading back, and undone when the check fails.

A switch has nothing like the server helper's rollback timer, so what could cut Lanvex off the switch is
refused before it starts: uplinks, link aggregations and their members, and the port through which the
switch reaches Lanvex (found from the switch's own ARP and MAC tables). Off by default: changes need
LANVEX_AUTOMATION_ENABLED and an administrator allowing them for the switch.
"""

import asyncio
import os
import socket
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lanvex.auth.models import User
from lanvex.changes.models import ChangeStatus
from lanvex.core.config import Settings
from lanvex.core.db import Database
from lanvex.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from lanvex.core.logging import get_logger
from lanvex.core.secrets import Vault
from lanvex.integrations.snmp.client import SnmpCredentials, SnmpError, connect, resolve
from lanvex.integrations.snmp.switch import SwitchFacts
from lanvex.integrations.snmp.vlans import DEFAULT_ROUTE, PlanError, drifted, plan, read_port, undo, write
from lanvex.netbox.client import NetBoxClient
from lanvex.switches.models import Switch, SwitchChange, SwitchRead
from lanvex.switches.ports import port_map

log = get_logger("lanvex.switches.changes")

OPEN = (ChangeStatus.PENDING_APPROVAL, ChangeStatus.APPROVED, ChangeStatus.RUNNING, ChangeStatus.VERIFYING)
STALE_AFTER = timedelta(minutes=10)


def now() -> datetime:
    return datetime.now(UTC)


def note(c: SwitchChange, message: str, level: str = "info") -> None:
    c.log = [*(c.log or []), {"at": now().isoformat(), "level": level, "message": message[:1000]}]


def vlans_text(untagged: list[int] | int | None, tagged: list[int]) -> str:
    u = untagged if isinstance(untagged, list) else [untagged] if untagged else []
    out = f"untagged {', '.join(map(str, u)) or '—'}"
    return out + (f", tagged {', '.join(map(str, tagged))}" if tagged else "")


def credentials(sw: Switch, settings: Settings, write: bool) -> SnmpCredentials:
    cipher = (
        sw.write_credential_ciphertext
        if write and sw.write_credential_ciphertext
        else sw.credential_ciphertext
    )
    return SnmpCredentials.model_validate(Vault.from_settings(settings).decrypt(cipher))


def session(sw: Switch, settings: Settings, write: bool):  # type: ignore[no-untyped-def]
    return connect(
        sw.address,
        sw.port,
        credentials(sw, settings, write),
        allowed=settings.server_allowed_networks,
        timeout=settings.snmp_timeout_seconds,
        retries=settings.snmp_retries,
    )


async def source_address(address: str, port: int) -> str | None:
    """The address this host sends from towards the switch (what the switch sees, without NAT)."""
    try:
        ip = await resolve(address, port)
        with socket.socket(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((ip, port))
            return str(s.getsockname()[0])
    except (OSError, SnmpError):
        return None


class SwitchChangeService:
    def __init__(self, db: AsyncSession, settings: Settings, nb: NetBoxClient | None = None) -> None:
        self.db, self.settings, self.nb = db, settings, nb

    async def get(self, change_id: uuid.UUID) -> SwitchChange:
        c = await self.db.get(SwitchChange, change_id)
        if c is None:
            raise NotFound("Change not found.")
        return c

    async def recent(self, switch_id: uuid.UUID | None = None, limit: int = 100) -> list[SwitchChange]:
        q = select(SwitchChange).order_by(SwitchChange.created_at.desc()).limit(limit)
        if switch_id is not None:
            q = q.where(SwitchChange.switch_id == switch_id)
        return list((await self.db.scalars(q)).all())

    async def open_change(self, switch_id: uuid.UUID) -> SwitchChange | None:
        return await self.db.scalar(
            select(SwitchChange).where(SwitchChange.switch_id == switch_id, SwitchChange.status.in_(OPEN))
        )

    # ---- preparing --------------------------------------------------------------------------------

    async def protected(self, sw: Switch, facts: SwitchFacts, s: Any) -> tuple[set[int], bool]:
        """Ports the switch reaches Lanvex through: where the MAC address of Lanvex's address (or of the
        switch's default gateway, when Lanvex is in another network) is learned. And whether that
        address was found at all."""
        wanted = {a for a in [await source_address(sw.address, sw.port)] if a}
        try:
            gateway = (await s.get(DEFAULT_ROUTE)).get(DEFAULT_ROUTE)
        except SnmpError:
            gateway = None
        if isinstance(gateway, str) and gateway not in ("0.0.0.0", ""):  # noqa: S104  (no default route)
            wanted.add(gateway)
        macs = {a.mac for a in facts.arp if a.address in wanted}
        return {e.port for e in facts.fdb if e.mac in macs}, bool(macs)

    async def prepare(
        self, sw: Switch, if_index: int, untagged: int, tagged: list[int], user: User
    ) -> SwitchChange:
        if not self.settings.automation_enabled:
            raise PermissionDenied(
                "Changes are turned off for this Lanvex installation (LANVEX_AUTOMATION_ENABLED)."
            )
        if not sw.changes_enabled:
            raise Conflict("Changes are not allowed for this switch. Allow them in its settings first.")
        current = await self.open_change(sw.id)
        if current is not None:
            raise Conflict(
                f"Change #{current.number} for this switch is still {current.status.replace('_', ' ')}."
            )
        row = await self.db.get(SwitchRead, sw.id)
        if row is None:
            raise Conflict("Read the switch first.")
        facts = SwitchFacts.model_validate(row.facts)
        port = facts.port(if_index)
        if port is None:
            raise NotFound("The switch has no such port (as of its last read).")
        if port.kind not in ("ethernet",):
            raise ValidationFailed(f"Only physical ports can be changed from Lanvex; {port.name} is not one.")
        if port.lag:
            lag = facts.port(port.lag)
            raise ValidationFailed(
                f"{port.name} is part of {lag.name if lag else 'a link aggregation'}: change it on the switch."
            )
        pm = await port_map(self.db)
        other = pm.uplinks.get((sw.id, if_index))
        if (sw.id, if_index) in pm.uplinks:
            name = pm.switches[other].name if other in pm.switches else "another switch"
            raise ValidationFailed(f"{port.name} leads to {name}: uplinks are not changed from Lanvex.")

        warnings: list[str] = []
        try:
            async with asyncio.timeout(60), session(sw, self.settings, write=False) as s:
                protected, found = await self.protected(sw, facts, s)
                state = await read_port(s, if_index)
        except SnmpError as e:
            raise Conflict(e.message) from e
        except TimeoutError as e:
            raise Conflict("The switch did not answer in time.") from e
        if if_index in protected:
            raise ValidationFailed(
                f"The switch reaches Lanvex through {port.name}: changing it could cut Lanvex off the switch."
            )
        if not found:
            warnings.append(
                "Lanvex could not tell which port the switch reaches it through; make sure it is not this one."
            )
        try:
            ops = plan(state, port.name, untagged, tagged)
        except PlanError as e:
            raise ValidationFailed(e.message) from e
        if not ops:
            raise ValidationFailed(f"{port.name} already has these VLANs.")

        for n in facts.neighbors:
            if n.port == if_index and "Bridge" in n.capabilities and "Station" not in n.capabilities:
                warnings.append(
                    f"{n.system_name or n.chassis_id} on {port.name} forwards for other devices "
                    "(a switch or access point): they change VLAN too."
                )
        devices = {m.mac for m in pm.by_port.get((sw.id, if_index), []) if pm.current(m)}
        if devices:
            n = len(devices)
            warnings.append(
                f"{n} device on {port.name} loses its connection until it has an address in VLAN {untagged}."
                if n == 1
                else f"{n} devices on {port.name} lose their connection until they have an address in VLAN {untagged}."
            )
        warnings.append("The switch may forget the change on restart unless its configuration is saved.")

        want_tagged = sorted(set(tagged) - {untagged})
        c = SwitchChange(
            switch_id=sw.id,
            switch_name=sw.name,
            if_index=if_index,
            port=port.name,
            summary=f"{port.name}: {vlans_text(state.untagged_vlans, state.tagged_vlans)} → {vlans_text(untagged, want_tagged)}",
            request={"untagged": untagged, "tagged": want_tagged},
            before=state.summary(),
            plan=ops,
            warnings=warnings,
            status=ChangeStatus.PENDING_APPROVAL,
            requested_by_id=user.id,
            requested_by_name=user.username,
            log=[],
        )
        note(c, f"Prepared by {user.username}: {c.summary}.")
        self.db.add(c)
        await self.db.flush()
        return c

    # ---- approval ---------------------------------------------------------------------------------

    def approve_blocker(self, c: SwitchChange, user: User) -> str | None:
        if c.status != ChangeStatus.PENDING_APPROVAL:
            return None
        if not self.settings.automation_enabled:
            return "Changes are turned off for this Lanvex installation."
        if self.settings.changes_require_second_admin and c.requested_by_id == user.id:
            return "Another administrator has to approve changes you prepared."
        return None

    async def approve(self, c: SwitchChange, user: User, confirm: str) -> SwitchChange:
        if c.status != ChangeStatus.PENDING_APPROVAL:
            raise Conflict(f"Change #{c.number} is {c.status.replace('_', ' ')}, not waiting for approval.")
        blocker = self.approve_blocker(c, user)
        if blocker:
            raise PermissionDenied(blocker)
        sw = await self.db.get(Switch, c.switch_id)
        if sw is None or not sw.changes_enabled:
            raise Conflict("Changes are no longer allowed for this switch.")
        if confirm != c.port:
            raise ValidationFailed(
                "Type the port's name exactly to approve.", fields={"confirm_name": [f"Type {c.port}."]}
            )
        c.status = ChangeStatus.APPROVED
        c.approved_by_id, c.approved_by_name, c.approved_at = user.id, user.username, now()
        note(c, f"Approved by {user.username}.")
        return c

    async def cancel(self, c: SwitchChange, user: User) -> SwitchChange:
        if c.status not in (ChangeStatus.PENDING_APPROVAL, ChangeStatus.APPROVED):
            raise Conflict(
                f"Change #{c.number} is {c.status.replace('_', ' ')} and can no longer be cancelled."
            )
        c.status = ChangeStatus.CANCELLED
        c.cancelled_by_name, c.finished_at = user.username, now()
        c.outcome = f"Cancelled by {user.username}. Nothing was changed on the switch."
        note(c, c.outcome)
        return c


# ---- running ------------------------------------------------------------------------------------------


async def execute(db: AsyncSession, settings: Settings, c: SwitchChange) -> None:
    """Apply an approved change and verify it; the outcome is in the change. Commits as it goes."""
    sw = await db.get(Switch, c.switch_id)
    if sw is None:
        c.status, c.finished_at, c.outcome = ChangeStatus.FAILED, now(), "The switch was removed."
        await db.commit()
        return
    untagged, tagged = c.request["untagged"], c.request["tagged"]
    done = 0
    try:
        async with asyncio.timeout(120), session(sw, settings, write=True) as s:
            state = await read_port(s, c.if_index)
            moved = drifted(state, c.plan)
            if moved:
                c.status, c.finished_at = ChangeStatus.FAILED, now()
                c.outcome = (
                    f"The switch's VLANs changed since the preview ({'; '.join(moved)}). Nothing was changed: "
                    "prepare the change again."
                )
                note(c, c.outcome, "error")
                await db.commit()
                return
            try:
                for op in c.plan:
                    await write(s, op)
                    done += 1
                    note(c, f"Written: {op['text']}.")
            except SnmpError as e:
                note(c, e.message, "error")
                await undo(s, c.plan, done, lambda m: note(c, m))
                c.status = ChangeStatus.ROLLED_BACK if done else ChangeStatus.FAILED
                c.finished_at = now()
                c.outcome = (
                    f"{e.message} {'The writes before it were undone.' if done else 'Nothing was changed.'}"
                )
                await db.commit()
                return
        c.status = ChangeStatus.VERIFYING
        note(c, "Written. Reading the port back.")
        await db.commit()
        async with asyncio.timeout(60), session(sw, settings, write=False) as s:
            after = await read_port(s, c.if_index)
        if after.untagged_vlans == [untagged] and after.tagged_vlans == tagged and after.pvid == untagged:
            c.status, c.finished_at = ChangeStatus.SUCCEEDED, now()
            c.outcome = f"{c.port} is {vlans_text(untagged, tagged)}, read back from the switch."
            note(c, c.outcome)
            await db.commit()
            return
        note(
            c,
            f"Read back {vlans_text(after.untagged_vlans, after.tagged_vlans)}, PVID {after.pvid}: not as planned.",
            "error",
        )
        async with asyncio.timeout(60), session(sw, settings, write=True) as s:
            await undo(s, c.plan, len(c.plan), lambda m: note(c, m))
        c.status, c.finished_at = ChangeStatus.ROLLED_BACK, now()
        c.outcome = (
            f"The switch did not take the change as planned (read back "
            f"{vlans_text(after.untagged_vlans, after.tagged_vlans)}, PVID {after.pvid}); it was undone."
        )
        await db.commit()
    except (SnmpError, TimeoutError) as e:
        message = e.message if isinstance(e, SnmpError) else "The switch stopped answering."
        if done or c.status == ChangeStatus.VERIFYING:
            c.status = ChangeStatus.NEEDS_ATTENTION
            c.outcome = (
                f"{message} Lanvex cannot tell what the switch has now: check {c.port} on the switch itself."
            )
        else:
            c.status, c.outcome = ChangeStatus.FAILED, f"{message} Nothing was changed."
        c.finished_at = now()
        note(c, c.outcome, "error")
        await db.commit()


class SwitchChangeRunner:
    """Runs approved switch changes one at a time per process; claimed with SKIP LOCKED."""

    POLL_SECONDS = 2.0

    def __init__(self, db: Database, settings: Settings, netbox: Callable[[], NetBoxClient | None]) -> None:
        self.db, self.settings, self.netbox = db, settings, netbox
        self.name = f"{socket.gethostname()}:{os.getpid()}"
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="lanvex-switch-changes")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def wake(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        while True:
            worked = False
            try:
                await self.reconcile_stale()
                worked = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("switch_change_runner_error")
            if not worked:
                try:
                    await asyncio.wait_for(self._wake.wait(), self.POLL_SECONDS)
                except TimeoutError:
                    pass
                self._wake.clear()

    async def run_once(self) -> bool:
        async with self.db.sessionmaker() as db:
            c = await db.scalar(
                select(SwitchChange)
                .where(SwitchChange.status == ChangeStatus.APPROVED)
                .order_by(SwitchChange.approved_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if c is None:
                return False
            c.status, c.started_at = ChangeStatus.RUNNING, now()
            note(c, f"Started ({self.name}).")
            await db.commit()
            await execute(db, self.settings, c)
            await self._after(db, c)
            return True

    async def _after(self, db: AsyncSession, c: SwitchChange) -> None:
        """Read the switch again, and raise an alert for a change that did not go through."""
        from lanvex.monitoring.service import MonitoringService
        from lanvex.switches.service import SwitchService

        sw = await db.get(Switch, c.switch_id)
        if sw is None:
            return
        try:
            if c.status in (ChangeStatus.SUCCEEDED, ChangeStatus.ROLLED_BACK):
                await SwitchService(db, self.settings, self.netbox()).read(sw)
            if c.status in (ChangeStatus.ROLLED_BACK, ChangeStatus.FAILED, ChangeStatus.NEEDS_ATTENTION):
                if self.settings.monitor_enabled:
                    await MonitoringService(db, self.settings).change_failed(
                        sw,
                        c.id,
                        f"Change #{c.number} on {sw.name} {c.status.replace('_', ' ')}",
                        [c.summary, *([c.outcome] if c.outcome else [])],
                    )
            await db.commit()
        except Exception:
            await db.rollback()
            log.exception("switch_change_after_failed", change=c.number)

    async def reconcile_stale(self) -> None:
        """A change left running by a process that stopped: nobody knows how far it got."""
        async with self.db.sessionmaker() as db:
            stale = (
                await db.scalars(
                    select(SwitchChange)
                    .where(
                        SwitchChange.status.in_((ChangeStatus.RUNNING, ChangeStatus.VERIFYING)),
                        SwitchChange.started_at < now() - STALE_AFTER,
                    )
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for c in stale:
                c.status, c.finished_at = ChangeStatus.NEEDS_ATTENTION, now()
                c.outcome = f"Lanvex stopped while the change ran: check {c.port} on the switch itself."
                note(c, c.outcome, "error")
            if stale:
                await db.commit()


def to_out(c: SwitchChange) -> dict[str, Any]:
    return {
        "id": c.id,
        "number": c.number,
        "switch_id": c.switch_id,
        "switch_name": c.switch_name,
        "if_index": c.if_index,
        "port": c.port,
        "summary": c.summary,
        "status": c.status,
        "requested_by": c.requested_by_name,
        "created_at": c.created_at,
        "approved_by": c.approved_by_name,
        "approved_at": c.approved_at,
        "started_at": c.started_at,
        "finished_at": c.finished_at,
        "outcome": c.outcome,
    }
