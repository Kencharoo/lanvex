"""Signing in with a directory: LDAP or Active Directory.

Lanvex binds with its service account, finds the user under LANVEX_LDAP_USER_BASE, then binds as that
user with the password given (on a connection of its own, so the password is checked by the directory
and never stored). Membership in LANVEX_LDAP_ADMIN_GROUP or LANVEX_LDAP_READONLY_GROUP decides the
role; a user in neither may not sign in. On Active Directory nested groups count
(LDAP_MATCHING_RULE_IN_CHAIN); elsewhere the user's memberOf, or the group's member, uniqueMember or
memberUid.

ldap3 is synchronous; every directory conversation runs in a worker thread.
"""

import asyncio
import ssl
from dataclasses import dataclass
from typing import Any

import ldap3
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars
from ldap3.utils.dn import parse_dn

from lanvex.auth.models import AuthSource, Role
from lanvex.core.config import Settings
from lanvex.core.logging import get_logger

log = get_logger("lanvex.auth.directory")

IN_CHAIN = "1.2.840.113556.1.4.1941"  # Active Directory: membership through nested groups
FILTERS = {
    "ad": "(&(objectCategory=person)(objectClass=user)(|(sAMAccountName={username})(userPrincipalName={username})))",
    "ldap": "(&(objectClass=person)(uid={username}))",
}
LOGIN_ATTRIBUTE = {"ad": "sAMAccountName", "ldap": "uid"}


class DirectoryUnavailable(Exception):
    """The directory did not answer, or refused Lanvex's own account."""


class NotAllowed(Exception):
    """The password was right, but the account is in neither group."""


@dataclass
class Identity:
    source: AuthSource
    external_id: str
    username: str
    display_name: str | None
    role: Role


def _first(value: Any) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, bytes):
        return value.hex()  # objectGUID, read without the schema
    return str(value) if value not in (None, "") else None


def same_dn(a: str, b: str) -> bool:
    """DNs compare without regard to case and spacing around separators."""
    try:
        return [(t.lower(), v.lower()) for t, v, _ in parse_dn(a)] == [
            (t.lower(), v.lower()) for t, v, _ in parse_dn(b)
        ]
    except LDAPException:
        return a.strip().lower() == b.strip().lower()


# Tests replace these with ldap3's in-memory directory (MOCK_SYNC and a Server holding its entries).
STRATEGY: Any = ldap3.SYNC
SERVER: Any = None


class Directory:
    """One sign-in attempt's worth of directory conversation."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.strategy = STRATEGY
        self.server = SERVER if SERVER is not None else self._server()

    def _server(self) -> Any:
        tls = ldap3.Tls(
            validate=ssl.CERT_REQUIRED, ca_certs_file=self.s.ldap_ca_file, version=ssl.PROTOCOL_TLS_CLIENT
        )
        servers = [
            ldap3.Server(
                url,
                use_ssl=url.startswith("ldaps://"),
                tls=tls,
                get_info=ldap3.NONE,
                connect_timeout=max(1, int(self.s.ldap_timeout_seconds)),
            )
            for url in self.s.ldap_url
        ]
        if len(servers) == 1:
            return servers[0]
        return ldap3.ServerPool(servers, ldap3.FIRST, active=1, exhaust=True)

    def _connect(self, user: str | None, password: str | None) -> tuple[Any, bool]:
        conn = ldap3.Connection(
            self.server,
            user=user,
            password=password,
            client_strategy=self.strategy,
            receive_timeout=max(1, int(self.s.ldap_timeout_seconds)),
            raise_exceptions=False,
            read_only=True,
        )
        conn.open()
        if self.s.ldap_starttls and not conn.start_tls():
            raise DirectoryUnavailable("The directory refused STARTTLS.")
        return conn, bool(conn.bind())

    def sign_in(self, username: str, password: str) -> Identity | None:
        """The account's identity and role, None for a wrong name or password."""
        username = username.strip()
        if not username or not password:  # an empty password would be an anonymous bind, which succeeds
            return None
        bind_password = self.s.ldap_bind_password.get_secret_value() if self.s.ldap_bind_password else None
        try:
            svc, ok = self._connect(self.s.ldap_bind_dn, bind_password)
        except LDAPException as e:
            raise DirectoryUnavailable(f"The directory did not answer: {e}.") from e
        if not ok:
            raise DirectoryUnavailable(
                f"The directory refused Lanvex's account ({svc.result.get('description', 'refused')})."
            )
        try:
            kind = self.s.ldap_type
            template = self.s.ldap_user_filter or FILTERS[kind]
            query = template.replace("{username}", escape_filter_chars(username))
            login = LOGIN_ATTRIBUTE[kind]
            attrs = [login, "displayName", "cn", "memberOf"] + (["objectGUID"] if kind == "ad" else [])
            svc.search(self.s.ldap_user_base, query, ldap3.SUBTREE, attributes=attrs, size_limit=2)
            found = [e for e in svc.response or [] if e.get("type") == "searchResEntry"]
            if len(found) != 1:
                return None  # nobody, or not one person: refuse either way
            entry = found[0]
            dn, a = entry["dn"], entry.get("attributes") or {}
            user, ok = self._connect(dn, password)
            user.unbind()
            if not ok:
                return None
            role = self._role(svc, dn, _first(a.get(login)) or username, a.get("memberOf") or [])
            if role is None:
                raise NotAllowed(username)
            guid = _first(a.get("objectGUID")) if kind == "ad" else None
            return Identity(
                source=AuthSource.DIRECTORY,
                external_id=guid or dn.lower(),
                username=(_first(a.get(login)) or username)[:64],
                display_name=(_first(a.get("displayName")) or _first(a.get("cn")) or None),
                role=role,
            )
        except LDAPException as e:
            raise DirectoryUnavailable(f"The directory did not answer: {e}.") from e
        finally:
            svc.unbind()

    def _role(self, conn: Any, dn: str, login: str, member_of: list[str]) -> Role | None:
        for group, role in (
            (self.s.ldap_admin_group, Role.ADMIN),
            (self.s.ldap_readonly_group, Role.READONLY),
        ):
            if group and self._member(conn, dn, login, member_of, group):
                return role
        return None

    def _member(self, conn: Any, dn: str, login: str, member_of: list[str], group: str) -> bool:
        if self.s.ldap_type == "ad":
            conn.search(
                dn, f"(memberOf:{IN_CHAIN}:={escape_filter_chars(group)})", ldap3.BASE, attributes=["1.1"]
            )
            return any(e.get("type") == "searchResEntry" for e in conn.response or [])
        if any(same_dn(g, group) for g in member_of):
            return True
        d, u = escape_filter_chars(dn), escape_filter_chars(login)
        conn.search(
            group, f"(|(member={d})(uniqueMember={d})(memberUid={u}))", ldap3.BASE, attributes=["1.1"]
        )
        return any(e.get("type") == "searchResEntry" for e in conn.response or [])


async def sign_in(settings: Settings, username: str, password: str) -> Identity | None:
    return await asyncio.to_thread(Directory(settings).sign_in, username, password)


async def test_connection(settings: Settings) -> str:
    """Lanvex's own account against the directory; the outcome in words. Raises DirectoryUnavailable."""

    def check() -> str:
        d = Directory(settings)
        bind_password = (
            settings.ldap_bind_password.get_secret_value() if settings.ldap_bind_password else None
        )
        try:
            conn, ok = d._connect(settings.ldap_bind_dn, bind_password)
        except LDAPException as e:
            raise DirectoryUnavailable(f"The directory did not answer: {e}.") from e
        try:
            if not ok:
                raise DirectoryUnavailable(
                    f"The directory refused Lanvex's account ({conn.result.get('description')})."
                )
            conn.search(settings.ldap_user_base, "(objectClass=*)", ldap3.BASE, attributes=["1.1"])
            if not conn.response:
                raise DirectoryUnavailable(f"Lanvex's account cannot read {settings.ldap_user_base}.")
            return "Connected; Lanvex's account can read the user base."
        finally:
            conn.unbind()

    return await asyncio.to_thread(check)
