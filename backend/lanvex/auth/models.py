import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lanvex.core.db import Base, TimestampMixin, utcnow


class Role(enum.StrEnum):
    ADMIN = "admin"
    READONLY = "readonly"


class AuthSource(enum.StrEnum):
    LOCAL = "local"  # a password Lanvex keeps
    DIRECTORY = "directory"  # LDAP or Active Directory
    SSO = "sso"  # OpenID Connect


# Accounts that sign in elsewhere have no password in Lanvex; no Argon2 hash ever looks like this.
NO_PASSWORD = "!external"  # noqa: S105 - a marker, not a password


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("uq_users_username_lower", func.lower(text("username")), unique=True),
        Index("uq_users_external", "auth_source", "external_id", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[Role] = mapped_column(
        Enum(Role, name="user_role", values_callable=lambda e: [m.value for m in e]), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    auth_source: Mapped[str] = mapped_column(
        String(16), default=AuthSource.LOCAL.value, server_default=AuthSource.LOCAL.value, nullable=False
    )
    # The account in the directory (its DN or objectGUID) or at the identity provider (issuer and subject).
    external_id: Mapped[str | None] = mapped_column(String(512))

    sessions: Mapped[list["Session"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(256))

    user: Mapped[User] = relationship(back_populates="sessions", lazy="joined")
