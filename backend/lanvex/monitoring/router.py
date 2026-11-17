import uuid
from typing import Literal

from fastapi import APIRouter, Query, Request, status

from lanvex.deps import DB, AdminUser, Audit, CurrentUser, SettingsDep
from lanvex.monitoring.notify import DeliveryError, send_test
from lanvex.monitoring.schemas import (
    AlertCount,
    AlertList,
    AlertOut,
    DeliveryResult,
    MonitoringOut,
    MonitoringSettings,
    WebhookIn,
    WebhookOut,
)
from lanvex.monitoring.service import MonitoringService
from lanvex.schemas import ApiModel

router = APIRouter(tags=["monitoring"])


def _svc(request: Request, db: DB, settings: SettingsDep) -> MonitoringService:
    return MonitoringService(db, settings, request.app.state.netbox)


@router.get("/alerts", response_model=AlertList)
async def list_alerts(
    request: Request,
    db: DB,
    settings: SettingsDep,
    user: CurrentUser,
    state: Literal["open", "resolved", "all"] = Query(default="open"),
) -> AlertList:
    svc = _svc(request, db, settings)
    count = await svc.count()
    alerts = await svc.alerts(None if state == "all" else state)
    return AlertList(
        alerts=[AlertOut.model_validate(a) for a in alerts],
        open=count.open,
        unacknowledged=count.unacknowledged,
    )


@router.get("/alerts/count", response_model=AlertCount)
async def count_alerts(request: Request, db: DB, settings: SettingsDep, user: CurrentUser) -> AlertCount:
    return await _svc(request, db, settings).count()


@router.post("/alerts/{alert_id}/acknowledge", response_model=AlertOut)
async def acknowledge(
    alert_id: uuid.UUID, request: Request, db: DB, settings: SettingsDep, user: AdminUser, audit: Audit
) -> AlertOut:
    svc = _svc(request, db, settings)
    a = await svc.get(alert_id)
    await svc.acknowledge(a, user)
    await db.commit()
    await audit.record(
        "alert.acknowledge", "success", target_type="alert", target_id=a.id, target_repr=a.title
    )
    return AlertOut.model_validate(a)


async def _monitoring_out(svc: MonitoringService) -> MonitoringOut:
    row = await svc.config_row()
    s = svc.settings
    return MonitoringOut(
        settings=await svc.config(),
        smtp_configured=s.smtp_configured,
        smtp_from=s.smtp_from,
        probe_seconds=s.monitor_probe_seconds,
        enabled=s.monitor_enabled,
        updated_at=row.updated_at if row else None,
        updated_by=row.updated_by if row else None,
    )


@router.get("/monitoring", response_model=MonitoringOut)
async def get_monitoring(request: Request, db: DB, settings: SettingsDep, user: AdminUser) -> MonitoringOut:
    return await _monitoring_out(_svc(request, db, settings))


@router.put("/monitoring", response_model=MonitoringOut)
async def put_monitoring(
    body: MonitoringSettings, request: Request, db: DB, settings: SettingsDep, user: AdminUser, audit: Audit
) -> MonitoringOut:
    svc = _svc(request, db, settings)
    async with audit.track("monitoring.update", "monitoring", target_repr="Monitoring") as t:
        await svc.save_config(body, user)
        await db.commit()
        t.detail = {"email_to": len(body.email_to), "email_enabled": body.email_enabled}
    return await _monitoring_out(svc)


@router.get("/monitoring/webhooks", response_model=list[WebhookOut])
async def list_webhooks(request: Request, db: DB, settings: SettingsDep, user: AdminUser) -> list[WebhookOut]:
    return [WebhookOut.model_validate(w) for w in await _svc(request, db, settings).webhooks()]


@router.post("/monitoring/webhooks", response_model=WebhookOut, status_code=status.HTTP_201_CREATED)
async def add_webhook(
    body: WebhookIn, request: Request, db: DB, settings: SettingsDep, user: AdminUser, audit: Audit
) -> WebhookOut:
    svc = _svc(request, db, settings)
    async with audit.track("monitoring.webhook.add", "webhook", target_repr=body.name) as t:
        w = await svc.save_webhook(body)
        await db.commit()
        t.target_id, t.detail = w.id, {"format": w.format, "host": w.url_hint}
    return WebhookOut.model_validate(w)


@router.put("/monitoring/webhooks/{webhook_id}", response_model=WebhookOut)
async def edit_webhook(
    webhook_id: uuid.UUID,
    body: WebhookIn,
    request: Request,
    db: DB,
    settings: SettingsDep,
    user: AdminUser,
    audit: Audit,
) -> WebhookOut:
    svc = _svc(request, db, settings)
    async with audit.track(
        "monitoring.webhook.edit", "webhook", target_id=webhook_id, target_repr=body.name
    ) as t:
        w = await svc.save_webhook(body, await svc.webhook(webhook_id))
        await db.commit()
        t.detail = {"format": w.format, "host": w.url_hint, "url_changed": body.url is not None}
    return WebhookOut.model_validate(w)


@router.delete("/monitoring/webhooks/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    webhook_id: uuid.UUID, request: Request, db: DB, settings: SettingsDep, user: AdminUser, audit: Audit
) -> None:
    svc = _svc(request, db, settings)
    w = await svc.webhook(webhook_id)
    async with audit.track("monitoring.webhook.delete", "webhook", target_id=w.id, target_repr=w.name):
        await db.delete(w)
        await db.commit()


class TestIn(ApiModel):
    channel: str  # email | webhook:<id>


@router.post("/monitoring/test", response_model=DeliveryResult)
async def test_channel(
    body: TestIn, request: Request, db: DB, settings: SettingsDep, user: AdminUser, audit: Audit
) -> DeliveryResult:
    svc = _svc(request, db, settings)
    try:
        await send_test(settings, svc, body.channel)
    except DeliveryError as e:
        result = DeliveryResult(channel=body.channel, ok=False, error=str(e))
    except ValueError:
        result = DeliveryResult(channel=body.channel, ok=False, error="Unknown channel.")
    else:
        result = DeliveryResult(channel=body.channel, ok=True)
    await audit.record(
        "monitoring.test",
        "success" if result.ok else "failure",
        target_type="monitoring",
        target_repr=body.channel.split(":")[0],
        detail={"error": result.error} if result.error else None,
    )
    return result
