"""Preparing, approving and tracking configuration changes on servers.

A change moves through: prepared (with an exact preview) → approved → applied by the runner →
verified → confirmed, or rolled back. Nothing reaches a server before an administrator approves the
preview, and the runner (runner.py) is the only code that applies changes.
"""

import dataclasses
import difflib
import fnmatch
import ipaddress
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lanvex.auth.models import User
from lanvex.changes.models import OPEN_STATUSES, ChangeStatus, ServerChange
from lanvex.changes.schemas import (
    ChangeDetail,
    ChangeOut,
    ChangeRequest,
    ConnectionPath,
    HelperInstall,
    HelperStatus,
    Preview,
    Readiness,
)
from lanvex.core.config import Settings
from lanvex.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from lanvex.integrations.linux import helper, transport
from lanvex.integrations.linux.commands import HELPER_PATH, Cmd
from lanvex.integrations.linux.facts import HostFacts
from lanvex.integrations.linux.lanvex_netapply import HELPER_VERSION
from lanvex.servers.models import Server
from lanvex.servers.service import ServerService

SUPPORTED_STACKS = {"netplan-networkd", "netplan-networkmanager", "networkmanager"}


def utcnow() -> datetime:
    return datetime.now(UTC)


def note(c: ServerChange, message: str, level: str = "info") -> None:
    """Append to a change's timeline. Reassigned so SQLAlchemy sees the JSONB change."""
    c.log = [*(c.log or []), {"at": utcnow().isoformat(), "level": level, "message": message[:1000]}]


def to_out(c: ServerChange) -> ChangeOut:
    return ChangeOut(
        id=c.id,
        number=c.number,
        server_id=c.server_id,
        server_name=c.server_name,
        interface=c.interface,
        summary=c.summary,
        status=c.status,
        requested_by=c.requested_by_name,
        created_at=c.created_at,
        approved_by=c.approved_by_name,
        approved_at=c.approved_at,
        started_at=c.started_at,
        finished_at=c.finished_at,
        outcome=c.outcome,
    )


def approve_blocker(c: ServerChange, user: User, settings: Settings) -> str | None:
    if c.status != ChangeStatus.PENDING_APPROVAL:
        return None
    if not settings.automation_enabled:
        return "Changes are turned off for this Lanvex installation."
    if settings.changes_require_second_admin and c.requested_by_id == user.id:
        return "Another administrator has to approve changes you prepared."
    return None


def to_detail(c: ServerChange, user: User, settings: Settings) -> ChangeDetail:
    admin = user.role == "admin"
    blocker = approve_blocker(c, user, settings)
    preview = None
    if c.preview:
        preview = Preview.model_validate(
            {
                **c.preview,
                "files": [
                    {k: f.get(k, "") for k in ("path", "label", "diff", "before", "after")}
                    for f in c.preview.get("files", [])
                ],
            }
        )
    return ChangeDetail(
        **to_out(c).model_dump(),
        request=ChangeRequest.model_validate(c.request),
        before=c.before,
        preview=preview,
        rollback_seconds=c.rollback_seconds,
        deadline_at=c.deadline_at,
        checks=c.checks or [],
        log=c.log or [],
        can_approve=admin and c.status == ChangeStatus.PENDING_APPROVAL and blocker is None,
        approve_blocker=blocker,
        can_cancel=admin and c.status in (ChangeStatus.PENDING_APPROVAL, ChangeStatus.APPROVED),
        can_recheck=admin and c.status == ChangeStatus.NEEDS_ATTENTION,
    )


def summarize(r: ChangeRequest) -> str:
    if r.method == "dhcp":
        parts = ["DHCP"]
    else:
        parts = ["static " + ", ".join(r.addresses), f"gateway {r.gateway}" if r.gateway else "no gateway"]
    if not r.changes_dns:
        parts.append("DNS unchanged")
    else:
        parts.append(("DNS " + ", ".join(r.dns_servers or [])) if r.dns_servers else "no DNS servers")
        if r.dns_search:
            parts.append("search " + ", ".join(r.dns_search))
    return f"{r.interface}: " + " · ".join(parts)


def interface_state(facts: HostFacts, name: str) -> dict[str, Any] | None:
    """The interface's IPv4 settings as last read, in the shape of a change request."""
    iface = next((i for i in facts.interfaces if i.name == name), None)
    if iface is None:
        return None
    static = [a.address for a in iface.addresses if a.family == 4 and not a.dynamic]
    dynamic = [a.address for a in iface.addresses if a.family == 4 and a.dynamic]
    gateways = [r.gateway for r in facts.routes if r.is_default and r.family == 4 and r.interface == name]
    scope = next((x for x in (facts.dns.resolved or []) if x.scope == name), None)
    servers = [s for s in (scope.servers if scope else []) if ":" not in s]
    return {
        "method": "dhcp" if dynamic and not static else "static",
        "addresses": static,
        "dynamic_addresses": dynamic,
        "gateway": gateways[0] if gateways else None,
        "dns_servers": servers,
        "dns_search": [d for d in (scope.domains if scope else []) if d != "~."],
        "dns_known": scope is not None,
        "kind": iface.kind,
        "managed_by": iface.managed_by,
        "master": iface.master,
    }


def unified_diff(before: str, after: str, path: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            f"{path} (now)",
            f"{path} (after)",
        )
    )


class ChangeService:
    def __init__(self, db: AsyncSession, settings: Settings, servers: ServerService):
        self.db = db
        self.settings = settings
        self.servers = servers

    # ---- lookups -----------------------------------------------------------------------------------

    async def get(self, change_id: uuid.UUID) -> ServerChange:
        c = await self.db.get(ServerChange, change_id)
        if c is None:
            raise NotFound("Change not found.")
        return c

    async def query(
        self,
        server_id: uuid.UUID | None = None,
        status: ChangeStatus | None = None,
        limit: int = 100,
        *,
        open_only: bool = False,
    ) -> list[ServerChange]:
        stmt = select(ServerChange).order_by(ServerChange.created_at.desc()).limit(limit)
        if server_id:
            stmt = stmt.where(ServerChange.server_id == server_id)
        if status:
            stmt = stmt.where(ServerChange.status == status)
        if open_only:
            stmt = stmt.where(ServerChange.status.in_(OPEN_STATUSES))
        return list((await self.db.scalars(stmt)).all())

    async def open_change(self, server_id: uuid.UUID) -> ServerChange | None:
        return await self.db.scalar(
            select(ServerChange).where(
                ServerChange.server_id == server_id, ServerChange.status.in_(OPEN_STATUSES)
            )
        )

    # ---- readiness ---------------------------------------------------------------------------------

    async def readiness(self, s: Server) -> Readiness:
        _, facts = await self.servers.latest_facts(s.id)
        stack_key = facts.stack.key if facts else None
        supported = (stack_key in SUPPORTED_STACKS) if facts else None
        blockers: list[str] = []
        if not self.settings.automation_enabled:
            blockers.append(
                "Changes are turned off for this Lanvex installation. An administrator of the Lanvex "
                "server enables them with LANVEX_AUTOMATION_ENABLED=true."
            )
        if facts is None:
            blockers.append("Read the server first, so Lanvex knows how its network is managed.")
        elif not supported:
            blockers.append(
                f"This server's network is managed by {facts.stack.primary}. Lanvex can change servers "
                "that use netplan or NetworkManager."
            )
        if s.helper_state != "ok":
            blockers.append(
                s.helper_message
                if s.helper_state
                else "The change helper has not been checked on this server yet."
            )
        if not s.changes_enabled:
            blockers.append("Changes are not enabled for this server.")
        path = await self.connection(s, facts) if facts else None
        if path is not None and not path.interfaces:
            if not s.verify_address:
                blockers.append(
                    f"Lanvex reaches {s.name} at {s.address}, which is not an address of any of its interfaces "
                    "(port forwarding, NAT, or Lanvex running on this same server, for example through "
                    "host.containers.internal). Lanvex then cannot tell which interface its connection uses, and "
                    "signing in again after a change would not show that the server is still reachable on its "
                    "network. Set a verification address in the server's settings: one of its own addresses that "
                    "Lanvex reaches directly."
                )
            elif not path.verify_interfaces:
                blockers.append(
                    f"The verification address {s.verify_address} is not an address of {s.name} as of its last "
                    "read. Enter one of the server's own addresses, or read the server again."
                )
        open_change = await self.open_change(s.id)
        return Readiness(
            automation_enabled=self.settings.automation_enabled,
            changes_enabled=s.changes_enabled,
            stack_key=stack_key,
            stack_label=facts.stack.primary if facts else None,
            stack_supported=supported,
            helper=HelperStatus(
                state=s.helper_state or "unknown",  # type: ignore[arg-type]
                version=s.helper_version,
                expected_version=HELPER_VERSION,
                message=s.helper_message,
                checked_at=s.helper_checked_at,
            ),
            helper_path=HELPER_PATH,
            helper_sha256=helper.sha256(),
            ready=not blockers,
            blockers=blockers,
            rollback_seconds=self.settings.change_rollback_seconds,
            second_admin_required=self.settings.changes_require_second_admin,
            open_change=to_out(open_change) if open_change else None,
            connection=path,
            protected_interfaces=list(s.protected_interfaces or []),
        )

    async def connection(self, s: Server, facts: HostFacts) -> ConnectionPath:
        """Which of the server's interfaces carry the address Lanvex connects to, and the verification
        address. Loopback does not count: an address forwarded to the server's loopback (Lanvex in a
        container on the same host) says nothing about its network."""
        own: dict[str, set[str]] = {}
        for i in facts.interfaces:
            for a in i.addresses:
                ip = ipaddress.ip_interface(a.address).ip
                if not ip.is_loopback:
                    own.setdefault(str(ip), set()).add(i.name)
        try:
            connect = await transport.resolve(s.address)
        except transport.SSHError:
            connect = []

        def on(addresses: list[str]) -> list[str]:
            return sorted({name for a in addresses for name in own.get(a, set())})

        return ConnectionPath(
            address=s.address,
            interfaces=on(connect),
            verify_address=s.verify_address,
            verify_interfaces=on([s.verify_address]) if s.verify_address else [],
        )

    def verify_target(self, target: transport.Target, s: Server) -> transport.Target | None:
        """The same credentials and pinned host key, at the server's verification address."""
        if not s.verify_address:
            return None
        return dataclasses.replace(target, address=s.verify_address)

    def _record_helper(self, s: Server, state: str, message: str, version: int | None = None) -> None:
        s.helper_state, s.helper_message = state, message[:500]
        s.helper_version, s.helper_checked_at = version, utcnow()

    async def check_helper(self, s: Server) -> Readiness:
        target = await self.servers.target_for(s)
        try:
            async with transport.connect(target, self.settings.ssh_connect_timeout_seconds) as conn:
                data = await helper.call(conn, Cmd.HELPER_VERSION)
        except helper.HelperUnavailable as e:
            self._record_helper(s, e.code, e.message)
        except helper.HelperRefused as e:
            self._record_helper(s, "error", e.message)
        else:
            state, message = helper.helper_version_state(data)
            self._record_helper(s, state, message, data.get("version"))
        await self.db.flush()
        return await self.readiness(s)

    async def upload_helper(self, s: Server) -> HelperInstall:
        target = await self.servers.target_for(s)
        async with transport.connect(target, self.settings.ssh_connect_timeout_seconds) as conn:
            path = await transport.upload(
                conn, helper.UPLOAD_NAME, helper.source(), self.settings.ssh_command_timeout_seconds
            )
        return HelperInstall(
            path=path, sha256=helper.sha256(), commands=helper.install_commands(path, s.username)
        )

    async def set_enabled(self, s: Server, enabled: bool) -> None:
        if enabled and not self.settings.automation_enabled:
            raise Conflict(
                "Changes are turned off for this Lanvex installation (LANVEX_AUTOMATION_ENABLED).",
                code="automation_disabled",
            )
        if not enabled and await self.open_change(s.id):
            raise Conflict("Finish or cancel the open change for this server first.")
        s.changes_enabled = enabled

    # ---- preparing --------------------------------------------------------------------------------

    async def prepare(self, s: Server, req: ChangeRequest, user: User) -> ServerChange:
        readiness = await self.readiness(s)
        if readiness.open_change:
            raise Conflict(
                f"Change #{readiness.open_change.number} for this server is still open. Finish or cancel it first."
            )
        if not readiness.ready:
            raise Conflict(" ".join(readiness.blockers), code="changes_not_ready")

        # Plan against the server as it is now, not as it was last read.
        snap = await self.servers.collect(s, user)
        await self.db.commit()  # the read is real data even if preparing the change fails below
        if snap.status != "ok" or not snap.facts:
            raise Conflict(f"Lanvex could not read the server: {snap.message}")
        facts = HostFacts.model_validate(snap.facts)
        if facts.stack.key not in SUPPORTED_STACKS:
            raise Conflict(f"Lanvex cannot change servers managed by {facts.stack.primary}.")
        guard = next((p for p in s.protected_interfaces or [] if fnmatch.fnmatchcase(req.interface, p)), None)
        if guard is not None:
            raise ValidationFailed(
                f"{req.interface} is protected on {s.name} ({guard}): another tool manages it. Change it there, "
                "or remove it from the server's protected interfaces.",
                fields={"interface": ["Protected."]},
            )
        before = interface_state(facts, req.interface)
        if before is None:
            raise ValidationFailed(
                f"{s.name} has no interface {req.interface}.", fields={"interface": ["Not on the server."]}
            )
        if before["kind"] == "loopback" or before["master"]:
            raise ValidationFailed(
                f"{req.interface} is a loopback interface or part of a bridge or bond and cannot be changed.",
                fields={"interface": ["Not changeable."]},
            )
        warnings = await self._safety(s, req, before)
        unresolved = await self.db.scalar(
            select(ServerChange.number)
            .where(ServerChange.server_id == s.id, ServerChange.status == ChangeStatus.NEEDS_ATTENTION)
            .order_by(ServerChange.created_at.desc())
            .limit(1)
        )
        if unresolved is not None:
            warnings.insert(
                0,
                f"Change #{unresolved} needs attention: Lanvex does not know whether it is in place. "
                "Check it first; this preview shows the server as it is now.",
            )

        target = await self.servers.target_for(s)
        direct = self.verify_target(target, s)
        if direct is not None:
            try:
                async with transport.connect(direct, self.settings.ssh_connect_timeout_seconds):
                    pass
            except transport.SSHError as e:
                raise Conflict(
                    f"Lanvex cannot reach {s.name} at its verification address {s.verify_address}: {e.message} "
                    "It only changes a server it can check on one of the server's own addresses afterwards. If Lanvex runs "
                    "on this server itself, it cannot do that: run Lanvex on another machine to change this server.",
                    code="cannot_verify",
                ) from e
        try:
            async with transport.connect(target, self.settings.ssh_connect_timeout_seconds) as conn:
                plan = await helper.call(conn, Cmd.HELPER_PLAN, req.helper_payload())
        except helper.HelperRefused as e:
            if e.code == "no_change":
                raise ValidationFailed(
                    "This is already the interface's configuration. Nothing to change.", code="no_change"
                ) from e
            raise ValidationFailed(f"The server cannot make this change: {e.message}", code=e.code) from e
        except helper.HelperUnavailable as e:
            self._record_helper(s, e.code, e.message)
            await self.db.commit()
            raise

        files = [
            {**f, "diff": unified_diff(f["before"], f["after"], f["path"])} for f in plan.get("files", [])
        ]
        c = ServerChange(
            server_id=s.id,
            server_name=s.name,
            interface=req.interface,
            summary=summarize(req),
            request=req.model_dump(mode="json"),
            before=before,
            preview={
                "backend": plan["backend"],
                "files": files,
                "commands": plan.get("commands", []),
                "warnings": warnings + list(plan.get("warnings", [])),
            },
            status=ChangeStatus.PENDING_APPROVAL,
            rollback_seconds=self.settings.change_rollback_seconds,
            requested_by_id=user.id,
            requested_by_name=user.username,
            log=[],
        )
        note(c, f"Prepared by {user.username}. Nothing has been changed on the server.")
        self.db.add(c)
        try:
            await self.db.flush()
        except IntegrityError as e:  # another change was prepared at the same moment
            await self.db.rollback()
            raise Conflict("Another change for this server was prepared at the same time.") from e
        return c

    async def _safety(self, s: Server, req: ChangeRequest, before: dict[str, Any]) -> list[str]:
        """Lanvex's own rules on top of the helper's: never cut the connection it relies on."""
        warnings: list[str] = []
        mgmt = set(await transport.resolve(s.address))
        if s.verify_address:
            mgmt.add(s.verify_address)
        current = {a.split("/")[0] for a in before["addresses"] + before["dynamic_addresses"]}
        on_iface = sorted(mgmt & current)
        if on_iface:
            keep = {a.split("/")[0] for a in req.addresses}
            if req.method == "dhcp" or not set(on_iface) <= keep:
                raise ValidationFailed(
                    f"Lanvex connects to {s.name} at {', '.join(on_iface)}, which is on {req.interface}. "
                    "Keep that address as a static address, or Lanvex loses the connection. To move the "
                    "server: add the new address, change the server's address in Lanvex, then remove "
                    "the old one.",
                    fields={"addresses": [f"Must include {', '.join(on_iface)}."]},
                )
            warnings.append(
                f"Lanvex reaches {s.name} through {req.interface}. If the change breaks that connection, the "
                f"server restores its previous configuration after {self.settings.change_rollback_seconds} seconds."
            )
        if req.method == "static" and req.gateway != before["gateway"]:
            if req.gateway is None:
                warnings.append(
                    f"{req.interface} will no longer have a default gateway ({before['gateway']} today)."
                )
            elif before["gateway"] is None:
                warnings.append(f"{req.interface} gets a default gateway: {req.gateway}.")
            else:
                warnings.append(f"The default gateway changes from {before['gateway']} to {req.gateway}.")
        if before["method"] == "dhcp" and req.method == "static":
            warnings.append(f"{req.interface} stops using DHCP.")
        return warnings

    # ---- approval --------------------------------------------------------------------------------

    async def approve(self, c: ServerChange, user: User, confirm_name: str) -> ServerChange:
        if c.status != ChangeStatus.PENDING_APPROVAL:
            raise Conflict(
                f"Change #{c.number} is {c.status.value.replace('_', ' ')}, not waiting for approval."
            )
        blocker = approve_blocker(c, user, self.settings)
        if blocker:
            raise PermissionDenied(blocker)
        s = await self.servers.get(c.server_id)
        if not s.changes_enabled:
            raise Conflict("Changes are no longer enabled for this server.")
        if confirm_name != s.name:
            raise ValidationFailed(
                "Type the server's name exactly to approve.", fields={"confirm_name": [f"Type {s.name}."]}
            )
        c.status = ChangeStatus.APPROVED
        c.approved_by_id, c.approved_by_name, c.approved_at = user.id, user.username, utcnow()
        note(c, f"Approved by {user.username}.")
        return c

    async def cancel(self, c: ServerChange, user: User) -> ServerChange:
        if c.status not in (ChangeStatus.PENDING_APPROVAL, ChangeStatus.APPROVED):
            raise Conflict(
                f"Change #{c.number} is {c.status.value.replace('_', ' ')} and can no longer be cancelled."
            )
        c.status = ChangeStatus.CANCELLED
        c.cancelled_by_name, c.finished_at = user.username, utcnow()
        c.outcome = f"Cancelled by {user.username}. Nothing was changed on the server."
        note(c, c.outcome)
        return c
