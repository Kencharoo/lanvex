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
    # Where browsers open NetBox objects, when that differs from netbox_url (which is how Lanvex reaches
    # NetBox). With Lanvex Core, Lanvex reaches it at http://core:8080/core and browsers at /core.
    netbox_public_url: str | None = None

    # Lanvex Core: the NetBox-based application bundled with Lanvex, served under /core/ through
    # lanvex/core_proxy.py, which signs Lanvex users in to it. Off unless both are set.
    core_upstream_url: AnyHttpUrl | None = None  # e.g. http://core:8080
    core_proxy_secret: SecretStr | None = None  # shared with Lanvex Core; at least 32 characters
    core_static_dir: str | None = None  # Lanvex Core's collected static files, served directly

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

    # Credentials at rest (server SSH keys and passwords)
    secret_key: SecretStr | None = None
    secret_key_fallbacks: list[SecretStr] = Field(default_factory=list)

    # Server management (read-only SSH)
    server_allowed_networks: list[str] = Field(
        default_factory=list, description="If set, Lanvex only connects to servers inside these CIDRs."
    )
    ssh_connect_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    ssh_command_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    # Snapshots are kept only when a read finds something different, so this many changes are kept.
    server_snapshot_retention: int = Field(default=200, ge=1, le=5000)
    # Read every server on a schedule (read-only). Servers may set their own interval; 0 turns the
    # schedule off for every server.
    server_read_interval_minutes: int = Field(default=60, ge=0, le=10080)
    read_scheduler_enabled: bool = True  # run scheduled reads in this process; tests drive them directly

    # Changing server configuration. Off unless explicitly enabled; each server must also be opted in.
    automation_enabled: bool = False
    change_rollback_seconds: int = Field(default=120, ge=30, le=900)
    changes_require_second_admin: bool = False
    change_runner_enabled: bool = (
        True  # run approved changes in this process; tests drive the runner directly
    )

    trusted_proxies: list[str] = Field(default_factory=list)

    @field_validator("netbox_url", "core_upstream_url", "netbox_public_url", "core_static_dir", mode="before")
    @classmethod
    def _empty_url_is_none(cls, v: object) -> object:
        return None if v == "" else v

    @field_validator("netbox_public_url")
    @classmethod
    def _public_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not (v.startswith("/") or v.startswith("http://") or v.startswith("https://")):
            raise ValueError("must be a path starting with / or an http(s) URL")
        return v.rstrip("/")

    @field_validator("core_proxy_secret", mode="before")
    @classmethod
    def _proxy_secret(cls, v: object) -> object:
        if v == "" or v is None:
            return None
        if len(str(v)) < 32:
            raise ValueError("must be at least 32 characters; the installer generates one")
        return v

    @field_validator("server_allowed_networks")
    @classmethod
    def _valid_networks(cls, v: list[str]) -> list[str]:
        import ipaddress

        return [str(ipaddress.ip_network(n, strict=False)) for n in v]

    @field_validator("netbox_token", "bootstrap_admin_password", "secret_key", mode="before")
    @classmethod
    def _empty_secret_is_none(cls, v: object) -> object:
        return None if v == "" else v

    @property
    def secure_cookies(self) -> bool:
        return self.cookie_secure if self.cookie_secure is not None else self.env == "production"

    @property
    def netbox_configured(self) -> bool:
        return self.netbox_url is not None and self.netbox_token is not None

    @property
    def core_enabled(self) -> bool:
        return self.core_upstream_url is not None and self.core_proxy_secret is not None

    def public_netbox_url(self, url: str | None) -> str | None:
        """A NetBox link as a browser should open it (see netbox_public_url)."""
        if not url or self.netbox_public_url is None or self.netbox_url is None:
            return url
        internal = str(self.netbox_url).rstrip("/")
        if url == internal or url.startswith(internal + "/"):
            return self.netbox_public_url + url[len(internal) :]
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
