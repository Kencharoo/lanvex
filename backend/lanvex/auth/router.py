import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Request, Response, status
from pydantic import Field
from sqlalchemy import delete, select

from lanvex.auth.models import Role, User
from lanvex.auth.models import Session as SessionModel
from lanvex.auth.service import (
    authenticate,
    count_active_admins,
    create_session,
    create_user,
    hash_password,
    revoke_session,
    revoke_user_sessions,
    verify_password,
)
from lanvex.core.errors import Conflict, NotAuthenticated, NotFound, ValidationFailed
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
    if user is None:
        await audit.record("auth.login", "denied", target_type="user", target_repr=body.username[:64])
        raise NotAuthenticated("Incorrect username or password.", code="invalid_credentials")
    token, session = await create_session(db, user, settings, ip, request.headers.get("user-agent"))
    await db.commit()
    await limiter.reset(key)
    await audit.record(
        "auth.login", "success", target_type="user", target_id=user.id, target_repr=user.username, actor=user
    )
    _set_cookie(response, settings, token)
    return SessionOut(
        user=UserOut.model_validate(user), csrf_token=session.csrf_token, expires_at=session.expires_at
    )


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
