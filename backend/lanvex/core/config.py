from functools import lru_cache
from typing import Annotated, Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    # Hosts seen in servers' neighbour tables, and MAC addresses seen on switch ports, are kept this
    # long after they were last seen.
    discovery_retention_days: int = Field(default=30, ge=1, le=3650)

    # Switches (read-only SNMP). Read on this schedule unless a switch sets its own; 0 = never.
    # LANVEX_SERVER_ALLOWED_NETWORKS limits switch addresses too.
    switch_read_interval_minutes: int = Field(default=15, ge=0, le=10080)
    snmp_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    snmp_retries: int = Field(default=1, ge=0, le=5)

    # Monitoring: servers' SSH ports are probed this often (a TCP connection, no sign-in), and checks
    # run after every read. The monitor (probes, sending alerts) runs in one process at a time.
    monitor_enabled: bool = True
    monitor_probe_seconds: int = Field(default=120, ge=30, le=3600)
    # Where Lanvex is reached in a browser, for links in alert messages (https://lanvex.example.net).
    public_url: str | None = None
    # Email for alerts. The recipients are set in Lanvex (Settings, Monitoring).
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_security: Literal["starttls", "tls", "none"] = "starttls"
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None
    smtp_timeout_seconds: float = Field(default=15.0, gt=0, le=120)

    # Changing server configuration. Off unless explicitly enabled; each server must also be opted in.
    automation_enabled: bool = False
    change_rollback_seconds: int = Field(default=120, ge=30, le=900)
    changes_require_second_admin: bool = False
    change_runner_enabled: bool = (
        True  # run approved changes in this process; tests drive the runner directly
    )

    trusted_proxies: list[str] = Field(default_factory=list)

    # Sign-in with a directory: LDAP or Active Directory. Users sign in with their directory account;
    # membership in one of the two groups (nested groups count on Active Directory) decides the role.
    ldap_url: Annotated[list[str], NoDecode] = Field(default_factory=list)  # several for failover
    ldap_type: Literal["ad", "ldap"] = "ldap"
    ldap_starttls: bool = False
    ldap_ca_file: str | None = None
    ldap_bind_dn: str | None = None
    ldap_bind_password: SecretStr | None = None
    ldap_user_base: str | None = None
    ldap_user_filter: str | None = None  # {username} is replaced; the default depends on the type
    ldap_admin_group: str | None = None
    ldap_readonly_group: str | None = None
    ldap_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    # Single sign-on with OpenID Connect (Keycloak, Entra ID, Authentik, Google Workspace, …).
    # Callback: <LANVEX_PUBLIC_URL>/api/v1/auth/sso/callback.
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: SecretStr | None = None
    oidc_scopes: str = "openid profile email"
    oidc_username_claim: str = "preferred_username"
    oidc_groups_claim: str = "groups"
    oidc_admin_groups: list[str] = Field(default_factory=list)
    oidc_readonly_groups: list[str] = Field(default_factory=list)
    oidc_name: str = Field(default="Single sign-on", max_length=40)  # the sign-in button's label

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

    @field_validator("ldap_url", mode="before")
    @classmethod
    def _ldap_urls(cls, v: object) -> object:
        # One URL as it is, or several separated by spaces; a JSON list works too.
        if isinstance(v, str) and not v.strip().startswith("["):
            return v.split()
        return v

    @field_validator("ldap_url")
    @classmethod
    def _ldap_scheme(cls, v: list[str]) -> list[str]:
        for url in v:
            if not url.startswith(("ldap://", "ldaps://")):
                raise ValueError("LANVEX_LDAP_URL must start with ldap:// or ldaps://")
        return v

    @field_validator("oidc_issuer")
    @classmethod
    def _issuer(cls, v: str | None) -> str | None:
        if v is not None and v.strip() and not v.startswith(("https://", "http://")):
            raise ValueError("LANVEX_OIDC_ISSUER must be an https:// URL")
        return v.rstrip("/") if v and v.strip() else None

    @field_validator(
        "netbox_token",
        "bootstrap_admin_password",
        "secret_key",
        "smtp_password",
        "ldap_bind_password",
        "oidc_client_secret",
        mode="before",
    )
    @classmethod
    def _empty_secret_is_none(cls, v: object) -> object:
        return None if v == "" else v

    @field_validator(
        "smtp_host",
        "smtp_username",
        "smtp_from",
        "public_url",
        "ldap_ca_file",
        "ldap_bind_dn",
        "ldap_user_base",
        "ldap_user_filter",
        "ldap_admin_group",
        "ldap_readonly_group",
        "oidc_client_id",
        mode="before",
    )
    @classmethod
    def _empty_is_none(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("public_url")
    @classmethod
    def _http_url(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(("http://", "https://")):
            raise ValueError("LANVEX_PUBLIC_URL must start with http:// or https://")
        return v.rstrip("/") if v else v

    @property
    def ldap_configured(self) -> bool:
        return bool(
            self.ldap_url and self.ldap_user_base and (self.ldap_admin_group or self.ldap_readonly_group)
        )

    @property
    def oidc_configured(self) -> bool:
        return bool(
            self.oidc_issuer
            and self.oidc_client_id
            and self.public_url
            and (self.oidc_admin_groups or self.oidc_readonly_groups)
        )

    @property
    def smtp_configured(self) -> bool:
        return self.smtp_host is not None and self.smtp_from is not None

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
