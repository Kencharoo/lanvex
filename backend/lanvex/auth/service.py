import hashlib
import hmac
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lanvex.auth.models import Role, Session, User
from lanvex.core.config import Settings
from lanvex.core.db import utcnow
from lanvex.core.errors import Conflict, ValidationFailed

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


async def authenticate(db: AsyncSession, username: str, password: str) -> User | None:
    user = await db.scalar(select(User).where(func.lower(User.username) == username.strip().lower()))
    if user is None:
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, user.password_hash) or not user.is_active:
        return None
    if _hasher.check_needs_rehash(user.password_hash):
        user.password_hash = _hasher.hash(password)
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


async def count_active_admins(db: AsyncSession) -> int:
    return int(
        await db.scalar(
            select(func.count()).select_from(User).where(User.role == Role.ADMIN, User.is_active.is_(True))
        )
        or 0
    )
