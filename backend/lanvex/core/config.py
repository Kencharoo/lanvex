from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration. Values come from LANVEX_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="LANVEX_", env_file=".env", extra="ignore")

    env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    log_json: bool = False

    database_url: SecretStr = SecretStr("postgresql+asyncpg://lanvex:lanvex@localhost:5432/lanvex")
    redis_url: str | None = "redis://localhost:6379/0"

    # NetBox
    netbox_url: AnyHttpUrl | None = None
    netbox_token: SecretStr | None = None
    netbox_verify_tls: bool = True
    netbox_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    netbox_max_retries: int = Field(default=2, ge=0, le=5)

    # Sessions
    session_cookie_name: str = "lanvex_session"
    session_idle_minutes: int = Field(default=60, ge=5)
    session_absolute_hours: int = Field(default=12, ge=1)
    cookie_secure: bool | None = None  # defaults to True in production

    # Rate limits
    login_rate_limit: int = 10  # attempts per window per IP+username
    login_rate_window_seconds: int = 300
    write_rate_limit: int = 120  # write operations per window per user
    write_rate_window_seconds: int = 60

    # Initial admin (created on startup if no users exist)
    bootstrap_admin_username: str | None = None
    bootstrap_admin_password: SecretStr | None = None

    # Phase 2 — infrastructure-changing automation. Off unless explicitly enabled.
    automation_enabled: bool = False

    trusted_proxies: list[str] = Field(default_factory=list)

    @field_validator("netbox_url", mode="before")
    @classmethod
    def _empty_url_is_none(cls, v: object) -> object:
        return None if v == "" else v

    @field_validator("netbox_token", "bootstrap_admin_password", mode="before")
    @classmethod
    def _empty_secret_is_none(cls, v: object) -> object:
        return None if v == "" else v

    @property
    def secure_cookies(self) -> bool:
        return self.cookie_secure if self.cookie_secure is not None else self.env == "production"

    @property
    def netbox_configured(self) -> bool:
        return self.netbox_url is not None and self.netbox_token is not None


@lru_cache
def get_settings() -> Settings:
    return Settings()
