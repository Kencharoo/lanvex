"""Server registration, credentials, connection tests, collection, and NetBox comparison.

Everything here reads from servers. Nothing changes a server's configuration.
"""

import asyncio
import ipaddress
import uuid
from datetime import UTC, datetime
from typing import Any

import asyncssh
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lanvex.auth.models import User
from lanvex.core.config import Settings
from lanvex.core.errors import Conflict, NotFound, ValidationFailed
from lanvex.core.secrets import Vault
from lanvex.integrations.base import capabilities
from lanvex.integrations.linux import adapter, transport
from lanvex.integrations.linux.commands import Cmd
from lanvex.integrations.linux.facts import HostFacts
from lanvex.netbox.client import NetBoxClient
from lanvex.servers.models import AuthMethod, ConnectionStatus, Server, ServerSnapshot
from lanvex.servers.schemas import (
    Comparison,
    ComparisonRow,
    Credentials,
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
        created_at=s.created_at,
    )


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
            url=(obj.get("display_url") or obj.get("url")),
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

    async def collect(self, s: Server, user: User | None) -> ServerSnapshot:
        snap = ServerSnapshot(server_id=s.id, requested_by=user.username if user else None)
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
        self.db.add(snap)
        await self.db.flush()
        await self._prune(s.id)
        return snap

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
                                    url=o.get("display_url"),
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
