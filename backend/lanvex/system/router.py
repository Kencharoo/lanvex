from typing import Literal

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from lanvex import __version__
from lanvex.core.config import get_settings
from lanvex.deps import DB, CurrentUser
from lanvex.netbox.client import MIN_SUPPORTED_VERSION, NetBoxClient
from lanvex.schemas import ApiModel

router = APIRouter(tags=["system"])


class Liveness(ApiModel):
    status: Literal["ok"] = "ok"
    version: str = __version__


class Readiness(ApiModel):
    status: Literal["ok", "degraded", "unavailable"]
    database: bool
    redis: bool | None
    netbox_configured: bool


class NetBoxStatusOut(ApiModel):
    configured: bool
    url: str | None
    reachable: bool
    authenticated: bool
    version: str | None
    supported: bool | None
    minimum_version: str
    latency_ms: int | None
    message: str | None


class SystemInfo(ApiModel):
    version: str
    environment: str
    automation_enabled: bool
    core_enabled: bool
    netbox_label: str
    credential_storage: bool
    server_allowed_networks: list[str]


@router.get("/health/live", response_model=Liveness)
async def live() -> Liveness:
    """Process is up. Used by container health checks."""
    return Liveness()


@router.get("/health/ready", response_model=Readiness)
async def ready(request: Request, response: Response, db: DB) -> Readiness:
    """Dependencies needed to serve requests. NetBox is reported separately; it is not a readiness gate."""
    try:
        await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    redis = request.app.state.redis
    redis_ok: bool | None = None
    if redis is not None:
        try:
            redis_ok = bool(await redis.ping())
        except Exception:
            redis_ok = False
    status: Literal["ok", "degraded", "unavailable"] = "ok"
    if not db_ok:
        status = "unavailable"
        response.status_code = 503
    elif redis_ok is False:
        status = "degraded"
    return Readiness(
        status=status, database=db_ok, redis=redis_ok, netbox_configured=get_settings().netbox_configured
    )


@router.get("/system/netbox", response_model=NetBoxStatusOut)
async def netbox_status(request: Request, _: CurrentUser) -> NetBoxStatusOut:
    client: NetBoxClient | None = request.app.state.netbox
    minimum = ".".join(map(str, MIN_SUPPORTED_VERSION))
    if client is None:
        return NetBoxStatusOut(
            configured=False,
            url=None,
            reachable=False,
            authenticated=False,
            version=None,
            supported=None,
            minimum_version=minimum,
            latency_ms=None,
            message="NetBox is not configured. Set LANVEX_NETBOX_URL and LANVEX_NETBOX_TOKEN.",
        )
    s = await client.status()
    return NetBoxStatusOut(
        configured=True,
        url=client.base_url,
        reachable=s.reachable,
        authenticated=s.authenticated,
        version=s.version,
        supported=s.supported,
        minimum_version=minimum,
        latency_ms=s.latency_ms,
        message=s.message,
    )


@router.get("/system/info", response_model=SystemInfo)
async def info(_: CurrentUser) -> SystemInfo:
    s = get_settings()
    return SystemInfo(
        version=__version__,
        environment=s.env,
        automation_enabled=s.automation_enabled,
        core_enabled=s.core_enabled,
        netbox_label="Lanvex Core" if s.core_enabled else "NetBox",
        credential_storage=s.secret_key is not None,
        server_allowed_networks=s.server_allowed_networks,
    )
