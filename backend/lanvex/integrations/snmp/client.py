"""SNMP reads: GET and GETBULK walks over SNMPv2c or SNMPv3. Lanvex never sends SET requests.

pysnmp does the protocol, including SNMPv3's user-based security (authentication and privacy). No MIB
files are loaded: Lanvex asks for numeric OIDs and interprets the values itself (switch.py).
"""

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    Udp6TransportTarget,
    UdpTransportTarget,
    UsmUserData,
    bulk_walk_cmd,
    get_cmd,
)
from pysnmp.hlapi.v3arch.asyncio import auth as usm
from pysnmp.proto import errind, rfc1902, rfc1905

from lanvex.integrations.linux.transport import TargetNotAllowed, check_allowed

Oid = tuple[int, ...]

AuthProtocol = Literal["none", "md5", "sha", "sha224", "sha256", "sha384", "sha512"]
PrivProtocol = Literal["none", "des", "aes", "aes192", "aes256", "aes192c", "aes256c"]

AUTH = {
    "none": usm.USM_AUTH_NONE,
    "md5": usm.USM_AUTH_HMAC96_MD5,
    "sha": usm.USM_AUTH_HMAC96_SHA,
    "sha224": usm.USM_AUTH_HMAC128_SHA224,
    "sha256": usm.USM_AUTH_HMAC192_SHA256,
    "sha384": usm.USM_AUTH_HMAC256_SHA384,
    "sha512": usm.USM_AUTH_HMAC384_SHA512,
}
# AES-192/256 as Net-SNMP does them (Blumenthal key extension), and as Cisco does them (Reeder).
PRIV = {
    "none": usm.USM_PRIV_NONE,
    "des": usm.USM_PRIV_CBC56_DES,
    "aes": usm.USM_PRIV_CFB128_AES,
    "aes192": usm.USM_PRIV_CFB192_AES_BLUMENTHAL,
    "aes256": usm.USM_PRIV_CFB256_AES_BLUMENTHAL,
    "aes192c": usm.USM_PRIV_CFB192_AES,
    "aes256c": usm.USM_PRIV_CFB256_AES,
}

MAX_ROWS = 200_000  # per walk: a runaway agent must not exhaust memory


class SnmpCredentials(BaseModel):
    """What signing in takes. Stored encrypted; never shown again after it was entered."""

    version: Literal["2c", "3"]
    community: str | None = Field(default=None, max_length=255)
    username: str | None = Field(default=None, max_length=64)
    auth_protocol: AuthProtocol = "none"
    auth_password: str | None = Field(default=None, max_length=255)
    priv_protocol: PrivProtocol = "none"
    priv_password: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def _complete(self) -> "SnmpCredentials":
        if self.version == "2c":
            if not self.community:
                raise ValueError("Enter the community.")
            self.username, self.auth_password, self.priv_password = None, None, None
            self.auth_protocol, self.priv_protocol = "none", "none"
            return self
        self.community = None
        if not (self.username or "").strip():
            raise ValueError("Enter the SNMPv3 user name.")
        self.username = self.username.strip()  # type: ignore[union-attr]
        if self.auth_protocol == "none":
            if self.priv_protocol != "none":
                raise ValueError("Privacy needs authentication too.")
            self.auth_password = self.priv_password = None
            return self
        if len(self.auth_password or "") < 8:
            raise ValueError("The authentication password needs at least 8 characters.")
        if self.priv_protocol == "none":
            self.priv_password = None
        elif len(self.priv_password or "") < 8:
            raise ValueError("The privacy password needs at least 8 characters.")
        return self

    def summary(self) -> dict[str, Any]:
        """What may be shown: never the community or the passwords."""
        if self.version == "2c":
            return {"version": "2c"}
        return {
            "version": "3",
            "username": self.username,
            "auth_protocol": self.auth_protocol,
            "priv_protocol": self.priv_protocol,
        }


class ContextUnavailable(Exception):
    """A per-VLAN community or context (Cisco) did not answer."""


class SnmpError(Exception):
    """A read that did not work, with the outcome it maps to (ConnectionStatus values)."""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status, self.message = status, message


_AUTH_ERRORS = (
    errind.UnknownUserName,
    errind.WrongDigest,
    errind.DecryptionError,
    errind.AuthenticationError,
    errind.AuthenticationFailure,
    errind.UnsupportedSecurityLevel,
    errind.UnknownSecurityName,
    errind.NotInTimeWindow,
    errind.UnknownCommunityName,
)
_AUTH_TEXT = {
    errind.UnknownUserName: "The switch does not know this SNMPv3 user.",
    errind.WrongDigest: "The authentication password or protocol does not match.",
    errind.DecryptionError: "The privacy password or protocol does not match.",
    errind.UnsupportedSecurityLevel: "The switch does not allow this user with this security level.",
}


def _failure(indication: Any, version: str) -> SnmpError:
    if isinstance(indication, errind.RequestTimedOut):
        return SnmpError(
            "unreachable",
            "No answer. Check the address and port"
            + (", and the community." if version == "2c" else ".")
            + " The switch may also not allow SNMP from Lanvex's address.",
        )
    for cls, text in _AUTH_TEXT.items():
        if isinstance(indication, cls):
            return SnmpError("auth_failed", text)
    if isinstance(indication, _AUTH_ERRORS):
        return SnmpError("auth_failed", f"Sign-in rejected: {indication}.")
    return SnmpError("error", f"SNMP error: {indication}.")


def value(v: Any) -> Any:
    """A pysnmp value as plain Python: bytes for strings, int for numbers, dotted text for OIDs."""
    if isinstance(v, rfc1902.IpAddress):
        return str(ipaddress.IPv4Address(bytes(v.asOctets())))
    if isinstance(v, rfc1902.OctetString):
        return bytes(v.asOctets())
    if isinstance(v, rfc1902.ObjectIdentifier):
        return ".".join(str(x) for x in v)
    if isinstance(v, rfc1905.NoSuchObject | rfc1905.NoSuchInstance | rfc1905.EndOfMibView):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return str(v)


def parse_oid(text: str) -> Oid:
    return tuple(int(x) for x in text.strip(".").split("."))


async def resolve(address: str, port: int) -> str:
    try:
        return str(ipaddress.ip_address(address))
    except ValueError:
        pass
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(address, port, type=socket.SOCK_DGRAM), 5)
    except (TimeoutError, OSError) as e:
        raise SnmpError("unreachable", f"{address} could not be resolved.") from e
    return str(sorted({i[4][0] for i in infos})[0])


class SnmpSession:
    """One SNMP engine talking to one agent. Use `connect`."""

    def __init__(self, engine: SnmpEngine, target: Any, creds: SnmpCredentials, max_repetitions: int) -> None:
        self.engine, self.target, self.creds = engine, target, creds
        self.max_repetitions = max_repetitions
        self.requests = 0

    def _auth(self, vlan: int | None) -> CommunityData | UsmUserData:
        c = self.creds
        if c.version == "2c":
            community = c.community or ""
            # Cisco IOS: each VLAN's bridge tables answer to "community@vlan".
            # SNMPv2c sends the community in clear text; it is offered because many switches have nothing
            # else. The UI recommends v3.
            return CommunityData(f"{community}@{vlan}" if vlan else community, mpModel=1)  # noqa: S508
        return UsmUserData(
            c.username or "",
            authKey=c.auth_password if c.auth_protocol != "none" else None,
            privKey=c.priv_password if c.priv_protocol != "none" else None,
            authProtocol=AUTH[c.auth_protocol],
            privProtocol=PRIV[c.priv_protocol],
        )

    def _context(self, vlan: int | None) -> ContextData:
        # SNMPv3: the same per-VLAN tables live in the context "vlan-<id>".
        return ContextData(contextName=f"vlan-{vlan}" if vlan and self.creds.version == "3" else "")

    async def get(self, *oids: str) -> dict[str, Any]:
        """GET some scalars. Those the agent does not have come back as None."""
        self.requests += 1
        indication, status, _, binds = await get_cmd(
            self.engine,
            self._auth(None),
            self.target,
            self._context(None),
            *(ObjectType(ObjectIdentity(o)) for o in oids),
            lookupMib=False,
        )
        if indication:
            raise _failure(indication, self.creds.version)
        if status:  # noSuchName and friends: nothing to read here
            return dict.fromkeys(oids)
        return {".".join(str(x) for x in name): value(v) for name, v in binds}

    async def walk(self, oid: str, vlan: int | None = None) -> list[tuple[Oid, Any]]:
        """Every (OID, value) under `oid`, in order. An agent without that subtree gives an empty list."""
        base = parse_oid(oid)
        rows: list[tuple[Oid, Any]] = []
        async for indication, status, _, binds in bulk_walk_cmd(
            self.engine,
            self._auth(vlan),
            self.target,
            self._context(vlan),
            0,
            self.max_repetitions,
            ObjectType(ObjectIdentity(oid)),
            lexicographicMode=False,
            lookupMib=False,
        ):
            self.requests += 1
            if indication:
                if vlan:  # no answer for this VLAN's community or context
                    raise ContextUnavailable(str(indication))
                raise _failure(indication, self.creds.version)
            if status:
                break
            for name, v in binds:
                key = tuple(int(x) for x in name)
                if key[: len(base)] != base:
                    return rows
                converted = value(v)
                if converted is not None:
                    rows.append((key[len(base) :], converted))
            if len(rows) >= MAX_ROWS:
                break
        return rows


@asynccontextmanager
async def connect(
    address: str,
    port: int,
    creds: SnmpCredentials,
    *,
    allowed: list[str],
    timeout: float = 2.0,
    retries: int = 1,
    max_repetitions: int = 20,
) -> AsyncIterator[SnmpSession]:
    ip = await resolve(address, port)
    try:
        check_allowed([ip], allowed)
    except TargetNotAllowed as e:
        raise SnmpError("not_allowed", e.message) from e
    target_cls = Udp6TransportTarget if ipaddress.ip_address(ip).version == 6 else UdpTransportTarget
    target = await target_cls.create((ip, port), timeout=timeout, retries=retries)
    engine = SnmpEngine()
    try:
        yield SnmpSession(engine, target, creds, max_repetitions)
    finally:
        engine.close_dispatcher()
