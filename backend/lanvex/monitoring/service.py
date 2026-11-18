"""Alerts: found by the checks, kept one per condition, queued for delivery to email and webhooks."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lanvex.auth.models import User
from lanvex.core.config import Settings
from lanvex.core.errors import LanvexError, NotFound, ValidationFailed
from lanvex.core.logging import get_logger
from lanvex.core.secrets import Vault
from lanvex.core.vendors import vendor
from lanvex.integrations.snmp.switch import SwitchFacts
from lanvex.monitoring import checks
from lanvex.monitoring.checks import EVENT_KINDS, Finding
from lanvex.monitoring.models import (
    Alert,
    AlertNotification,
    AlertState,
    MonitoringConfig,
    ServerProbe,
    Severity,
    Webhook,
)
from lanvex.monitoring.schemas import AlertCount, MonitoringSettings, WebhookIn
from lanvex.netbox.client import NetBoxClient
from lanvex.servers.models import ConnectionStatus, Server, ServerSnapshot
from lanvex.switches.models import Switch
from lanvex.switches.ports import Move, PortLocator, PortMap, port_map

log = get_logger("lanvex.monitoring")

RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}
READ_KINDS = {"drift", "pool_full", "unknown_device"}  # need a successful read


def at_least(severity: str, minimum: str) -> bool:
    return RANK[Severity(severity)] >= RANK[Severity(minimum)]


def url_hint(url: str) -> str:
    """What may be shown of a webhook URL: scheme and host, never the path or query with its token."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname or ''}" + (f":{parts.port}" if parts.port else "") + "/…"


class MonitoringService:
    def __init__(self, db: AsyncSession, settings: Settings, nb: NetBoxClient | None = None):
        self.db = db
        self.settings = settings
        self.nb = nb

    # ---- settings ------------------------------------------------------------------------------------

    async def config_row(self) -> MonitoringConfig | None:
        return await self.db.get(MonitoringConfig, 1)

    async def config(self) -> MonitoringSettings:
        row = await self.config_row()
        return MonitoringSettings.model_validate(row.data if row else {})

    async def save_config(self, body: MonitoringSettings, user: User) -> MonitoringConfig:
        row = await self.config_row()
        if row is None:
            row = MonitoringConfig(id=1)
            self.db.add(row)
        row.data = body.model_dump(mode="json")
        row.updated_at = datetime.now(UTC)
        row.updated_by = user.username
        await self.db.flush()
        return row

    # ---- alerts --------------------------------------------------------------------------------------

    async def alerts(self, state: str | None, limit: int = 500) -> list[Alert]:
        q = select(Alert).order_by(Alert.state.asc(), Alert.last_seen_at.desc()).limit(limit)
        if state:
            q = q.where(Alert.state == state)
        return list((await self.db.scalars(q)).all())

    async def count(self) -> AlertCount:
        rows = (
            await self.db.execute(
                select(Alert.severity, Alert.acknowledged_at.is_(None), func.count())
                .where(Alert.state == AlertState.OPEN)
                .group_by(Alert.severity, Alert.acknowledged_at.is_(None))
            )
        ).all()
        return AlertCount(
            open=sum(n for _, _, n in rows),
            unacknowledged=sum(n for _, unacked, n in rows if unacked),
            critical=sum(n for sev, unacked, n in rows if sev == Severity.CRITICAL and unacked),
        )

    async def get(self, alert_id: uuid.UUID) -> Alert:
        a = await self.db.get(Alert, alert_id)
        if a is None:
            raise NotFound("Alert not found.")
        return a

    async def acknowledge(self, a: Alert, user: User) -> Alert:
        now = datetime.now(UTC)
        a.acknowledged_at, a.acknowledged_by = now, user.username
        if a.kind in EVENT_KINDS and a.state == AlertState.OPEN:
            a.state, a.resolved_at = AlertState.RESOLVED, now
        if a.kind == "port_down" and a.state == AlertState.OPEN and a.switch_id:
            # Unplugged on purpose: stop watching the port, so the alert does not come back.
            sw = await self.db.get(Switch, a.switch_id)
            if sw is not None:
                sw.watched_ports = [w for w in sw.watched_ports or [] if w.get("name") != a.key]
            a.state, a.resolved_at = AlertState.RESOLVED, now
        await self.db.flush()
        return a

    async def reconcile(self, s: Server | Switch, kinds: set[str], findings: list[Finding]) -> list[Alert]:
        """Bring the server's or switch's alerts of `kinds` in line with what the checks found. Returns
        alerts opened."""
        cfg = await self.config()
        now = datetime.now(UTC)
        owner = Alert.switch_id if isinstance(s, Switch) else Alert.server_id
        ids = {"switch_id": s.id} if isinstance(s, Switch) else {"server_id": s.id}
        current: dict[tuple[str, str], Alert] = {}
        for a in await self.db.scalars(
            select(Alert).where(owner == s.id, Alert.state == AlertState.OPEN, Alert.kind.in_(kinds))
        ):
            if getattr(cfg, a.kind, False):
                current[(a.kind, a.key)] = a
            else:  # the check was turned off: its alerts end without a message
                a.state, a.resolved_at = AlertState.RESOLVED, now
        kinds = {k for k in kinds if getattr(cfg, k, False)}
        opened: list[Alert] = []
        found = {(f.kind, f.key) for f in findings if f.kind in kinds}
        for f in findings:
            if f.kind not in kinds:
                continue
            a = current.get((f.kind, f.key))
            if a is not None:
                new_lines = [line for line in f.detail if line not in a.detail]
                a.title, a.detail, a.severity, a.last_seen_at = f.title, f.detail, f.severity, now
                if new_lines and f.kind == "drift":
                    a.acknowledged_at = a.acknowledged_by = None
                    await self._queue(a, "changed", cfg)
                continue
            if f.kind in EVENT_KINDS:
                seen = await self.db.scalar(
                    select(Alert.id).where(owner == s.id, Alert.kind == f.kind, Alert.key == f.key).limit(1)
                )
                if seen is not None:
                    continue
            a = Alert(
                kind=f.kind,
                **ids,
                server_name=s.name,
                key=f.key,
                severity=f.severity,
                title=f.title,
                detail=f.detail,
                state=AlertState.OPEN,
                opened_at=now,
                last_seen_at=now,
            )
            self.db.add(a)
            await self.db.flush()
            await self._queue(a, "opened", cfg)
            opened.append(a)
        for (kind, key), a in current.items():
            if (kind, key) in found or kind in ("change_failed", "device_moved"):
                continue
            if kind == "unknown_device" and not await self._documented(key):
                continue  # the lease ended, the device is still not documented
            a.state, a.resolved_at, a.last_seen_at = AlertState.RESOLVED, now, now
            if cfg.notify_resolved:
                await self._queue(a, "resolved", cfg)
        await self.db.flush()
        return opened

    async def _documented(self, mac: str) -> bool:
        return mac in await self.known_macs([mac])

    async def known_macs(self, macs: list[str]) -> set[str]:
        """The MAC addresses among `macs` that the documentation has: on an interface of a device or virtual
        machine, or as a fixed DHCP address."""
        wanted = sorted({m.lower() for m in macs})
        if self.nb is None or not wanted:
            return set()
        from lanvex.networks.service import MAC_FIELD, _macs  # here: networks imports servers, as does this

        known: set[str] = set()
        for i in range(0, len(wanted), 40):
            chunk = wanted[i : i + 40]
            for path in ("dcim/interfaces/", "virtualization/interfaces/"):
                try:
                    for obj in await self.nb.list_all(path, {"mac_address": chunk}, cap=1000):
                        known |= _macs(obj)
                except LanvexError:
                    continue
            try:
                for ip in await self.nb.list_all("ipam/ip-addresses/", {f"cf_{MAC_FIELD}": chunk}, cap=1000):
                    mac = (ip.get("custom_fields") or {}).get(MAC_FIELD)
                    if mac:
                        known.add(str(mac).lower())
            except LanvexError:
                continue
        return known & set(wanted)

    # ---- after a read, a probe, a change -----------------------------------------------------------

    async def after_read(self, s: Server, snap: ServerSnapshot) -> list[Alert]:
        """Run the checks that rest on a read. A failed read only says it failed; what the last successful
        read found stays as it was."""
        cfg = await self.config()
        findings = checks.read_failed(s.name, snap.status, snap.message)
        kinds = {"read_failed"}
        if snap.status == ConnectionStatus.OK and snap.facts:
            from lanvex.integrations.linux.facts import HostFacts
            from lanvex.networks.service import NetworksService
            from lanvex.servers.service import ServerService

            facts = HostFacts.model_validate(snap.facts)
            now = datetime.now(UTC)
            servers = ServerService(self.db, self.settings, self.nb)
            kinds |= READ_KINDS
            if cfg.drift and self.nb is not None:
                try:
                    comparison = await servers.compare(s) if s.netbox_object_id else None
                    view = await NetworksService(servers, self.nb).view(s)
                    findings += checks.drift(s.name, comparison, view, cfg)
                except LanvexError as e:  # the documentation cannot be read now: keep what is open
                    log.warning("monitor_drift_skipped", server=s.name, error=e.message)
                    kinds.discard("drift")
            if cfg.pool_full:
                findings += checks.pool_usage(s.name, facts, cfg.pool_threshold, now)
            if cfg.unknown_device and self.nb is not None and facts.dhcp:
                try:
                    leased = [lease.mac.lower() for lease in facts.dhcp.leases]
                    known = await self.known_macs(leased)
                    # A device already reported by a switch or another server is not reported again.
                    known |= set(
                        await self.db.scalars(
                            select(Alert.key).where(
                                Alert.kind == "unknown_device",
                                Alert.key.in_(leased),
                                or_(Alert.server_id.is_(None), Alert.server_id != s.id),
                            )
                        )
                    )
                    findings += checks.unknown_devices(s.name, facts, known, cfg, now)
                except LanvexError as e:
                    log.warning("monitor_devices_skipped", server=s.name, error=e.message)
                    kinds.discard("unknown_device")
            elif self.nb is None:
                kinds.discard("unknown_device")
        return await self.reconcile(s, kinds, findings)

    async def after_probe(self, s: Server, probe: ServerProbe) -> list[Alert]:
        cfg = await self.config()
        findings = checks.unreachable(s.name, probe.failures, cfg.unreachable_after, probe.last_error)
        return await self.reconcile(s, {"unreachable"}, findings)

    async def after_switch_read(
        self, sw: Switch, facts: SwitchFacts | None = None, moves: Sequence[Move] = ()
    ) -> list[Alert]:
        """A failed read (other than no answer, which the probe reports); after a successful one, ports
        that went down, devices new on its ports, and documented devices on another port than before."""
        cfg = await self.config()
        findings = checks.switch_read_failed(sw.name, sw.last_status, sw.last_status_message)
        kinds = {"read_failed"}
        if sw.last_status == "ok" and facts is not None:
            pm = await port_map(self.db)
            peers = {
                n.port: (n.system_name or n.chassis_id or "an LLDP neighbour", False)
                for n in facts.neighbors
                if n.port is not None
            }
            for (sid, idx), other in pm.uplinks.items():
                if sid == sw.id:
                    peers[idx] = (pm.switches[other].name if other in pm.switches else "another switch", True)
            sw.watched_ports = checks.watch(sw.watched_ports or [], facts, peers)
            kinds.add("port_down")
            findings += checks.ports_down(sw.name, facts, sw.watched_ports)
            if self.nb is not None and (cfg.unknown_device or cfg.device_moved):
                try:
                    findings += await self._switch_devices(sw, pm, moves, cfg)
                    kinds |= {"unknown_device", "device_moved"}
                except LanvexError as e:  # the documentation cannot be read now: keep what is open
                    log.warning("monitor_switch_devices_skipped", switch=sw.name, error=e.message)
        return await self.reconcile(sw, kinds, findings)

    async def _switch_devices(
        self, sw: Switch, pm: PortMap, moves: Sequence[Move], cfg: MonitoringSettings
    ) -> list[Finding]:
        placed = {
            m.mac: m for (sid, _), rows in pm.by_port.items() if sid == sw.id for m in rows if pm.current(m)
        }
        macs = sorted(set(placed) | {mv.mac for mv in moves})
        if not macs:
            return []
        locator = PortLocator(self.db, self.settings, self.nb)
        servers, _ = await locator._servers()
        idents = await locator.identities(pm, macs, servers)
        out: list[Finding] = []
        if cfg.unknown_device and sw.first_read_at is not None:
            unknown = [m for m in placed if idents[m].kind == "unknown"]
            fixed = await self.known_macs(unknown)  # fixed DHCP addresses
            alerted = set(
                await self.db.scalars(
                    select(Alert.key).where(Alert.kind == "unknown_device", Alert.key.in_(unknown))
                )
            )
            new = [
                checks.SeenDevice(
                    mac, placed[mac].port, placed[mac].vlan or None, idents[mac].addresses, vendor(mac)
                )
                for mac in unknown
                if mac not in fixed and mac not in alerted and placed[mac].first_seen > sw.first_read_at
            ]
            out += checks.switch_unknown_devices(sw.name, new, cfg)
        if cfg.device_moved:
            names = {sid: s.name for sid, s in pm.switches.items()}
            moved = []
            for mv in moves:
                ident = idents.get(mv.mac)
                if ident is None or ident.kind == "unknown":
                    continue
                who = (
                    f"{ident.name} / {ident.interface}"
                    if ident.name and ident.interface
                    else ident.name or mv.mac
                )
                moved.append(
                    (
                        who,
                        f"{mv.mac}@{mv.to_switch}:{mv.to_port}"[:200],
                        f"{names.get(mv.to_switch, '?')} {mv.to_port}",
                        f"{names.get(mv.from_switch, '?')} {mv.from_port}",
                    )
                )
            out += checks.device_moves(moved)
        return out

    async def after_switch_probe(self, sw: Switch) -> list[Alert]:
        cfg = await self.config()
        findings = checks.switch_unreachable(
            sw.name, sw.probe_failures, cfg.unreachable_after, sw.last_probe_error
        )
        return await self.reconcile(sw, {"unreachable"}, findings)

    async def change_failed(self, s: Server, change_id: uuid.UUID, title: str, detail: list[str]) -> None:
        f = Finding("change_failed", str(change_id), Severity.WARNING, title, detail)
        cfg = await self.config()
        if not cfg.change_failed:
            return
        now = datetime.now(UTC)
        a = Alert(
            kind=f.kind,
            server_id=s.id,
            server_name=s.name,
            key=f.key,
            severity=f.severity,
            title=f.title,
            detail=f.detail,
            state=AlertState.OPEN,
            opened_at=now,
            last_seen_at=now,
        )
        self.db.add(a)
        await self.db.flush()
        await self._queue(a, "opened", cfg)

    # ---- delivery queue ------------------------------------------------------------------------------

    async def _queue(self, a: Alert, event: str, cfg: MonitoringSettings) -> None:
        channels: list[str] = []
        if (
            cfg.email_enabled
            and cfg.email_to
            and self.settings.smtp_configured
            and at_least(a.severity, cfg.email_min_severity)
        ):
            channels.append("email")
        for w in await self.db.scalars(select(Webhook).where(Webhook.enabled.is_(True))):
            if at_least(a.severity, w.min_severity):
                channels.append(f"webhook:{w.id}")
        for c in channels:
            self.db.add(AlertNotification(alert_id=a.id, event=event, channel=c))

    # ---- webhooks ------------------------------------------------------------------------------------

    async def webhooks(self) -> list[Webhook]:
        return list((await self.db.scalars(select(Webhook).order_by(Webhook.name))).all())

    async def webhook(self, webhook_id: uuid.UUID) -> Webhook:
        w = await self.db.get(Webhook, webhook_id)
        if w is None:
            raise NotFound("Webhook not found.")
        return w

    async def save_webhook(self, body: WebhookIn, w: Webhook | None = None) -> Webhook:
        if w is None:
            if not body.url:
                raise ValidationFailed("A URL is needed.", fields={"url": ["Required."]})
            w = Webhook()
            self.db.add(w)
        w.name, w.format, w.enabled, w.min_severity = (
            body.name.strip(),
            body.format,
            body.enabled,
            body.min_severity,
        )
        if body.url:
            w.url_ciphertext = Vault.from_settings(self.settings).encrypt({"url": body.url})
            w.url_hint = url_hint(body.url)
        await self.db.flush()
        return w

    def webhook_url(self, w: Webhook) -> str:
        data: dict[str, Any] = Vault.from_settings(self.settings).decrypt(w.url_ciphertext)
        return str(data["url"])


async def check_after_read(
    db: AsyncSession, settings: Settings, nb: NetBoxClient | None, s: Server, snap: ServerSnapshot
) -> None:
    """Monitoring after a read, in the read's transaction; a failing check never fails the read."""
    if not settings.monitor_enabled:
        return
    try:
        async with db.begin_nested():
            await MonitoringService(db, settings, nb).after_read(s, snap)
    except Exception:
        log.exception("monitor_checks_failed", server=s.name)


async def check_after_switch_read(
    db: AsyncSession,
    settings: Settings,
    nb: NetBoxClient | None,
    sw: Switch,
    facts: SwitchFacts | None = None,
    moves: Sequence[Move] = (),
) -> None:
    """Monitoring after a switch read, in the read's transaction; a failing check never fails the read."""
    if not settings.monitor_enabled:
        return
    try:
        async with db.begin_nested():
            await MonitoringService(db, settings, nb).after_switch_read(sw, facts, moves)
    except Exception:
        log.exception("monitor_checks_failed", switch=sw.name)
