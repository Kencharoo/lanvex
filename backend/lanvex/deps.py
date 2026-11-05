"""Shared FastAPI dependencies: database, current user, roles, CSRF, rate limits, NetBox client."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from lanvex.audit.service import Auditor
from lanvex.auth.models import Role, Session, User
from lanvex.auth.service import constant_time_equals, resolve_session
from lanvex.core.config import Settings, get_settings
from lanvex.core.errors import NotAuthenticated, NotConfigured, PermissionDenied
from lanvex.core.ratelimit import RateLimiter
from lanvex.netbox.client import NetBoxClient

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.db.sessionmaker() as session:
        yield session


DB = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_rate_limiter(request: Request) -> RateLimiter:
    return request.app.state.rate_limiter


async def get_session(request: Request, db: DB, settings: SettingsDep) -> Session:
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise NotAuthenticated("Sign in to continue.")
    session = await resolve_session(db, token, settings)
    if session is None:
        raise NotAuthenticated("Your session has expired. Sign in again.")
    if request.method not in SAFE_METHODS:
        header = request.headers.get("x-csrf-token", "")
        if not header or not constant_time_equals(header, session.csrf_token):
            raise PermissionDenied("Missing or invalid CSRF token.", code="csrf_failed")
    request.state.user = session.user
    return session


CurrentSession = Annotated[Session, Depends(get_session)]


async def get_current_user(session: CurrentSession) -> User:
    return session.user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def require_admin(request: Request, user: CurrentUser, settings: SettingsDep) -> User:
    if user.role != Role.ADMIN:
        raise PermissionDenied("This action requires the administrator role.")
    if request.method not in SAFE_METHODS:
        limiter: RateLimiter = request.app.state.rate_limiter
        await limiter.hit(f"write:{user.id}", settings.write_rate_limit, settings.write_rate_window_seconds)
    return user


AdminUser = Annotated[User, Depends(require_admin)]


def get_netbox(request: Request) -> NetBoxClient:
    client: NetBoxClient | None = request.app.state.netbox
    if client is None:
        raise NotConfigured(
            "NetBox is not configured. Set LANVEX_NETBOX_URL and LANVEX_NETBOX_TOKEN.",
            code="netbox_not_configured",
        )
    return client


NetBox = Annotated[NetBoxClient, Depends(get_netbox)]


def get_auditor(request: Request, db: DB) -> Auditor:
    return Auditor(db, request, getattr(request.state, "user", None))


Audit = Annotated[Auditor, Depends(get_auditor)]
