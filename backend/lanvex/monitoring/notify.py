"""Sending alerts, and the monitor that probes servers and sends what is queued.

The monitor runs in one Lanvex process at a time: it holds a PostgreSQL advisory lock on a connection
of its own, and the other processes' monitors wait for it.
"""

import asyncio
import json
import smtplib
import ssl
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from lanvex import __version__
from lanvex.core.config import Settings
from lanvex.core.db import Database
from lanvex.core.logging import get_logger
from lanvex.integrations.linux import transport
from lanvex.monitoring.models import Alert, AlertNotification, ServerProbe, Severity, Webhook
from lanvex.monitoring.service import MonitoringService
from lanvex.netbox.client import NetBoxClient
from lanvex.servers.models import Server

log = get_logger("lanvex.monitoring.notify")

MONITOR_LOCK = 0x4C414E5645580002
RETRY_MINUTES = [1, 2, 5, 15, 60]  # after each failed attempt; then the notification is given up
MAX_ATTEMPTS = len(RETRY_MINUTES) + 1
LISTED = 20  # alerts listed in one message; the rest are counted
PRIORITY = {"critical": ("5", 8), "warning": ("4", 5), "info": ("3", 2)}  # ntfy, gotify
EVENT_LABEL = {"opened": "", "changed": "Changed: ", "resolved": "Resolved: "}


class DeliveryError(Exception):
    pass


# ---- messages -----------------------------------------------------------------------------------------


def alert_link(settings: Settings) -> str | None:
    return f"{settings.public_url}/alerts" if settings.public_url else None


def subject(items: list[tuple[str, Alert]]) -> str:
    if len(items) == 1:
        event, a = items[0]
        return f"{EVENT_LABEL[event]}{a.title}"
    return f"{len(items)} alerts"


def text_body(items: list[tuple[str, Alert]], settings: Settings) -> str:
    lines: list[str] = []
    for event, a in items[:LISTED]:
        lines.append(f"[{a.severity.upper()}] {EVENT_LABEL[event]}{a.title}")
        lines += [f"  {d}" for d in a.detail[:10]]
        if len(a.detail) > 10:
            lines.append(f"  … {len(a.detail) - 10} more")
        lines.append("")
    if len(items) > LISTED:
        lines.append(f"… and {len(items) - LISTED} more.")
    if link := alert_link(settings):
        lines.append(link)
    return "\n".join(lines).strip() + "\n"


def payload(fmt: str, items: list[tuple[str, Alert]], settings: Settings) -> tuple[dict[str, str], bytes]:
    """Headers and body for a webhook of format `fmt`."""
    title = subject(items)
    body = text_body(items, settings)
    worst = max((a.severity for _, a in items), key=lambda s: ["info", "warning", "critical"].index(s))
    resolved = all(e == "resolved" for e, _ in items)
    js = {"Content-Type": "application/json"}
    if fmt == "ntfy":
        tag = (
            "white_check_mark"
            if resolved
            else {"critical": "rotating_light", "warning": "warning"}.get(worst, "information_source")
        )
        headers = {"Title": title, "Priority": PRIORITY[worst][0], "Tags": tag, "Content-Type": "text/plain"}
        if link := alert_link(settings):
            headers["Click"] = link
        return headers, body.encode()
    if fmt == "gotify":
        return js, json.dumps({"title": title, "message": body, "priority": PRIORITY[worst][1]}).encode()
    if fmt == "slack":
        return js, json.dumps({"text": f"*{title}*\n{body}"}).encode()
    if fmt == "discord":
        return js, json.dumps({"content": f"**{title}**\n{body}"[:2000]}).encode()
    if fmt == "teams":
        card = {
            "type": "AdaptiveCard",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.4",
            "body": [
                {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True},
                {"type": "TextBlock", "text": body.replace("\n", "\n\n"), "wrap": True},
            ],
        }
        message = {
            "type": "message",
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}],
        }
        return js, json.dumps(message).encode()
    events = [
        {
            "event": e,
            "alert": {
                "id": str(a.id),
                "kind": a.kind,
                "severity": a.severity,
                "state": a.state,
                "server": a.server_name,
                "title": a.title,
                "detail": a.detail,
                "opened_at": a.opened_at.isoformat(),
                "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
            },
        }
        for e, a in items
    ]
    return js, json.dumps(
        {"source": "lanvex", "version": __version__, "url": alert_link(settings), "events": events}
    ).encode()


# ---- channels ---------------------------------------------------------------------------------------


def _send_email(settings: Settings, to: list[str], subj: str, body: str) -> None:
    assert settings.smtp_host and settings.smtp_from
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = ", ".join(to)
    msg["Subject"] = "[Lanvex] " + " ".join(subj.split())
    msg.set_content(body)
    context = ssl.create_default_context()
    timeout = settings.smtp_timeout_seconds
    smtp: smtplib.SMTP
    if settings.smtp_security == "tls":
        smtp = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=timeout, context=context)
    else:
        smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout)
    try:
        if settings.smtp_security == "starttls":
            smtp.starttls(context=context)
        if settings.smtp_username and settings.smtp_password:
            smtp.login(settings.smtp_username, settings.smtp_password.get_secret_value())
        smtp.send_message(msg)
    finally:
        try:
            smtp.quit()
        except smtplib.SMTPException:
            pass


async def send_email(settings: Settings, to: list[str], items: list[tuple[str, Alert]]) -> None:
    try:
        await asyncio.to_thread(_send_email, settings, to, subject(items), text_body(items, settings))
    except (OSError, smtplib.SMTPException) as e:
        raise DeliveryError(f"Email: {type(e).__name__}: {str(e)[:200]}") from e


async def send_webhook(
    fmt: str, url: str, hint: str, items: list[tuple[str, Alert]], settings: Settings
) -> None:
    headers, body = payload(fmt, items, settings)
    headers["User-Agent"] = f"Lanvex/{__version__}"
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            r = await client.post(url, content=body, headers=headers)
    except httpx.HTTPError as e:  # the message may hold the URL and its token: name the host only
        raise DeliveryError(f"{hint}: {type(e).__name__}") from e
    if r.status_code >= 300:
        raise DeliveryError(f"{hint}: HTTP {r.status_code}")


# ---- the monitor ------------------------------------------------------------------------------------


class Monitor:
    TICK_SECONDS = 15
    CONCURRENCY = 10

    def __init__(self, db: Database, settings: Settings, netbox: Callable[[], NetBoxClient | None]) -> None:
        self.db = db
        self.settings = settings
        self.netbox = netbox
        self._task: asyncio.Task[None] | None = None
        self._lock: AsyncConnection | None = None
        self._next_probe = datetime.min.replace(tzinfo=UTC)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="lanvex-monitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._lock is not None:
            await self._lock.close()

    async def _hold_lock(self) -> bool:
        if self._lock is not None:
            return True
        conn = await self.db.engine.connect()
        if await conn.scalar(select(func.pg_try_advisory_lock(MONITOR_LOCK))):
            self._lock = conn
            log.info("monitor_started")
            return True
        await conn.close()
        return False

    async def _loop(self) -> None:
        while True:
            try:
                if await self._hold_lock():
                    await self.tick()
            except Exception:  # never let one bad round end monitoring
                log.exception("monitor_round_failed")
                if self._lock is not None:  # the connection may be gone, and the lock with it
                    try:
                        await self._lock.close()
                    finally:
                        self._lock = None
            await asyncio.sleep(self.TICK_SECONDS)

    async def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        if now >= self._next_probe:
            self._next_probe = now + timedelta(seconds=self.settings.monitor_probe_seconds)
            await self.probe_all()
        await self.deliver(now)

    # ---- probes -------------------------------------------------------------------------------------

    async def probe(self, address: str, port: int) -> str | None:
        """None if the SSH port answers; otherwise why not. A TCP connection only: no sign-in, no SSH."""
        try:
            addresses = await transport.resolve(address)
            transport.check_allowed(addresses, self.settings.server_allowed_networks)
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(addresses[0], port), timeout=self.settings.ssh_connect_timeout_seconds
            )
        except TimeoutError:
            return "No answer (timed out)."
        except transport.SSHError as e:
            return e.message[:300]
        except OSError as e:
            return (e.strerror or type(e).__name__)[:300]
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return None

    async def probe_all(self) -> None:
        async with self.db.sessionmaker() as session:
            servers = list((await session.scalars(select(Server))).all())
        limit = asyncio.Semaphore(self.CONCURRENCY)

        async def one(s: Server) -> tuple[uuid.UUID, str | None]:
            async with limit:
                return s.id, await self.probe(s.address, s.port)

        results = await asyncio.gather(*(one(s) for s in servers))
        now = datetime.now(UTC)
        async with self.db.sessionmaker() as session:
            svc = MonitoringService(session, self.settings, self.netbox())
            for server_id, error in results:
                s = await session.get(Server, server_id)
                if s is None:
                    continue
                p = await session.get(ServerProbe, server_id) or ServerProbe(server_id=server_id, failures=0)
                session.add(p)
                p.last_probe_at = now
                if error is None:
                    p.failures, p.last_ok_at, p.last_error = 0, now, None
                else:
                    p.failures, p.last_error = p.failures + 1, error
                await session.flush()
                await svc.after_probe(s, p)
            await session.commit()

    # ---- delivery -----------------------------------------------------------------------------------

    async def deliver(self, now: datetime | None = None) -> int:
        """Send what is due, one message per channel. Returns the number of notifications sent."""
        now = now or datetime.now(UTC)
        sent = 0
        async with self.db.sessionmaker() as session:
            due = list(
                (
                    await session.scalars(
                        select(AlertNotification)
                        .where(
                            AlertNotification.sent_at.is_(None),
                            AlertNotification.attempts < MAX_ATTEMPTS,
                            AlertNotification.next_attempt_at <= now,
                        )
                        .order_by(AlertNotification.created_at)
                        .limit(500)
                    )
                ).all()
            )
            if not due:
                return 0
            svc = MonitoringService(session, self.settings, self.netbox())
            cfg = await svc.config()
            by_channel: dict[str, list[AlertNotification]] = {}
            for n in due:
                by_channel.setdefault(n.channel, []).append(n)
            for channel, rows in by_channel.items():
                items = [(n.event, await session.get(Alert, n.alert_id)) for n in rows]
                listed = [(e, a) for e, a in items if a is not None]
                try:
                    if channel == "email":
                        if not (self.settings.smtp_configured and cfg.email_enabled and cfg.email_to):
                            raise DeliveryError("Email is not set up any more.")
                        await send_email(self.settings, cfg.email_to, listed)
                    else:
                        w = await session.get(Webhook, uuid.UUID(channel.split(":", 1)[1]))
                        if w is None or not w.enabled:
                            for n in rows:  # removed or switched off: nothing to send to
                                n.attempts, n.error = MAX_ATTEMPTS, "The webhook was removed or switched off."
                            continue
                        await send_webhook(w.format, svc.webhook_url(w), w.url_hint, listed, self.settings)
                except DeliveryError as e:
                    for n in rows:
                        n.attempts += 1
                        n.error = str(e)[:500]
                        n.next_attempt_at = now + timedelta(
                            minutes=RETRY_MINUTES[min(n.attempts, MAX_ATTEMPTS) - 1]
                        )
                    log.warning("alert_delivery_failed", channel=channel.split(":")[0], error=str(e))
                    continue
                for n in rows:
                    n.attempts += 1
                    n.sent_at, n.error = now, None
                sent += len(rows)
            await session.commit()
        return sent


async def send_test(settings: Settings, svc: MonitoringService, channel: str) -> None:
    """A test message to one channel, now. Raises DeliveryError."""
    now = datetime.now(UTC)
    sample = Alert(
        id=uuid.uuid4(),
        kind="test",
        server_name="",
        key="",
        severity=Severity.INFO,
        title="Test message from Lanvex",
        detail=["Alerts will arrive here."],
        state="open",
        opened_at=now,
        last_seen_at=now,
    )
    items = [("opened", sample)]
    if channel == "email":
        cfg = await svc.config()
        if not settings.smtp_configured:
            raise DeliveryError("SMTP is not configured (LANVEX_SMTP_HOST, LANVEX_SMTP_FROM).")
        if not cfg.email_to:
            raise DeliveryError("No recipients.")
        await send_email(settings, cfg.email_to, items)
        return
    w = await svc.webhook(uuid.UUID(channel.split(":", 1)[1]))
    await send_webhook(w.format, svc.webhook_url(w), w.url_hint, items, settings)
