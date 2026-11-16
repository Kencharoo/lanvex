"""Runs approved configuration changes, one at a time per process.

For each change:
  1. The helper on the server backs up the configuration, arms a rollback timer, and applies the change.
  2. Lanvex opens a new SSH connection and asks the helper to verify the result.
  3. If every check passes, Lanvex confirms and the timer is cancelled: the change is permanent.
     If a check fails, Lanvex asks the helper to roll back now.
     If Lanvex cannot reach the server, it does not confirm, and the server's timer restores the
     previous configuration on its own.

Status is reported from what Lanvex has actually seen. When it cannot tell (for example the server
stayed unreachable after its rollback deadline), the change is marked "needs attention", never
"succeeded" or "rolled back".

Several processes may run a runner; a change is claimed with SELECT … FOR UPDATE SKIP LOCKED.
"""

import asyncio
import dataclasses
import os
import socket
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncssh
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lanvex.audit.models import AuditEvent
from lanvex.changes.models import IN_PROGRESS, ChangeStatus, ServerChange
from lanvex.changes.schemas import ChangeRequest
from lanvex.changes.service import note
from lanvex.core.config import Settings
from lanvex.core.db import Database
from lanvex.core.logging import get_logger
from lanvex.core.secrets import VaultError
from lanvex.integrations.linux import helper, transport
from lanvex.integrations.linux.commands import Cmd
from lanvex.netbox.client import NetBoxClient
from lanvex.servers.models import Server
from lanvex.servers.service import ServerService

log = get_logger("lanvex.changes")

CONFIRM_MARGIN = timedelta(seconds=15)  # stop verifying this long before the server's deadline
AFTER_DEADLINE = timedelta(seconds=20)  # give the server's rollback this long before checking on it
STALE_AFTER = timedelta(seconds=120)  # a running change whose runner went quiet this long is reconciled
RETRY_SECONDS = 5.0
DIRECT_TRIES = 3  # tries to reach the verification address after a change
DIRECT_RETRY_SECONDS = 3.0
CONNECT_ERRORS = (transport.SSHError, asyncssh.Error, OSError, helper.HelperUnavailable)


class Clock:
    """Real time. Tests substitute one that moves forward when sleeping."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def _local(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%H:%M:%S UTC")


class ChangeRunner:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        netbox: Callable[[], NetBoxClient | None] = lambda: None,
        clock: Clock | None = None,
    ):
        self.db = db
        self.settings = settings
        self.netbox = netbox
        self.clock = clock or Clock()
        self.name = f"{socket.gethostname()}:{os.getpid()}"
        self.poll_seconds = 3.0
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._busy: set[uuid.UUID] = set()

    # ---- lifecycle -----------------------------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="lanvex-change-runner")

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
        last_reconcile = 0.0
        loop = asyncio.get_running_loop()
        while True:
            worked = False
            try:
                if loop.time() - last_reconcile > 30:
                    await self.reconcile_stale()
                    last_reconcile = loop.time()
                worked = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("change_runner_error")
            if not worked:
                try:
                    await asyncio.wait_for(self._wake.wait(), self.poll_seconds)
                except TimeoutError:
                    pass
                self._wake.clear()

    # ---- claiming --------------------------------------------------------------------------------

    async def run_once(self) -> bool:
        """Claim and run the oldest approved change. Returns False when there was none."""
        async with self.db.sessionmaker() as db:
            c = await db.scalar(
                select(ServerChange)
                .where(ServerChange.status == ChangeStatus.APPROVED)
                .order_by(ServerChange.approved_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if c is None:
                return False
            c.status = ChangeStatus.RUNNING
            c.started_at = c.heartbeat_at = self.clock.now()
            c.runner = self.name
            note(c, "Lanvex started applying the change.")
            await db.commit()
            change_id = c.id
        await self.execute(change_id)
        return True

    async def execute(self, change_id: uuid.UUID) -> None:
        self._busy.add(change_id)
        try:
            async with self.db.sessionmaker() as db:
                c = await db.get(ServerChange, change_id)
                s = await db.get(Server, c.server_id) if c else None
                if c is None or s is None:
                    return
                run = _Execution(self, db, c, s)
                try:
                    await run.run()
                except Exception as e:  # anything unexpected: say exactly how much is known
                    log.exception("change_failed_unexpectedly", change=str(change_id))
                    await db.rollback()
                    c = await db.get(ServerChange, change_id)
                    if c is not None and c.status in IN_PROGRESS:
                        await run.finish(
                            ChangeStatus.NEEDS_ATTENTION,
                            f"Lanvex stopped with an internal error ({type(e).__name__}) while applying the "
                            "change. Unless the change was confirmed, the server restores its previous "
                            "configuration by itself. Use “Check again” to find out.",
                        )
        finally:
            self._busy.discard(change_id)

    # ---- recovery ---------------------------------------------------------------------------------

    async def reconcile_stale(self) -> None:
        """Changes left running by a runner that stopped (crash, restart, deploy)."""
        cutoff = self.clock.now() - STALE_AFTER
        async with self.db.sessionmaker() as db:
            ids = (
                await db.scalars(
                    select(ServerChange.id).where(
                        ServerChange.status.in_(IN_PROGRESS), ServerChange.heartbeat_at < cutoff
                    )
                )
            ).all()
        for change_id in ids:
            if change_id not in self._busy:
                await self.reconcile(change_id)

    async def reconcile(self, change_id: uuid.UUID) -> None:
        """Ask the server what became of a change Lanvex lost track of, once its deadline has passed."""
        async with self.db.sessionmaker() as db:
            c = await db.get(ServerChange, change_id)
            s = await db.get(Server, c.server_id) if c else None
            if c is None or s is None:
                return
            run = _Execution(self, db, c, s)
            deadline = c.deadline_at or (
                (c.started_at or self.clock.now()) + timedelta(seconds=c.rollback_seconds + 60)
            )
            if self.clock.now() < deadline + AFTER_DEADLINE:
                if c.status in IN_PROGRESS:
                    return  # the server may still be waiting for a confirmation; check later
                note(
                    c,
                    f"The server's rollback deadline ({_local(deadline)}) has not passed yet. Check again after it.",
                )
                await db.commit()
                return
            if c.status in IN_PROGRESS:
                note(
                    c,
                    "Lanvex stopped while this change was in progress. Asking the server what happened.",
                    "warning",
                )
            await run.settle_from_server(after_loss=c.status == ChangeStatus.NEEDS_ATTENTION)


def _restored(reason: str | None) -> str:
    """Wording for a rollback Lanvex asked for; the helper's own reason only when it says more."""
    if not reason or "Lanvex's request" in reason:
        return "Lanvex had the server restore its previous configuration."
    return f"The server restored its previous configuration ({reason.rstrip('.')})."


class _Execution:
    def __init__(self, runner: ChangeRunner, db: AsyncSession, c: ServerChange, s: Server):
        self.r = runner
        self.db = db
        self.c = c
        self.s = s
        self.settings = runner.settings
        self.servers = ServerService(db, runner.settings, runner.netbox())
        self.request = ChangeRequest.model_validate(c.request)
        self.id = c.id.hex

    async def save(self) -> None:
        self.c.heartbeat_at = self.r.clock.now()
        await self.db.commit()

    async def finish(self, status: ChangeStatus, outcome: str, level: str = "info") -> None:
        c = self.c
        c.status, c.outcome = status, outcome[:1000]
        c.finished_at = c.heartbeat_at = self.r.clock.now()
        note(c, outcome, level if status != ChangeStatus.SUCCEEDED else "info")
        self.db.add(
            AuditEvent(
                actor_id=c.approved_by_id,
                actor_username=c.approved_by_name,
                action="server.change.execute",
                target_type="server_change",
                target_id=str(c.id),
                target_repr=f"#{c.number} {c.server_name} {c.interface}",
                outcome="success" if status == ChangeStatus.SUCCEEDED else "failure",
                detail={"status": status.value, "outcome": outcome[:500], "summary": c.summary},
            )
        )
        await self.db.commit()
        log.info("change_finished", change=str(c.id), number=c.number, status=status.value)

    async def target(self) -> transport.Target:
        return await self.servers.target_for(self.s)

    def connect(self, target: transport.Target, timeout: float | None = None):  # type: ignore[no-untyped-def]
        return transport.connect(target, timeout or self.settings.ssh_connect_timeout_seconds)

    # ---- 1. apply ------------------------------------------------------------------------------------

    async def run(self) -> None:
        c, s = self.c, self.s
        if not self.settings.automation_enabled:
            return await self.finish(
                ChangeStatus.FAILED,
                "Changes were turned off for this Lanvex installation before the change ran. Nothing was changed.",
                "error",
            )
        if not s.changes_enabled:
            return await self.finish(
                ChangeStatus.FAILED,
                "Changes were disabled for this server before the change ran. Nothing was changed.",
                "error",
            )
        try:
            target = await self.target()
        except (transport.SSHError, VaultError) as e:
            return await self.finish(
                ChangeStatus.FAILED, f"Lanvex could not use this server: {e}. Nothing was changed.", "error"
            )

        preview = c.preview or {}
        payload: dict[str, Any] = self.request.helper_payload() | {
            "id": self.id,
            "rollback_seconds": c.rollback_seconds,
            "expect": {
                "backend": preview.get("backend"),
                "files": {f["path"]: f.get("sha256") for f in preview.get("files", [])},
            },
        }
        connected = False
        try:
            async with self.connect(target) as conn:
                connected = True
                result = await helper.call(conn, Cmd.HELPER_APPLY, payload)
        except helper.HelperRefused as e:
            if e.code == "apply_failed":
                return await self.finish(
                    ChangeStatus.ROLLED_BACK,
                    f"The server could not apply the change and put its previous configuration back: {e.message}",
                    "error",
                )
            return await self.finish(
                ChangeStatus.FAILED, f"The server refused the change: {e.message}", "error"
            )
        except CONNECT_ERRORS as e:
            if not connected or (isinstance(e, helper.HelperUnavailable) and e.code != "timeout"):
                return await self.finish(
                    ChangeStatus.FAILED,
                    f"Lanvex could not run the change: {_why(e)} Nothing was changed.",
                    "error",
                )
            # The connection dropped while the helper was working: it may or may not have applied.
            c.deadline_at = (c.started_at or self.r.clock.now()) + timedelta(seconds=c.rollback_seconds + 30)
            note(c, f"The connection dropped while the change was being applied ({_why(e)}).", "warning")
        else:
            c.deadline_at = datetime.fromtimestamp(float(result["deadline"]), UTC)
            note(
                c,
                f"Applied ({result.get('backend')}). The server restores its previous configuration at "
                f"{_local(c.deadline_at)} unless Lanvex confirms the change first.",
            )
        c.status = ChangeStatus.VERIFYING
        await self.save()
        await self.verify(target)

    # ---- 2. verify, then confirm or roll back ---------------------------------------------------------

    async def verify(self, target: transport.Target) -> None:
        c, clock = self.c, self.r.clock
        assert c.deadline_at is not None
        await clock.sleep(2)
        stop_at = c.deadline_at - CONFIRM_MARGIN
        last_error: Exception | None = None
        while clock.now() < stop_at:
            remaining = (stop_at - clock.now()).total_seconds()
            try:
                async with self.connect(
                    target, min(self.settings.ssh_connect_timeout_seconds, max(remaining, 1))
                ) as conn:
                    state = await helper.call(conn, Cmd.HELPER_STATUS, {"id": self.id})
                    if state["state"] in ("arming", "armed"):
                        raise _NotYet("The server is still applying the change.")
                    if state["state"] != "applied":
                        return await self.settle(state)
                    result = await helper.call(
                        conn, Cmd.HELPER_VERIFY, {"id": self.id, "dns_name": self.request.verify_dns_name}
                    )
                    checks = [
                        {
                            "id": "lanvex",
                            "label": "Lanvex can still sign in",
                            "ok": True,
                            "detail": f"Signed in again at {self.s.address} after the change.",
                        },
                        *result.get("checks", []),
                    ]
                    direct = await self.check_direct(target, stop_at)
                    if direct is not None:
                        checks.insert(1, direct)
                    c.checks = checks
                    await self.save()
                    if result.get("passed") and (direct is None or direct["ok"]):
                        return await self.confirm(conn)
                    return await self.roll_back(conn, checks)
            except _NotYet as e:
                last_error = e
            except helper.HelperRefused as e:
                last_error = e
                if e.code == "unknown_change":
                    return await self.finish(
                        ChangeStatus.FAILED,
                        "The server has no record of this change, so it was never applied.",
                        "error",
                    )
            except CONNECT_ERRORS as e:
                if last_error is None:
                    note(c, f"Lanvex cannot reach the server yet: {_why(e)}", "warning")
                last_error = e
            await self.save()
            await clock.sleep(min(RETRY_SECONDS, max((stop_at - clock.now()).total_seconds(), 0)))

        # Out of time without a verdict: do not confirm. The server's timer restores the old configuration.
        c.checks = [
            {
                "id": "lanvex",
                "label": "Lanvex can still sign in",
                "ok": False,
                "detail": f"No working SSH session to {self.s.address} since the change"
                + (f": {_why(last_error)}" if last_error else "."),
            }
        ]
        note(
            c,
            f"Lanvex could not verify the change in time, so it did not confirm it. The server restores its "
            f"previous configuration at {_local(c.deadline_at)}.",
            "warning",
        )
        await self.save()
        await self.wait_until(c.deadline_at + AFTER_DEADLINE)
        await self.settle_from_server(after_loss=True)

    async def check_direct(self, target: transport.Target, stop_at: datetime) -> dict[str, Any] | None:
        """When Lanvex connects through an address that is not the server's own (NAT, port forwarding, a
        container on the same host), signing in again proves little: it must also reach the server at its
        verification address, one of the server's own. A few tries, as an address can take seconds to
        come back after an interface is reconfigured."""
        address = self.s.verify_address
        if not address:
            return None
        direct = dataclasses.replace(target, address=address)
        why: Exception | None = None
        for attempt in range(DIRECT_TRIES):
            remaining = (stop_at - self.r.clock.now()).total_seconds()
            if remaining <= 1:
                break
            try:
                async with self.connect(direct, min(self.settings.ssh_connect_timeout_seconds, remaining)):
                    return {
                        "id": "lanvex_direct",
                        "label": f"Lanvex reaches the server at {address}",
                        "ok": True,
                        "detail": f"Signed in at {address}, one of the server's own addresses, after the change.",
                    }
            except CONNECT_ERRORS as e:
                why = e
            if attempt + 1 < DIRECT_TRIES:
                await self.r.clock.sleep(DIRECT_RETRY_SECONDS)
        return {
            "id": "lanvex_direct",
            "label": f"Lanvex reaches the server at {address}",
            "ok": False,
            "detail": f"No SSH session to {address} after the change" + (f": {_why(why)}" if why else "."),
        }

    async def confirm(self, conn: asyncssh.SSHClientConnection) -> None:
        try:
            await helper.call(conn, Cmd.HELPER_CONFIRM, {"id": self.id})
        except helper.HelperRefused as e:
            await self.finish(
                ChangeStatus.ROLLED_BACK,
                f"Every check passed, but the server had already restored its previous configuration: {e.message}",
                "warning",
            )
            return await self.refresh()
        await self.finish(
            ChangeStatus.SUCCEEDED,
            "Applied, verified and confirmed. The new configuration is permanent.",
        )
        await self.refresh()

    async def roll_back(self, conn: asyncssh.SSHClientConnection, checks: list[dict[str, Any]]) -> None:
        failed = ", ".join(ch["label"] for ch in checks if ch.get("ok") is False)
        note(
            self.c,
            f"Verification failed: {failed}. Lanvex asked the server to restore its previous configuration.",
            "warning",
        )
        await self.save()
        try:
            result = await helper.call(conn, Cmd.HELPER_ROLLBACK, {"id": self.id})
        except CONNECT_ERRORS + (helper.HelperRefused,) as e:
            note(self.c, f"The rollback request did not complete ({_why(e)}).", "warning")
            await self.save()
            await self.wait_until(self.c.deadline_at + AFTER_DEADLINE)  # type: ignore[operator]
            return await self.settle_from_server(after_loss=True)
        await self.finish(
            ChangeStatus.ROLLED_BACK,
            f"Verification failed ({failed}). " + _restored(result.get("reason")),
            "warning",
        )
        await self.refresh()

    # ---- 3. when Lanvex lost track ------------------------------------------------------------------

    async def wait_until(self, when: datetime) -> None:
        while self.r.clock.now() < when:
            await self.r.clock.sleep(min(10.0, (when - self.r.clock.now()).total_seconds()))
            await self.save()

    async def settle_from_server(self, after_loss: bool) -> None:
        """Ask the server for the change's final state; retry for a while if it is unreachable."""
        target = await self.target()
        give_up = self.r.clock.now() + timedelta(seconds=90)
        last_error: Exception | None = None
        while True:
            try:
                async with self.connect(target) as conn:
                    state = await helper.call(conn, Cmd.HELPER_STATUS, {"id": self.id})
                return await self.settle(state, after_loss)
            except helper.HelperRefused as e:
                if e.code == "unknown_change":
                    return await self.finish(
                        ChangeStatus.FAILED,
                        "The server has no record of this change, so it was never applied.",
                        "error",
                    )
                last_error = e
            except CONNECT_ERRORS as e:
                last_error = e
            if self.r.clock.now() >= give_up:
                break
            await self.r.clock.sleep(RETRY_SECONDS)
            await self.save()
        deadline = self.c.deadline_at
        await self.finish(
            ChangeStatus.NEEDS_ATTENTION,
            "Lanvex cannot reach the server to find out what happened"
            + (f" ({_why(last_error)})" if last_error else "")
            + ". "
            + (
                f"It should have restored its previous configuration at {_local(deadline)}. "
                if deadline
                else ""
            )
            + "Check the server on its console, then use “Check again”.",
            "error",
        )

    async def settle(self, state: dict[str, Any], after_loss: bool = False) -> None:
        name, reason = state.get("state"), state.get("reason")
        if name == "confirmed":
            await self.finish(
                ChangeStatus.SUCCEEDED,
                "The server reports the change as confirmed. The new configuration is permanent.",
            )
        elif name == "rolled_back":
            lead = "Lanvex could not verify the change, so it did not confirm it. " if after_loss else ""
            await self.finish(
                ChangeStatus.ROLLED_BACK,
                f"{lead}The server restored its previous configuration" + (f": {reason}" if reason else "."),
                "warning",
            )
        elif name == "failed":
            await self.finish(
                ChangeStatus.FAILED, "The server did not apply the change. Nothing was changed.", "error"
            )
        else:
            return await self.finish(
                ChangeStatus.NEEDS_ATTENTION,
                f"The server reports the change as '{name}' after its rollback deadline. Check the server, then use “Check again”.",
                "error",
            )
        await self.refresh()

    async def refresh(self) -> None:
        """Read the server again so its page shows the configuration it really has now."""
        try:
            snap = await self.servers.collect(self.s, None)
            await self.db.commit()
            if snap.status != "ok":
                note(self.c, f"Lanvex could not read the server afterwards: {snap.message}", "warning")
                await self.db.commit()
        except Exception as e:  # a failed read must not change the outcome
            log.warning("change_refresh_failed", change=str(self.c.id), error=str(e))


class _NotYet(Exception):
    pass


def _why(e: Exception | None) -> str:
    if e is None:
        return ""
    return getattr(e, "message", None) or str(e) or type(e).__name__
