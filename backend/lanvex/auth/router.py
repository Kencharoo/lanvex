import html
import uuid
from datetime import datetime
from typing import Annotated

import httpx
from fastapi import APIRouter, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import Field
from sqlalchemy import delete, select

from lanvex.auth import directory, sso
from lanvex.auth.models import AuthSource, Role, User
from lanvex.auth.models import Session as SessionModel
from lanvex.auth.service import (
    SignInRefused,
    authenticate,
    count_active_admins,
    create_session,
    create_user,
    find_user,
    hash_password,
    provision,
    retire_user,
    revoke_session,
    revoke_user_sessions,
    verify_password,
)
from lanvex.core.errors import (
    Conflict,
    NotAuthenticated,
    NotConfigured,
    NotFound,
    UpstreamUnavailable,
    ValidationFailed,
)
from lanvex.core.http import client_ip, is_loopback_host, request_is_https
from lanvex.core.logging import get_logger
from lanvex.deps import DB, AdminUser, Audit, CurrentSession, CurrentUser, SettingsDep, get_rate_limiter
from lanvex.schemas import ApiModel

router = APIRouter(tags=["auth"])
log = get_logger("lanvex.auth")


class LoginRequest(ApiModel):
    username: Annotated[str, Field(min_length=1, max_length=64)]
    password: Annotated[str, Field(min_length=1, max_length=256)]


class UserOut(ApiModel):
    id: uuid.UUID
    username: str
    display_name: str | None
    role: Role
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime
    auth_source: AuthSource = AuthSource.LOCAL


class SessionOut(ApiModel):
    user: UserOut
    csrf_token: str
    expires_at: datetime


class UserCreate(ApiModel):
    username: Annotated[str, Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")]
    display_name: Annotated[str | None, Field(max_length=128)] = None
    password: Annotated[str, Field(max_length=256)]
    role: Role = Role.READONLY


class UserUpdate(ApiModel):
    display_name: Annotated[str | None, Field(max_length=128)] = None
    role: Role | None = None
    is_active: bool | None = None
    password: Annotated[str | None, Field(max_length=256)] = None


class SignInMethods(ApiModel):
    """What the sign-in page offers: always a name and password (local, and the directory's when
    configured), and single sign-on when configured."""

    directory: bool
    sso: str | None  # the button's label


class SignInConfiguration(ApiModel):
    """How sign-in is set up (from .env), for administrators: no passwords or secrets."""

    directory: dict[str, object] | None
    sso: dict[str, object] | None


class DirectoryTest(ApiModel):
    ok: bool
    message: str


class PasswordChange(ApiModel):
    current_password: Annotated[str, Field(max_length=256)]
    new_password: Annotated[str, Field(max_length=256)]


def _set_cookie(response: Response, settings: SettingsDep, token: str) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_absolute_hours * 3600,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="strict",
        path="/",
    )


@router.post("/auth/login", response_model=SessionOut)
async def login(
    body: LoginRequest, request: Request, response: Response, db: DB, settings: SettingsDep, audit: Audit
):
    if settings.secure_cookies and not request_is_https(request) and not is_loopback_host(request):
        # Browsers drop Secure cookies on plain HTTP; without this the sign-in would appear to do nothing.
        # The log names the peer, so a reverse proxy missing from LANVEX_TRUSTED_PROXIES is easy to find.
        log.warning(
            "sign_in_refused_without_https",
            peer=request.client.host if request.client else None,
            forwarded_proto=request.headers.get("x-forwarded-proto"),
            hint="If peer is your reverse proxy, add it to LANVEX_TRUSTED_PROXIES.",
        )
        raise ValidationFailed(
            "Lanvex only issues sessions over HTTPS in this configuration. Open it via https://, or, for a "
            "trusted test network, set LANVEX_COOKIE_SECURE=false.",
            code="insecure_transport",
        )
    ip = client_ip(request)
    limiter = get_rate_limiter(request)
    key = f"login:{ip}:{body.username.strip().lower()}"
    await limiter.hit(key, settings.login_rate_limit, settings.login_rate_window_seconds)
    user = await authenticate(db, body.username, body.password)
    method = "password"
    if user is None and settings.ldap_configured:
        known = await find_user(db, body.username)
        if known is None or known.auth_source == AuthSource.DIRECTORY:
            user = await _directory_sign_in(db, settings, audit, body.username, body.password)
            method = "directory"
    if user is None:
        await audit.record("auth.login", "denied", target_type="user", target_repr=body.username[:64])
        raise NotAuthenticated("Incorrect username or password.", code="invalid_credentials")
    token, session = await create_session(db, user, settings, ip, request.headers.get("user-agent"))
    await db.commit()
    await limiter.reset(key)
    await audit.record(
        "auth.login",
        "success",
        target_type="user",
        target_id=user.id,
        target_repr=user.username,
        actor=user,
        detail={"method": method},
    )
    _set_cookie(response, settings, token)
    return SessionOut(
        user=UserOut.model_validate(user), csrf_token=session.csrf_token, expires_at=session.expires_at
    )


async def _directory_sign_in(
    db: DB, settings: SettingsDep, audit: Audit, username: str, password: str
) -> User | None:
    try:
        who = await directory.sign_in(settings, username, password)
    except directory.DirectoryUnavailable as e:
        log.warning("directory_unavailable", error=str(e))
        await audit.record(
            "auth.login",
            "failure",
            target_type="user",
            target_repr=username[:64],
            detail={"method": "directory"},
        )
        raise UpstreamUnavailable(
            "The directory did not answer. Local accounts can still sign in.", code="directory_unavailable"
        ) from e
    except directory.NotAllowed:
        await audit.record(
            "auth.login",
            "denied",
            target_type="user",
            target_repr=username[:64],
            detail={"method": "directory", "reason": "not_in_group"},
        )
        raise NotAuthenticated(
            "This account is in none of the groups allowed to use Lanvex.", code="not_allowed"
        ) from None
    if who is None:
        return None
    try:
        return await provision(db, who)
    except SignInRefused as e:
        await audit.record(
            "auth.login", "denied", target_type="user", target_repr=username[:64], detail={"reason": e.code}
        )
        raise NotAuthenticated(e.message, code=e.code) from None


@router.get("/auth/methods", response_model=SignInMethods)
async def sign_in_methods(settings: SettingsDep) -> SignInMethods:
    return SignInMethods(
        directory=settings.ldap_configured,
        sso=settings.oidc_name if settings.oidc_configured else None,
    )


def _sso_http(request: Request) -> httpx.AsyncClient:
    # Tests give the app a transport that plays the identity provider.
    transport = getattr(request.app.state, "sso_transport", None)
    return httpx.AsyncClient(timeout=15.0, follow_redirects=False, transport=transport)


def _to_login(code: str) -> RedirectResponse:
    r = RedirectResponse(f"/login?sso_error={code}", status_code=status.HTTP_303_SEE_OTHER)
    r.delete_cookie(sso.COOKIE, path=sso.COOKIE_PATH)
    return r


@router.get("/auth/sso/start", include_in_schema=False)
async def sso_start(request: Request, settings: SettingsDep, next: str | None = None) -> Response:  # noqa: A002
    if not settings.oidc_configured:
        raise NotConfigured("Single sign-on is not set up.", code="sso_not_configured")
    try:
        async with _sso_http(request) as http:
            url, cookie = await sso.start(settings, http, next)
    except sso.SsoError as e:
        log.warning("sso_start_failed", code=e.code, error=e.detail)
        return _to_login(e.code)
    r = RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)
    # Lax, not Strict: the browser comes back from the identity provider, another site.
    r.set_cookie(
        sso.COOKIE,
        cookie,
        max_age=sso.MAX_AGE,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        path=sso.COOKIE_PATH,
    )
    return r


@router.get("/auth/sso/callback", include_in_schema=False)
async def sso_callback(
    request: Request,
    db: DB,
    settings: SettingsDep,
    audit: Audit,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Response:
    if not settings.oidc_configured:
        raise NotConfigured("Single sign-on is not set up.", code="sso_not_configured")
    if error or not code or not state:
        await audit.record(
            "auth.login", "denied", target_type="user", detail={"method": "sso", "reason": error or "no_code"}
        )
        return _to_login("cancelled" if error == "access_denied" else "failed")
    try:
        async with _sso_http(request) as http:
            who, next_path = await sso.finish(settings, http, code, state, request.cookies.get(sso.COOKIE))
        user = await provision(db, who)
    except (sso.SsoError, SignInRefused) as e:
        reason = e.code
        log.warning("sso_sign_in_failed", code=reason, error=getattr(e, "detail", str(e)))
        await audit.record(
            "auth.login", "denied", target_type="user", detail={"method": "sso", "reason": reason}
        )
        return _to_login(reason)
    token, session = await create_session(
        db, user, settings, client_ip(request), request.headers.get("user-agent")
    )
    await db.commit()
    await audit.record(
        "auth.login",
        "success",
        target_type="user",
        target_id=user.id,
        target_repr=user.username,
        actor=user,
        detail={"method": "sso"},
    )
    # A page that moves on by itself, rather than a redirect: the session cookie is SameSite=Strict,
    # and a browser leaves it out of a redirect that started at the identity provider.
    target = html.escape(next_path, quote=True)
    r = HTMLResponse(
        f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url={target}">'
        f'<title>Lanvex</title><a href="{target}">Continue</a>'
    )
    _set_cookie(r, settings, token)
    r.delete_cookie(sso.COOKIE, path=sso.COOKIE_PATH)
    return r


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    session: CurrentSession, response: Response, db: DB, settings: SettingsDep, audit: Audit
) -> Response:
    user = session.user
    await revoke_session(db, session)
    await db.commit()
    await audit.record(
        "auth.logout", "success", target_type="user", target_id=user.id, target_repr=user.username
    )
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/auth/session", response_model=SessionOut)
async def current_session(session: CurrentSession) -> SessionOut:
    return SessionOut(
        user=UserOut.model_validate(session.user),
        csrf_token=session.csrf_token,
        expires_at=session.expires_at,
    )


@router.post("/auth/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: PasswordChange, user: CurrentUser, session: CurrentSession, db: DB, audit: Audit
) -> None:
    if user.auth_source != AuthSource.LOCAL:
        raise Conflict(
            "This account's password is kept by the directory or identity provider, not by Lanvex."
        )
    if not verify_password(body.current_password, user.password_hash):
        await audit.record("auth.password_change", "denied", target_type="user", target_id=user.id)
        raise ValidationFailed(
            "Current password is incorrect.", fields={"current_password": ["Incorrect password."]}
        )
    user.password_hash = hash_password(body.new_password)
    # Sign out every other session for this user.
    await db.execute(
        delete(SessionModel).where(SessionModel.user_id == user.id, SessionModel.id != session.id)
    )
    await db.commit()
    await audit.record(
        "auth.password_change", "success", target_type="user", target_id=user.id, target_repr=user.username
    )


@router.get("/auth/configuration", response_model=SignInConfiguration)
async def sign_in_configuration(settings: SettingsDep, _: AdminUser) -> SignInConfiguration:
    d = None
    if settings.ldap_configured:
        encrypted = settings.ldap_starttls or all(u.startswith("ldaps://") for u in settings.ldap_url)
        d = {
            "type": settings.ldap_type,
            "urls": settings.ldap_url,
            "encrypted": encrypted,
            "user_base": settings.ldap_user_base,
            "admin_group": settings.ldap_admin_group,
            "readonly_group": settings.ldap_readonly_group,
        }
    sso_conf = None
    if settings.oidc_configured:
        sso_conf = {
            "name": settings.oidc_name,
            "issuer": settings.oidc_issuer,
            "client_id": settings.oidc_client_id,
            "callback": sso.callback_url(settings),
            "admin_groups": settings.oidc_admin_groups,
            "readonly_groups": settings.oidc_readonly_groups,
        }
    return SignInConfiguration(directory=d, sso=sso_conf)


@router.post("/auth/directory/test", response_model=DirectoryTest)
async def test_directory(settings: SettingsDep, _: AdminUser) -> DirectoryTest:
    if not settings.ldap_configured:
        return DirectoryTest(
            ok=False, message="No directory is set up (LANVEX_LDAP_URL, LANVEX_LDAP_USER_BASE, a group)."
        )
    try:
        return DirectoryTest(ok=True, message=await directory.test_connection(settings))
    except directory.DirectoryUnavailable as e:
        return DirectoryTest(ok=False, message=str(e))


# ---- user administration (admin only) ----------------------------------------------


@router.get("/users", response_model=list[UserOut])
async def list_users(db: DB, _: AdminUser) -> list[User]:
    return list((await db.scalars(select(User).order_by(User.username))).all())


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def add_user(body: UserCreate, db: DB, _: AdminUser, audit: Audit) -> User:
    async with audit.track(
        "user.create", "user", target_repr=body.username, detail={"role": body.role.value}
    ) as t:
        user = await create_user(db, body.username, body.password, body.role, body.display_name)
        await db.commit()
        t.target_id = user.id
    return user


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_user(user_id: uuid.UUID, db: DB, admin: AdminUser, audit: Audit) -> Response:
    """Delete a user. The audit log keeps its name; changes it prepared or approved keep theirs."""
    user = await db.get(User, user_id)
    if user is None:
        raise NotFound("User not found.")
    async with audit.track("user.delete", "user", target_id=user.id, target_repr=user.username):
        if user.id == admin.id:
            raise Conflict(
                "You cannot delete your own account. Another administrator can, or `lanvex delete-user`."
            )
        await retire_user(db, user.username, delete_it=True)
        await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/users/{user_id}", response_model=UserOut)
async def edit_user(user_id: uuid.UUID, body: UserUpdate, db: DB, admin: AdminUser, audit: Audit) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise NotFound("User not found.")
    changes = body.model_dump(exclude_unset=True, exclude={"password"})
    async with audit.track(
        "user.update",
        "user",
        target_id=user.id,
        target_repr=user.username,
        detail={**{k: str(v) for k, v in changes.items()}, "password_changed": body.password is not None},
    ):
        demoting = body.role is not None and body.role != Role.ADMIN and user.role == Role.ADMIN
        deactivating = body.is_active is False and user.role == Role.ADMIN
        if (demoting or deactivating) and await count_active_admins(db) <= 1:
            raise Conflict("Lanvex needs at least one active administrator.")
        if user.id == admin.id and body.is_active is False:
            raise Conflict("You cannot deactivate your own account.")
        if user.auth_source != AuthSource.LOCAL and (body.password is not None or body.role is not None):
            raise Conflict(
                "This account signs in with the directory or single sign-on: its groups there decide its role, "
                "and its password is kept there."
            )
        if body.display_name is not None:
            user.display_name = body.display_name
        if body.role is not None:
            user.role = body.role
        if body.is_active is not None:
            user.is_active = body.is_active
        if body.password is not None:
            user.password_hash = hash_password(body.password)
        if body.password is not None or body.is_active is False or body.role is not None:
            await revoke_user_sessions(db, user.id)
        await db.commit()
    await db.refresh(user)
    return user
