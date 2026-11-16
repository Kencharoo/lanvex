"""Server registration, credentials, connection tests, collection, and NetBox comparison.

Everything here reads from servers. Changing a server's configuration lives in lanvex.changes.
"""

import asyncio
import ipaddress
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncssh
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from lanvex.auth.models import User
from lanvex.core.config import Settings, get_settings
from lanvex.core.errors import Conflict, NotFound, ValidationFailed
from lanvex.core.secrets import Vault
from lanvex.integrations.base import capabilities
from lanvex.integrations.linux import adapter, transport
from lanvex.integrations.linux.commands import Cmd
from lanvex.integrations.linux.facts import HostFacts
from lanvex.netbox.client import NetBoxClient
from lanvex.servers.history import HistoryChange, fingerprint
from lanvex.servers.history import changes as history_changes
from lanvex.servers.models import (
    AuthMethod,
    ConnectionStatus,
    DiscoveredAddress,
    Server,
    ServerSnapshot,
)
from lanvex.servers.record import Recorder, RecordPlan, check_addresses
from lanvex.servers.schemas import (
    Comparison,
    ComparisonRow,
    Credentials,
    HistoryEntry,
    HostKeyOut,
    NetBoxLink,
    NetBoxRef,
    PinnedHostKey,
    ServerCreate,
    ServerOut,
    ServerUpdate,
    TestResult,
)

NOT_STORED = "[not stored: this file can contain secrets]"
SECRET_BEARING = {Cmd.IFUPDOWN_CONFIG.value, Cmd.IFUPDOWN_INCLUDES.value}
_locks: dict[uuid.UUID, asyncio.Lock] = {}


def _lock(server_id: uuid.UUID) -> asyncio.Lock:
    return _locks.setdefault(server_id, asyncio.Lock())


def authorized_keys_line(public_key: str | None) -> str | None:
    # `restrict` turns off port, agent and X11 forwarding and PTY allocation; commands still run.
    return f"restrict {public_key}" if public_key else None


def to_out(s: Server, netbox: NetBoxRef | None = None) -> ServerOut:
    try:
        algorithm = asyncssh.import_public_key(s.host_key).get_algorithm()
    except (asyncssh.KeyImportError, ValueError):
        algorithm = None
    if netbox is None and s.netbox_object_type and s.netbox_object_id:
        netbox = NetBoxRef(object_type=s.netbox_object_type, object_id=s.netbox_object_id)
    return ServerOut(
        id=s.id,
        name=s.name,
        description=s.description,
        address=s.address,
        port=s.port,
        username=s.username,
        adapter=s.adapter,
        capabilities=sorted(c.value for c in capabilities(s.adapter)),
        auth_method=s.auth_method,
        public_key=s.public_key,
        authorized_keys_line=authorized_keys_line(s.public_key),
        host_key_fingerprint=s.host_key_fingerprint,
        host_key_algorithm=algorithm,
        host_key_pinned_at=s.host_key_pinned_at,
        netbox=netbox,
        last_status=s.last_status,
        last_status_message=s.last_status_message,
        last_checked_at=s.last_checked_at,
        last_collected_at=s.last_collected_at,
        os_pretty_name=s.os_pretty_name,
        os_supported=s.os_supported,
        network_stack=s.network_stack,
        changes_enabled=s.changes_enabled,
        read_interval_minutes=s.read_interval_minutes,
        read_interval=read_interval(s, get_settings()),
        next_read_at=s.next_read_at if read_interval(s, get_settings()) and not s.reads_paused else None,
        reads_paused=s.reads_paused,
        verify_address=s.verify_address,
        protected_interfaces=list(s.protected_interfaces or []),
        confirm_in_browser=s.confirm_in_browser,
        created_at=s.created_at,
    )


def read_interval(s: Server, settings: Settings) -> int:
    """Minutes between scheduled reads of this server; 0 when it is not read on a schedule."""
    if not settings.server_read_interval_minutes:
        return 0  # schedule switched off for the installation
    return (
        settings.server_read_interval_minutes if s.read_interval_minutes is None else s.read_interval_minutes
    )


SCHEDULE = "(schedule)"  # requested_by of scheduled reads; cannot be a username
AUTH_FAILURES_BEFORE_PAUSE = 2  # consecutive scheduled sign-in failures before reads pause


def verified_host_key(k: PinnedHostKey) -> tuple[str, str]:
    """Parse the key and make sure it is the one whose fingerprint the administrator checked."""
    try:
        key = asyncssh.import_public_key(k.public_key.strip())
    except (asyncssh.KeyImportError, ValueError) as e:
        raise ValidationFailed(
            "The host key is not a valid SSH public key.", fields={"host_key": ["Invalid key."]}
        ) from e
    fp = key.get_fingerprint("sha256")
    if fp != k.fingerprint_sha256.strip():
        raise ValidationFailed(
            "The host key does not match the fingerprint that was confirmed. Scan the server again.",
            fields={"host_key": ["Fingerprint mismatch."]},
        )
    return key.export_public_key("openssh").decode().strip(), fp


def build_credentials(c: Credentials, name: str) -> tuple[dict[str, Any], str | None]:
    """Returns (secret material to encrypt, public key to display)."""
    if c.method == AuthMethod.GENERATED_KEY:
        key = asyncssh.generate_private_key("ssh-ed25519", comment=f"lanvex@{name}")
        return {"private_key": key.export_private_key("openssh").decode()}, key.export_public_key(
            "openssh"
        ).decode().strip()
    if c.method == AuthMethod.PRIVATE_KEY:
        try:
            key = asyncssh.import_private_key((c.private_key or "").strip(), c.passphrase or None)
        except asyncssh.KeyEncryptionError as e:
            raise ValidationFailed(
                "The passphrase does not unlock this key.", fields={"passphrase": ["Incorrect passphrase."]}
            ) from e
        except (asyncssh.KeyImportError, ValueError) as e:
            if "passphrase" in str(e).lower() and not c.passphrase:
                raise ValidationFailed(
                    "The private key is encrypted. Enter its passphrase.",
                    fields={"passphrase": ["Required."]},
                ) from e
            raise ValidationFailed(
                "The private key could not be read.", fields={"private_key": [str(e)]}
            ) from e
        # Stored decrypted inside the vault; the passphrase is not kept.
        return {"private_key": key.export_private_key("openssh").decode()}, key.export_public_key(
            "openssh"
        ).decode().strip()
    return {"password": c.password}, None


class ServerService:
    def __init__(self, db: AsyncSession, settings: Settings, nb: NetBoxClient | None):
        self.db = db
        self.settings = settings
        self.nb = nb

    # ---- lookups -------------------------------------------------------------

    async def get(self, server_id: uuid.UUID) -> Server:
        s = await self.db.get(Server, server_id)
        if s is None:
            raise NotFound("Server not found.")
        return s

    async def all(self) -> list[Server]:
        return list((await self.db.scalars(select(Server).order_by(func.lower(Server.name)))).all())

    async def _unique_name(self, name: str, exclude: uuid.UUID | None = None) -> None:
        stmt = select(Server.id).where(func.lower(Server.name) == name.lower())
        if exclude:
            stmt = stmt.where(Server.id != exclude)
        if await self.db.scalar(stmt):
            raise Conflict("A server with this name already exists.", fields={"name": ["Already in use."]})

    async def _check_target(self, address: str) -> list[str]:
        addresses = await transport.resolve(address)
        transport.check_allowed(addresses, self.settings.server_allowed_networks)
        return addresses

    async def netbox_ref(self, link: NetBoxLink | None, *, strict: bool) -> NetBoxRef | None:
        if link is None:
            return None
        if self.nb is None:
            if strict:
                raise ValidationFailed("Linking to NetBox requires a configured NetBox connection.")
            return NetBoxRef(object_type=link.object_type, object_id=link.object_id)
        path = "dcim/devices/" if link.object_type == "dcim.device" else "virtualization/virtual-machines/"
        try:
            obj = await self.nb.get(f"{path}{link.object_id}/", {"brief": True})
        except NotFound as e:
            if strict:
                raise ValidationFailed(
                    "That NetBox object does not exist.", fields={"netbox": ["Not found."]}
                ) from e
            return NetBoxRef(object_type=link.object_type, object_id=link.object_id)
        except Exception:
            if strict:
                raise
            return NetBoxRef(object_type=link.object_type, object_id=link.object_id)
        return NetBoxRef(
            object_type=link.object_type,
            object_id=link.object_id,
            name=obj.get("name") or obj.get("display"),
            url=self.settings.public_netbox_url(obj.get("display_url") or obj.get("url")),
        )

    # ---- registration ----------------------------------------------------------

    async def scan(self, address: str, port: int) -> HostKeyOut:
        resolved = await self._check_target(address)
        hk = await transport.scan_host_key(address, port, self.settings.ssh_connect_timeout_seconds)
        return HostKeyOut(
            algorithm=hk.algorithm,
            fingerprint_sha256=hk.fingerprint_sha256,
            public_key=hk.public_key,
            resolved_addresses=resolved,
        )

    async def create(self, data: ServerCreate, user: User) -> Server:
        vault = Vault.from_settings(self.settings)
        await self._unique_name(data.name)
        await self._check_target(data.address)
        host_key, fp = verified_host_key(data.host_key)
        secret, public_key = build_credentials(data.credentials, data.name)
        await self.netbox_ref(data.netbox, strict=True)
        s = Server(
            name=data.name,
            description=data.description,
            address=data.address,
            port=data.port,
            username=data.username,
            auth_method=data.credentials.method,
            credential_ciphertext=vault.encrypt(secret),
            public_key=public_key,
            host_key=host_key,
            host_key_fingerprint=fp,
            netbox_object_type=data.netbox.object_type if data.netbox else None,
            netbox_object_id=data.netbox.object_id if data.netbox else None,
            created_by_id=user.id,
        )
        minutes = read_interval(s, self.settings)
        # Added servers are read right away by whoever adds them; the schedule takes over after that.
        s.next_read_at = datetime.now(UTC) + timedelta(minutes=minutes) if minutes else None
        self.db.add(s)
        await self.db.flush()
        return s

    async def update(self, s: Server, data: ServerUpdate) -> Server:
        sent = data.model_fields_set
        if data.name is not None and data.name != s.name:
            await self._unique_name(data.name, exclude=s.id)
            s.name = data.name
        if data.description is not None:
            s.description = data.description
        if data.username is not None:
            s.username = data.username
        moving = (data.address is not None and data.address != s.address) or (
            data.port is not None and data.port != s.port
        )
        if moving and data.host_key is None:
            raise ValidationFailed(
                "Changing the address or port requires verifying the host key again.",
                fields={"host_key": ["Scan the new address and confirm its fingerprint."]},
            )
        if data.address is not None:
            await self._check_target(data.address)
            s.address = data.address
        if data.port is not None:
            s.port = data.port
        if data.host_key is not None:
            s.host_key, s.host_key_fingerprint = verified_host_key(data.host_key)
            s.host_key_pinned_at = datetime.now(UTC)
            if s.last_status == ConnectionStatus.HOST_KEY_MISMATCH:
                s.last_status, s.last_status_message = None, "New host key pinned; not tested yet."
        if "read_interval_minutes" in sent:
            s.read_interval_minutes = data.read_interval_minutes
            minutes = read_interval(s, self.settings)
            s.next_read_at = datetime.now(UTC) + timedelta(minutes=minutes) if minutes else None
        if "verify_address" in sent:
            if data.verify_address is not None:
                await self._check_target(data.verify_address)
            s.verify_address = data.verify_address
        if data.protected_interfaces is not None:
            s.protected_interfaces = data.protected_interfaces
        if data.confirm_in_browser is not None:
            s.confirm_in_browser = data.confirm_in_browser
        if data.unlink_netbox:
            s.netbox_object_type = s.netbox_object_id = None
        elif "netbox" in sent and data.netbox is not None:
            await self.netbox_ref(data.netbox, strict=True)
            s.netbox_object_type, s.netbox_object_id = data.netbox.object_type, data.netbox.object_id
        return s

    async def replace_credentials(self, s: Server, c: Credentials) -> Server:
        vault = Vault.from_settings(self.settings)
        secret, public_key = build_credentials(c, s.name)
        s.auth_method = c.method
        s.credential_ciphertext = vault.encrypt(secret)
        s.public_key = public_key
        s.last_status, s.last_status_message = None, "Credentials replaced; not tested yet."
        return s

    # ---- connecting ------------------------------------------------------------

    async def target_for(self, s: Server) -> transport.Target:
        """Connection details for a server, after checking it is inside the allowed networks."""
        await self._check_target(s.address)
        return self._target(s)

    def _target(self, s: Server) -> transport.Target:
        secret = Vault.from_settings(self.settings).decrypt(s.credential_ciphertext)
        return transport.Target(
            address=s.address,
            port=s.port,
            username=s.username,
            host_key=s.host_key,
            private_key=secret.get("private_key"),
            password=secret.get("password"),
        )

    def _record(self, s: Server, status: ConnectionStatus, message: str) -> None:
        s.last_status, s.last_status_message, s.last_checked_at = status, message[:500], datetime.now(UTC)

    async def test(self, s: Server) -> TestResult:
        try:
            await self._check_target(s.address)
            async with _lock(s.id):
                hostname = await adapter.test_connection(
                    self._target(s), self.settings.ssh_connect_timeout_seconds
                )
        except transport.SSHError as e:
            status = (
                ConnectionStatus(e.code)
                if e.code in ConnectionStatus._value2member_map_
                else ConnectionStatus.ERROR
            )
            self._record(s, status, e.message)
            return TestResult(status=status, message=e.message)
        msg = f"Signed in as {s.username} on {hostname}."
        self._record(s, ConnectionStatus.OK, msg)
        return TestResult(status=ConnectionStatus.OK, message=msg, remote_hostname=hostname)

    async def collect(self, s: Server, user: User | None, *, scheduled: bool = False) -> ServerSnapshot:
        """Read the server. A read that finds the same state as the latest snapshot is not stored again:
        that snapshot is confirmed instead, so snapshots are the server's history of changes."""
        snap = ServerSnapshot(
            server_id=s.id, requested_by=user.username if user else (SCHEDULE if scheduled else None)
        )
        try:
            await self._check_target(s.address)
            async with _lock(s.id):
                c = await adapter.collect(
                    self._target(s),
                    self.settings.ssh_connect_timeout_seconds,
                    self.settings.ssh_command_timeout_seconds,
                )
        except transport.SSHError as e:
            status = (
                ConnectionStatus(e.code)
                if e.code in ConnectionStatus._value2member_map_
                else ConnectionStatus.ERROR
            )
            snap.status, snap.message = status, e.message[:500]
            self._record(s, status, e.message)
        else:
            snap.status = ConnectionStatus.OK
            snap.duration_ms = c.duration_ms
            snap.facts = c.facts.model_dump(mode="json")
            snap.raw = {
                k: (v | {"stdout": NOT_STORED}) if k in SECRET_BEARING else v for k, v in c.raw.items()
            }
            snap.message = f"{len(c.facts.warnings)} warning(s)" if c.facts.warnings else None
            self._record(s, ConnectionStatus.OK, f"Collected in {c.duration_ms} ms.")
            s.last_collected_at = snap.taken_at = datetime.now(UTC)
            s.os_pretty_name = c.facts.system.os.pretty_name
            s.os_supported = c.facts.system.os.supported
            s.network_stack = c.facts.stack.primary
            await self._remember_neighbors(s, c.facts)
            if s.reads_paused:  # a successful read ends a pause
                self.resume_reads(s)
        snap.fingerprint = fingerprint(snap.facts) if snap.facts else None
        latest = await self.db.scalar(
            select(ServerSnapshot)
            .where(ServerSnapshot.server_id == s.id)
            .order_by(ServerSnapshot.taken_at.desc())
            .limit(1)
        )
        if (
            latest is not None
            and latest.status == snap.status
            and latest.fingerprint == snap.fingerprint
            and (snap.fingerprint is not None or latest.message == snap.message)
        ):
            latest.confirmed_at = datetime.now(UTC)
            latest.reads += 1
            if snap.facts is not None:
                # The same state, read again: keep what changes without being a new state (leases,
                # neighbours, when it was read), so pages built on the latest read show the latest read.
                latest.facts, latest.raw, latest.duration_ms = snap.facts, snap.raw, snap.duration_ms
            await self.db.flush()
            self._maybe_pause(s, latest, scheduled)
            return latest
        self.db.add(snap)
        await self.db.flush()
        await self._prune(s.id)
        self._maybe_pause(s, snap, scheduled)
        return snap

    def _maybe_pause(self, s: Server, result: ServerSnapshot, scheduled: bool) -> None:
        """Stop reading on a schedule when every attempt makes things worse: each failed sign-in counts
        against the server's protection (OpenSSH's PerSourcePenalties, fail2ban), which then blocks
        Lanvex's address, and behind NAT everyone else's with it."""
        if not scheduled or s.reads_paused:
            return
        if result.status == ConnectionStatus.REFUSED:
            reason = (
                "The server refuses connections from Lanvex's address for now, after failed sign-ins (OpenSSH "
                "PerSourcePenalties). Every attempt extends that, so Lanvex stopped reading it on a schedule."
            )
        elif result.status == ConnectionStatus.AUTH_FAILED and result.reads >= AUTH_FAILURES_BEFORE_PAUSE:
            reason = (
                f"The server rejected Lanvex's credentials {result.reads} times in a row. Lanvex stopped reading it "
                "on a schedule, so the server's protection against failed sign-ins does not block Lanvex's address."
            )
        else:
            return
        s.reads_paused = (
            reason
            + " Fix the cause, then read the server once: scheduled reads resume after a successful read."
        )
        s.next_read_at = None

    def resume_reads(self, s: Server) -> None:
        s.reads_paused = None
        minutes = read_interval(s, self.settings)
        s.next_read_at = datetime.now(UTC) + timedelta(minutes=minutes) if minutes else None

    async def _remember_neighbors(self, s: Server, facts: HostFacts) -> None:
        """Keep the hosts in the server's neighbour table, and the devices its DHCP gave an address, for
        discovery; forget old ones."""
        now = datetime.now(UTC)
        rows = {
            lease.ip: {
                "id": uuid.uuid4(),
                "server_id": s.id,
                "address": lease.ip,
                "family": 4,
                "interface": iface,
                "mac": lease.mac,
                "router": False,
                "first_seen": now,
                "last_seen": now,
            }
            for lease in (facts.dhcp.leases if facts.dhcp else [])
            if (iface := _lease_interface(facts, lease.ip))  # leases of Lanvex's own pools only
        }
        rows |= {
            n.address: {
                "id": uuid.uuid4(),
                "server_id": s.id,
                "address": n.address,
                "family": n.family,
                "interface": n.interface,
                "mac": n.mac,
                "router": n.router,
                "first_seen": now,
                "last_seen": now,
            }
            for n in facts.neighbors
        }
        if rows:
            stmt = pg_insert(DiscoveredAddress).values(list(rows.values()))
            await self.db.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_discovered_addresses_server_address",
                    set_={
                        "interface": stmt.excluded.interface,
                        "mac": stmt.excluded.mac,
                        "router": stmt.excluded.router,
                        "last_seen": stmt.excluded.last_seen,
                    },
                )
            )
        await self.db.execute(
            delete(DiscoveredAddress).where(
                DiscoveredAddress.server_id == s.id,
                DiscoveredAddress.last_seen < now - timedelta(days=self.settings.discovery_retention_days),
            )
        )

    async def history(self, server_id: uuid.UUID, limit: int = 100) -> list[HistoryEntry]:
        """Newest first: each kept state and what differed from the readable state before it."""
        snaps = list(
            reversed(
                (
                    await self.db.scalars(
                        select(ServerSnapshot)
                        .where(ServerSnapshot.server_id == server_id)
                        .order_by(ServerSnapshot.taken_at.desc())
                        .limit(limit + 1)
                    )
                ).all()
            )
        )
        entries: list[HistoryEntry] = []
        previous: HostFacts | None = None
        for i, snap in enumerate(snaps):
            facts = HostFacts.model_validate(snap.facts) if snap.facts else None
            found: list[HistoryChange] = []
            if facts and previous:
                found = history_changes(previous, facts)
            if facts:
                previous = facts
            entries.append(
                HistoryEntry(
                    snapshot_id=snap.id,
                    taken_at=snap.taken_at,
                    confirmed_at=snap.confirmed_at,
                    reads=snap.reads,
                    status=snap.status,
                    message=snap.message,
                    requested_by=snap.requested_by,
                    first=i == 0,
                    changes=found,
                )
            )
        # The extra (oldest) snapshot was only read for comparison when there are more than `limit`.
        return list(reversed(entries[-limit:]))

    async def _prune(self, server_id: uuid.UUID) -> None:
        keep = self.settings.server_snapshot_retention
        old = (
            await self.db.scalars(
                select(ServerSnapshot.id)
                .where(ServerSnapshot.server_id == server_id)
                .order_by(ServerSnapshot.taken_at.desc())
                .offset(keep)
            )
        ).all()
        if old:
            await self.db.execute(delete(ServerSnapshot).where(ServerSnapshot.id.in_(old)))

    async def snapshots(self, server_id: uuid.UUID, limit: int = 50) -> list[ServerSnapshot]:
        return list(
            (
                await self.db.scalars(
                    select(ServerSnapshot)
                    .where(ServerSnapshot.server_id == server_id)
                    .order_by(ServerSnapshot.taken_at.desc())
                    .limit(limit)
                )
            ).all()
        )

    async def latest_facts(self, server_id: uuid.UUID) -> tuple[ServerSnapshot | None, HostFacts | None]:
        snap = await self.db.scalar(
            select(ServerSnapshot)
            .where(ServerSnapshot.server_id == server_id, ServerSnapshot.status == ConnectionStatus.OK)
            .order_by(ServerSnapshot.taken_at.desc())
            .limit(1)
        )
        return snap, (HostFacts.model_validate(snap.facts) if snap and snap.facts else None)

    # ---- documented vs actual ------------------------------------------------------

    async def record_plan(self, s: Server, addresses: list[str]) -> tuple[Recorder, RecordPlan, list[Any]]:
        """Plan the NetBox writes that make the documentation match the server's last read."""
        if self.nb is None:
            raise ValidationFailed("Recording needs a configured NetBox connection.")
        if not s.netbox_object_id:
            raise ValidationFailed(f"Link this server to its {self.nb.name} device or virtual machine first.")
        _, facts = await self.latest_facts(s.id)
        if facts is None:
            raise ValidationFailed("Read the server first.")
        comparison = await self.compare(s)
        assert comparison.netbox is not None
        recorder = Recorder(self.nb, s, comparison.netbox, facts)
        plan, steps = await recorder.plan(comparison.rows, check_addresses(addresses))
        return recorder, plan, steps

    async def compare(self, s: Server) -> Comparison:
        snap, facts = await self.latest_facts(s.id)
        link = (
            NetBoxLink(object_type=s.netbox_object_type, object_id=s.netbox_object_id)
            if s.netbox_object_id
            else None
        )  # type: ignore[arg-type]
        ref = await self.netbox_ref(link, strict=False)
        suggestions: list[NetBoxRef] = []
        if facts is None:
            return Comparison(
                linked=link is not None,
                netbox=ref,
                suggestions=[],
                snapshot_taken_at=None,
                rows=[],
                counts={},
            )

        actual: dict[str, tuple[str, str]] = {}  # host -> (address/len, interface)
        for iface in facts.interfaces:
            if iface.kind == "loopback":
                continue
            for a in iface.addresses:
                if a.scope in ("host", "link"):
                    continue
                actual[str(ipaddress.ip_interface(a.address).ip)] = (a.address, iface.name)

        documented: dict[str, tuple[str, str | None, int]] = {}
        if self.nb is not None and link is not None:
            key = "device_id" if link.object_type == "dcim.device" else "virtual_machine_id"
            for ip in await self.nb.list_all("ipam/ip-addresses/", {key: link.object_id}, cap=2000):
                obj = ip.get("assigned_object") or {}
                documented[str(ipaddress.ip_interface(ip["address"]).ip)] = (
                    ip["address"],
                    obj.get("name"),
                    ip["id"],
                )

        rows: list[ComparisonRow] = []
        for host, (addr, iface) in actual.items():
            if host in documented:
                nb_addr, nb_iface, nb_id = documented[host]
                status = "match"
                if nb_addr != addr:
                    status = "mask_differs"
                elif nb_iface and nb_iface != iface:
                    status = "interface_differs"
                rows.append(
                    ComparisonRow(
                        address=host,
                        status=status,
                        server_address=addr,
                        server_interface=iface,  # type: ignore[arg-type]
                        netbox_address=nb_addr,
                        netbox_interface=nb_iface,
                        netbox_ip_id=nb_id,
                    )
                )
            else:
                rows.append(
                    ComparisonRow(
                        address=host, status="only_on_server", server_address=addr, server_interface=iface
                    )
                )
        for host, (nb_addr, nb_iface, nb_id) in documented.items():
            if host not in actual:
                rows.append(
                    ComparisonRow(
                        address=host,
                        status="only_in_netbox",
                        netbox_address=nb_addr,
                        netbox_interface=nb_iface,
                        netbox_ip_id=nb_id,
                    )
                )

        if self.nb is not None:
            undocumented = [r for r in rows if r.status == "only_on_server"]
            if undocumented:
                found = await self.nb.list_all(
                    "ipam/ip-addresses/", {"address": [r.address for r in undocumented]}, cap=1000
                )
                where: dict[str, str] = {}
                for ip in found:
                    obj = ip.get("assigned_object") or {}
                    parent = obj.get("device") or obj.get("virtual_machine") or {}
                    label = (
                        f"{ip['address']} on {parent.get('name')} / {obj.get('name')}"
                        if parent
                        else f"{ip['address']}, not assigned"
                    )
                    where.setdefault(str(ipaddress.ip_interface(ip["address"]).ip), label)
                for r in undocumented:
                    r.recorded_elsewhere = where.get(r.address)
            if link is None and facts.system.hostname:
                short = facts.system.hostname.split(".")[0]
                for path, otype in (
                    ("dcim/devices/", "dcim.device"),
                    ("virtualization/virtual-machines/", "virtualization.virtualmachine"),
                ):
                    try:
                        for o in (
                            await self.nb.page(
                                path, {"name": [facts.system.hostname, short], "brief": True}, limit=5
                            )
                        ).results:
                            suggestions.append(
                                NetBoxRef(
                                    object_type=otype,
                                    object_id=o["id"],
                                    name=o.get("name"),
                                    url=self.settings.public_netbox_url(o.get("display_url")),
                                )
                            )
                    except Exception:  # noqa: S112 - suggestions are best-effort (e.g. no permission on VMs)
                        continue

        order = {
            "only_on_server": 0,
            "only_in_netbox": 1,
            "mask_differs": 2,
            "interface_differs": 3,
            "match": 4,
        }
        rows.sort(
            key=lambda r: (
                order[r.status],
                ipaddress.ip_address(r.address).version,
                ipaddress.ip_address(r.address),
            )
        )
        counts: dict[str, int] = {}
        for r in rows:
            counts[r.status] = counts.get(r.status, 0) + 1
        return Comparison(
            linked=link is not None,
            netbox=ref,
            suggestions=suggestions,
            snapshot_taken_at=snap.taken_at if snap else None,
            rows=rows,
            counts=counts,
        )


def _lease_interface(facts: HostFacts, ip: str) -> str | None:
    """The interface of the DHCP pool a lease's address belongs to."""
    address = ipaddress.ip_address(ip)
    for p in facts.dhcp.pools if facts.dhcp else []:
        if p.router and p.netmask:
            try:
                if address in ipaddress.ip_network(f"{p.router}/{p.netmask}", strict=False):
                    return p.interface
            except ValueError:
                continue
    return None
