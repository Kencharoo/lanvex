from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis
from sqlalchemy import func, select, text
from starlette.exceptions import HTTPException as StarletteHTTPException

from lanvex import __version__, core_proxy
from lanvex.audit.router import router as audit_router
from lanvex.auth.models import Role, User
from lanvex.auth.router import router as auth_router
from lanvex.auth.service import create_user
from lanvex.changes.router import router as changes_router
from lanvex.changes.runner import ChangeRunner
from lanvex.core.config import Settings, get_settings
from lanvex.core.db import Database
from lanvex.core.errors import install_error_handlers
from lanvex.core.http import RequestContextMiddleware
from lanvex.core.logging import configure_logging, get_logger
from lanvex.core.ratelimit import MemoryRateLimiter, RedisRateLimiter
from lanvex.dashboard.router import router as dashboard_router
from lanvex.devices.router import router as devices_router
from lanvex.discovery.router import router as discovery_router
from lanvex.ipam.router import router as ipam_router
from lanvex.lookups.router import router as lookups_router
from lanvex.netbox.client import NetBoxClient
from lanvex.servers.router import router as servers_router
from lanvex.servers.scheduler import ReadScheduler
from lanvex.system.router import router as system_router
from lanvex.topology.router import router as topology_router
from lanvex.vlans.router import router as vlans_router

log = get_logger("lanvex")


def build_netbox(settings: Settings) -> NetBoxClient | None:
    if not settings.netbox_configured:
        return None
    assert settings.netbox_url and settings.netbox_token
    return NetBoxClient(
        str(settings.netbox_url),
        settings.netbox_token.get_secret_value(),
        timeout=settings.netbox_timeout_seconds,
        verify_tls=settings.netbox_verify_tls,
        max_retries=settings.netbox_max_retries,
        name="Lanvex Core" if settings.core_enabled else "NetBox",
    )


# Any fixed number; it only has to be the same in every worker.
BOOTSTRAP_LOCK = 0x4C414E5645580001


async def bootstrap_admin(db: Database, settings: Settings) -> None:
    """Create the first administrator from env vars, only when the user table is empty.

    Every uvicorn worker runs this at startup, at the same moment. A transaction-scoped advisory lock
    makes the others wait until the first has committed, so they find the administrator and stop.
    """
    if not (settings.bootstrap_admin_username and settings.bootstrap_admin_password):
        return
    async with db.sessionmaker() as session:
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": BOOTSTRAP_LOCK})
        if await session.scalar(select(func.count()).select_from(User)):
            return
        await create_user(
            session,
            settings.bootstrap_admin_username,
            settings.bootstrap_admin_password.get_secret_value(),
            Role.ADMIN,
            display_name="Administrator",
        )
        await session.commit()
        log.info("bootstrap_admin_created", username=settings.bootstrap_admin_username)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.db = Database(settings.database_url.get_secret_value())
        app.state.redis = Redis.from_url(settings.redis_url) if settings.redis_url else None
        app.state.rate_limiter = (
            RedisRateLimiter(app.state.redis) if app.state.redis is not None else MemoryRateLimiter()
        )
        if getattr(app.state, "netbox", None) is None:
            app.state.netbox = build_netbox(settings)
        await bootstrap_admin(app.state.db, settings)
        app.state.change_runner = None
        if settings.change_runner_enabled:
            app.state.change_runner = ChangeRunner(app.state.db, settings, lambda: app.state.netbox)
            app.state.change_runner.start()
        app.state.read_scheduler = None
        if settings.read_scheduler_enabled:
            app.state.read_scheduler = ReadScheduler(app.state.db, settings, lambda: app.state.netbox)
            app.state.read_scheduler.start()
        log.info(
            "lanvex_started",
            version=__version__,
            env=settings.env,
            netbox_configured=app.state.netbox is not None,
            automation_enabled=settings.automation_enabled,
            core_enabled=settings.core_enabled,
        )
        yield
        if app.state.change_runner is not None:
            await app.state.change_runner.stop()
        if app.state.read_scheduler is not None:
            await app.state.read_scheduler.stop()
        await core_proxy.aclose(app)
        if app.state.netbox is not None:
            await app.state.netbox.aclose()
        if app.state.redis is not None:
            await app.state.redis.aclose()
        await app.state.db.dispose()

    app = FastAPI(
        title="Lanvex API",
        version=__version__,
        description="Lanvex — Network Infrastructure, Simplified.",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.netbox = None
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)

    api = APIRouter(prefix="/api/v1")
    for r in (
        auth_router,
        system_router,
        ipam_router,
        vlans_router,
        devices_router,
        lookups_router,
        dashboard_router,
        audit_router,
        servers_router,
        changes_router,
        topology_router,
        discovery_router,
    ):
        api.include_router(r)
    app.include_router(api)

    core_proxy.install(app)
    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built SPA when present (single-container deployments)."""
    static_dir = Path(__file__).resolve().parent / "static"
    index = static_dir / "index.html"
    if not index.exists():
        return
    app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        if path == "api" or path.startswith("api/"):
            raise StarletteHTTPException(status_code=404, detail="Not found.")
        candidate = (static_dir / path).resolve()
        if path and candidate.is_file() and static_dir in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


app = create_app()
