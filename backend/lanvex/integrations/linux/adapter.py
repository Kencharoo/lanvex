"""Linux server adapter (Ubuntu Server, Debian, Fedora Server).

Reads run as the SSH user with a fixed command set. The one change capability goes through the helper
(lanvex_netapply.py), which an administrator installs on the server.
"""

import asyncio
import time
from dataclasses import dataclass

from lanvex.integrations.base import Capability, register
from lanvex.integrations.linux import transport
from lanvex.integrations.linux.assemble import Results, assemble, detect_stack
from lanvex.integrations.linux.commands import (
    BASE_COMMANDS,
    LLDPD_COMMANDS,
    NETWORKD_COMMANDS,
    NETWORKMANAGER_COMMANDS,
    RESOLVED_COMMANDS,
    Cmd,
)
from lanvex.integrations.linux.facts import HostFacts

NAME = "linux-ssh"
CAPABILITIES = {
    Capability.READ_SYSTEM,
    Capability.READ_INTERFACES,
    Capability.READ_ADDRESSES,
    Capability.READ_ROUTES,
    Capability.READ_DNS,
    Capability.READ_NETWORK_STACK,
    Capability.READ_NEIGHBORS,
    Capability.CHANGE_INTERFACE_IPV4,
    Capability.CHANGE_NETWORKS,
}
register(NAME, CAPABILITIES)

CONCURRENCY = 4  # sessions per connection; sshd's default MaxSessions is 10


@dataclass(slots=True)
class Collection:
    facts: HostFacts
    raw: dict[str, dict[str, object]]
    duration_ms: int


async def _run_many(conn, cmds: tuple[Cmd, ...], timeout: float, results: Results) -> None:  # type: ignore[no-untyped-def]
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(c: Cmd) -> None:
        async with sem:
            results[c] = await transport.run(conn, c, timeout)

    await asyncio.gather(*(one(c) for c in cmds))


async def test_connection(target: transport.Target, timeout: float) -> str:
    """Log in and run one harmless command. Returns the remote hostname."""
    async with transport.connect(target, timeout) as conn:
        r = await transport.run(conn, Cmd.HOSTNAME, timeout)
        if not r.ok:
            raise transport.SSHError(
                f"Logged in, but a basic command failed: {r.stderr.strip()}", code="error"
            )
        return r.stdout.strip()


async def collect(target: transport.Target, connect_timeout: float, command_timeout: float) -> Collection:
    started = time.monotonic()
    results: Results = {}
    async with transport.connect(target, connect_timeout) as conn:
        await _run_many(conn, BASE_COMMANDS, command_timeout, results)
        # Only ask the tools of stacks that are actually running.
        units = detect_stack(results).units
        extra: tuple[Cmd, ...] = ()
        if units.get("NetworkManager") == "active":
            extra += NETWORKMANAGER_COMMANDS
        if units.get("systemd-networkd") == "active":
            extra += NETWORKD_COMMANDS
        if units.get("systemd-resolved") == "active":
            extra += RESOLVED_COMMANDS
        lldpd = results.get(Cmd.LLDPD_STATE)
        if lldpd is not None and lldpd.stdout.strip() == "active":
            extra += LLDPD_COMMANDS
        if extra:
            await _run_many(conn, extra, command_timeout, results)
    facts = assemble(results)
    raw = {
        c.value: {"exit_status": r.exit_status, "stdout": r.stdout[:65536], "stderr": r.stderr}
        for c, r in results.items()
    }
    return Collection(facts=facts, raw=raw, duration_ms=int((time.monotonic() - started) * 1000))
