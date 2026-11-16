"""SSH transport for Linux servers.

Security properties, all enforced here:
- The server's host key must match the one an administrator pinned; anything else is refused.
- Only the credential stored for that server is used: no SSH agent, no ~/.ssh keys, no ~/.ssh/config.
- Only commands from commands.COMMANDS can be run (callers pass a Cmd, not a string).
- Every step has a timeout; output is capped.
"""

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import asyncssh

from lanvex.core.errors import LanvexError
from lanvex.integrations.linux.assemble import CommandResult
from lanvex.integrations.linux.commands import COMMANDS, STDIN_COMMANDS, Cmd

MAX_OUTPUT = 4 * 1024 * 1024
HOST_KEY_ALGS = (
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "rsa-sha2-512",
    "rsa-sha2-256",
)


class SSHError(LanvexError):
    """Connection-level failure. `code` is one of the ConnectionStatus values."""

    status_code = 502


class TargetNotAllowed(SSHError):
    """The address is outside LANVEX_SERVER_ALLOWED_NETWORKS. A policy refusal, not a network failure."""

    status_code = 403


@dataclass(slots=True)
class Target:
    address: str
    port: int
    username: str
    host_key: str  # pinned, OpenSSH public key format
    private_key: str | None = None  # OpenSSH private key (unencrypted; protected by the vault at rest)
    password: str | None = None


@dataclass(slots=True)
class HostKey:
    algorithm: str
    fingerprint_sha256: str
    public_key: str


def describe_key(key: asyncssh.SSHKey) -> HostKey:
    return HostKey(
        algorithm=key.get_algorithm(),
        fingerprint_sha256=key.get_fingerprint("sha256"),
        public_key=key.export_public_key("openssh").decode().strip(),
    )


async def resolve(address: str) -> list[str]:
    try:
        return [str(ipaddress.ip_address(address))]
    except ValueError:
        pass
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(address, None, type=socket.SOCK_STREAM), 5)
    except (TimeoutError, OSError) as e:
        raise SSHError(f"{address} could not be resolved.", code="unreachable") from e
    return sorted({str(i[4][0]) for i in infos})


def check_allowed(addresses: list[str], allowed: list[str]) -> None:
    if not allowed:
        return
    nets = [ipaddress.ip_network(n) for n in allowed]
    for a in addresses:
        ip = ipaddress.ip_address(a)
        if not any(ip in n for n in nets):
            raise TargetNotAllowed(
                f"{a} is outside the networks Lanvex may connect to (LANVEX_SERVER_ALLOWED_NETWORKS).",
                code="not_allowed",
            )


def _common_options(timeout: float) -> dict:
    return {
        "connect_timeout": timeout,
        "agent_path": None,
        "config": None,
        "client_host_keys": [],
        "x509_trusted_certs": None,
        "server_host_key_algs": list(HOST_KEY_ALGS),
    }


async def scan_host_key(address: str, port: int, timeout: float) -> HostKey:
    """Fetch the server's host key without authenticating. Used once, for an administrator to verify."""
    try:
        key = await asyncio.wait_for(
            asyncssh.get_server_host_key(
                address, port, server_host_key_algs=list(HOST_KEY_ALGS), config=None
            ),
            timeout + 2,
        )
    except (TimeoutError, OSError, asyncssh.Error) as e:
        raise SSHError(
            f"No SSH server answered at {address}:{port} ({type(e).__name__}).", code="unreachable"
        ) from e
    if key is None:
        raise SSHError(f"{address}:{port} did not present a host key.", code="unreachable")
    return describe_key(key)


PENALIZED = (
    "The server refuses connections from Lanvex's address for now (\"Not allowed at this time\"): OpenSSH's "
    "protection against repeated failed sign-ins (PerSourcePenalties) has blocked it, and every new attempt "
    "extends the block. Fix the credentials, then wait for the block to end, or lift it on the server."
)


async def _penalized(t: Target, timeout: float) -> bool:
    """OpenSSH 9.8+ answers a penalized address with a line before its version string and closes the
    connection; SSH clients skip such lines, so the connection just seems lost. Look at the raw first line."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(t.address, t.port), timeout)
    except (TimeoutError, OSError):
        return False
    try:
        data = await asyncio.wait_for(reader.read(256), timeout)
    except (TimeoutError, OSError):
        return False
    finally:
        writer.close()
    return b"Not allowed at this time" in data


@asynccontextmanager
async def connect(t: Target, timeout: float) -> AsyncIterator[asyncssh.SSHClientConnection]:
    try:
        pinned = asyncssh.import_public_key(t.host_key)
    except (asyncssh.KeyImportError, ValueError) as e:
        raise SSHError("The pinned host key is not a valid SSH public key.", code="error") from e
    keys = []
    if t.private_key:
        try:
            keys = [asyncssh.import_private_key(t.private_key)]
        except (asyncssh.KeyImportError, ValueError) as e:
            raise SSHError("The stored private key could not be loaded.", code="error") from e
    options = _common_options(timeout) | {
        "known_hosts": ([pinned], [], []),
        "username": t.username,
        "client_keys": keys or None,
        "password": t.password,
        "preferred_auth": "publickey" if keys else "password",
        "login_timeout": timeout,
        "server_host_key_algs": [pinned.get_algorithm()]
        + [a for a in HOST_KEY_ALGS if a != pinned.get_algorithm()],
    }
    if not keys:
        options["client_keys"] = []
    try:
        conn = await asyncio.wait_for(asyncssh.connect(t.address, t.port, **options), timeout + 5)
    except asyncssh.HostKeyNotVerifiable as e:
        raise SSHError(
            "The server presented a different host key than the one pinned in Lanvex. This happens after a "
            "reinstall, or when something is intercepting the connection. Lanvex will not connect until an "
            "administrator verifies and pins the new key.",
            code="host_key_mismatch",
        ) from e
    except asyncssh.PermissionDenied as e:
        raise SSHError(
            f"The server rejected the credentials for user '{t.username}'.", code="auth_failed"
        ) from e
    except (TimeoutError, OSError) as e:
        if isinstance(e, ConnectionResetError | ConnectionAbortedError) and await _penalized(t, timeout):
            raise SSHError(PENALIZED, code="refused") from e
        raise SSHError(
            f"{t.address}:{t.port} could not be reached ({type(e).__name__}).", code="unreachable"
        ) from e
    except asyncssh.Error as e:
        if isinstance(e, asyncssh.ConnectionLost | asyncssh.DisconnectError) and await _penalized(t, timeout):
            raise SSHError(PENALIZED, code="refused") from e
        raise SSHError(f"SSH connection failed: {e.reason}", code="error") from e
    try:
        yield conn
    finally:
        conn.close()
        await conn.wait_closed()


async def run(
    conn: asyncssh.SSHClientConnection, cmd: Cmd, timeout: float, stdin: str | None = None
) -> CommandResult:
    line = COMMANDS[cmd]  # KeyError for anything not in the allow-list
    if stdin is not None and cmd not in STDIN_COMMANDS:
        raise ValueError(f"{cmd} does not take input")
    try:
        if stdin is None:
            r = await conn.run(line, check=False, timeout=timeout, stdin=asyncssh.DEVNULL)
        else:
            r = await conn.run(line, check=False, timeout=timeout, input=stdin)
    except (asyncssh.TimeoutError, TimeoutError):
        return CommandResult(stdout="", stderr=f"timed out after {timeout:g}s", exit_status=None)
    except asyncssh.ChannelOpenError as e:
        return CommandResult(stdout="", stderr=f"could not open a session: {e.reason}", exit_status=None)
    out = r.stdout if isinstance(r.stdout, str) else (r.stdout or b"").decode("utf-8", "replace")
    err = r.stderr if isinstance(r.stderr, str) else (r.stderr or b"").decode("utf-8", "replace")
    return CommandResult(stdout=out[:MAX_OUTPUT], stderr=err[:4096], exit_status=r.exit_status)


async def upload(conn: asyncssh.SSHClientConnection, filename: str, data: bytes, timeout: float) -> str:
    """Write a file into the SSH user's home directory over SFTP. Returns the remote path.

    Only used to hand the change helper to an administrator, who installs it with sudo after checking
    its checksum. `filename` is a constant chosen by Lanvex, never user input.
    """
    try:
        async with asyncio.timeout(timeout):
            async with conn.start_sftp_client() as sftp:
                home = await sftp.realpath(".")
                path = f"{home.rstrip('/')}/{filename}"
                async with sftp.open(path, "wb", attrs=asyncssh.SFTPAttrs(permissions=0o644)) as f:
                    await f.write(data)
                await sftp.chmod(path, 0o644)
                return path
    except (TimeoutError, asyncssh.Error, OSError) as e:
        raise SSHError(f"Could not copy {filename} to the server: {e}", code="error") from e
