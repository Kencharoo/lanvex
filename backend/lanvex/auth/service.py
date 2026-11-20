import hashlib
import hmac
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lanvex.auth.directory import Identity
from lanvex.auth.models import NO_PASSWORD, AuthSource, Role, Session, User
from lanvex.core.config import Settings
from lanvex.core.db import utcnow
from lanvex.core.errors import Conflict, NotFound, ValidationFailed

_hasher = PasswordHasher()
# Verified against when the username does not exist, so response time does not reveal valid usernames.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))

MIN_PASSWORD_LENGTH = 12


def hash_password(password: str) -> str:
    validate_password(password)
    return _hasher.hash(password)


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationFailed(
            "Password is too short.", fields={"password": [f"Use at least {MIN_PASSWORD_LENGTH} characters."]}
        )
    if len(password) > 256:
        raise ValidationFailed("Password is too long.", fields={"password": ["Use at most 256 characters."]})


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


class SignInRefused(Exception):
    """Signed in elsewhere, but Lanvex will not let this account in; `code` says why."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


async def find_user(db: AsyncSession, username: str) -> User | None:
    return await db.scalar(select(User).where(func.lower(User.username) == username.strip().lower()))


async def authenticate(db: AsyncSession, username: str, password: str) -> User | None:
    """A local user with this password. Directory and SSO accounts have no password here."""
    user = await find_user(db, username)
    if user is None or user.auth_source != AuthSource.LOCAL:
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, user.password_hash) or not user.is_active:
        return None
    if _hasher.check_needs_rehash(user.password_hash):
        user.password_hash = _hasher.hash(password)
    return user


async def provision(db: AsyncSession, who: Identity) -> User:
    """The Lanvex account for someone the directory or the identity provider signed in: created on their
    first sign-in, with role and name brought up to date on every one."""
    user = await db.scalar(
        select(User).where(User.auth_source == who.source.value, User.external_id == who.external_id)
    )
    if user is None:
        if await find_user(db, who.username):
            raise SignInRefused(
                "taken",
                f"Lanvex already has a user named {who.username}. An administrator can rename or remove it.",
            )
        user = User(
            username=who.username,
            display_name=who.display_name,
            password_hash=NO_PASSWORD,
            role=who.role,
            auth_source=who.source.value,
            external_id=who.external_id,
        )
        db.add(user)
        await db.flush()
        return user
    if not user.is_active:
        raise SignInRefused("disabled", "This account is deactivated in Lanvex.")
    if user.role != who.role:
        user.role = who.role
    if who.display_name:
        user.display_name = who.display_name
    if who.username.lower() != user.username.lower() and not await find_user(db, who.username):
        user.username = who.username  # renamed in the directory
    await db.flush()
    return user


async def create_session(
    db: AsyncSession, user: User, settings: Settings, ip: str | None, user_agent: str | None
) -> tuple[str, Session]:
    token = secrets.token_urlsafe(32)
    now = utcnow()
    session = Session(
        token_hash=hash_token(token),
        csrf_token=secrets.token_urlsafe(32),
        user_id=user.id,
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(hours=settings.session_absolute_hours),
        ip_address=ip,
        user_agent=(user_agent or "")[:256] or None,
    )
    user.last_login_at = now
    db.add(session)
    await db.flush()
    return token, session


async def resolve_session(db: AsyncSession, token: str, settings: Settings) -> Session | None:
    session = await db.scalar(select(Session).where(Session.token_hash == hash_token(token)))
    if session is None:
        return None
    now = utcnow()
    idle_limit = session.last_seen_at + timedelta(minutes=settings.session_idle_minutes)
    if now >= session.expires_at or now >= idle_limit or not session.user.is_active:
        await db.delete(session)
        await db.commit()
        return None
    # Avoid a write on every request; a minute of granularity is plenty for idle expiry.
    if (now - session.last_seen_at) > timedelta(minutes=1):
        session.last_seen_at = now
        await db.commit()
    return session


async def revoke_session(db: AsyncSession, session: Session) -> None:
    await db.delete(session)


async def revoke_user_sessions(db: AsyncSession, user_id: object) -> None:
    await db.execute(delete(Session).where(Session.user_id == user_id))


async def create_user(
    db: AsyncSession, username: str, password: str, role: Role, display_name: str | None = None
) -> User:
    existing = await db.scalar(select(User).where(func.lower(User.username) == username.lower()))
    if existing:
        raise Conflict("A user with this username already exists.", fields={"username": ["Already taken."]})
    user = User(
        username=username, password_hash=hash_password(password), role=role, display_name=display_name
    )
    db.add(user)
    await db.flush()
    return user


async def retire_user(db: AsyncSession, username: str, *, delete_it: bool) -> User:
    """Deactivate a user (sessions ended) or delete it. Lanvex always keeps one active administrator.
    The audit log keeps the user's name either way."""
    user = await db.scalar(select(User).where(func.lower(User.username) == username.lower()))
    if user is None:
        raise NotFound(f"There is no user '{username}'.")
    if user.role == Role.ADMIN and user.is_active and await count_active_admins(db) <= 1:
        raise Conflict("Lanvex needs at least one active administrator.")
    await revoke_user_sessions(db, user.id)
    if delete_it:
        await db.delete(user)
    else:
        user.is_active = False
    await db.flush()
    return user


async def count_active_admins(db: AsyncSession) -> int:
    return int(
        await db.scalar(
            select(func.count()).select_from(User).where(User.role == Role.ADMIN, User.is_active.is_(True))
        )
        or 0
    )
