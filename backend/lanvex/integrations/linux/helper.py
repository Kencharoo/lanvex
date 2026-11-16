"""Talking to the change helper (lanvex_netapply.py) on a Linux server.

The helper is the only thing Lanvex runs with root rights, and only after an administrator has
installed it and allowed it in sudoers. Requests go to it as JSON on stdin; it answers with one
JSON object on stdout.
"""

import hashlib
import json
import shlex
from functools import cache
from pathlib import Path
from typing import Any

import asyncssh

from lanvex.core.errors import LanvexError
from lanvex.integrations.linux import transport
from lanvex.integrations.linux.commands import HELPER_PATH, Cmd
from lanvex.integrations.linux.lanvex_netapply import HELPER_VERSION

HELPER_FILE = Path(__file__).with_name("lanvex_netapply.py")
UPLOAD_NAME = "lanvex-netapply"

# Generous: netplan apply and NetworkManager reactivation can take a while on a busy server.
TIMEOUTS = {
    Cmd.HELPER_VERSION: 30.0,
    Cmd.HELPER_PLAN: 90.0,
    Cmd.HELPER_APPLY: 240.0,
    Cmd.HELPER_VERIFY: 120.0,
    Cmd.HELPER_CONFIRM: 60.0,
    Cmd.HELPER_ROLLBACK: 240.0,
    Cmd.HELPER_STATUS: 30.0,
    Cmd.HELPER_INSPECT: 60.0,
}


class HelperUnavailable(LanvexError):
    """The helper could not be run: not installed, not allowed in sudoers, no Python, or no answer."""

    status_code, code = 502, "helper_unavailable"


class HelperRefused(LanvexError):
    """The helper ran and refused the request. `code` is the helper's own reason."""

    status_code, code = 422, "helper_refused"


@cache
def source() -> bytes:
    return HELPER_FILE.read_bytes()


@cache
def sha256() -> str:
    return hashlib.sha256(source()).hexdigest()


def install_commands(path: str, username: str) -> list[str]:
    """What an administrator runs on the server to install an uploaded (or downloaded) helper."""
    src = shlex.quote(path)
    return [
        f"echo {shlex.quote(sha256() + '  ' + path)} | sha256sum -c -",
        f"sudo install -D -m 0755 -o root -g root {src} {HELPER_PATH}",
        f"echo {shlex.quote(f'{username} ALL=(root) NOPASSWD: {HELPER_PATH}')} | sudo tee /etc/sudoers.d/lanvex >/dev/null",
        "sudo chmod 0440 /etc/sudoers.d/lanvex && sudo visudo -cf /etc/sudoers.d/lanvex",
    ]


def _unavailable(r: transport.CommandResult) -> HelperUnavailable:
    err = (r.stderr or r.stdout or "").strip()
    low = err.lower()
    if r.exit_status is None:
        return HelperUnavailable(f"The helper did not answer: {err or 'no response'}.", code="timeout")
    if "password is required" in low or "not allowed to" in low or "may not run sudo" in low:
        return HelperUnavailable(
            "sudo does not let this user run the helper without a password. Install the helper and add "
            "the sudoers rule from the install steps.",
            code="no_sudo",
        )
    if "bad interpreter" in low or "python3: no such file" in low or "unable to execute" in low:
        # sudo found the helper but could not start it: its interpreter, /usr/bin/python3, is missing
        return HelperUnavailable(
            "The helper is installed, but Python 3 (/usr/bin/python3) is not, so it cannot run.",
            code="no_python",
        )
    if "command not found" in low or "no such file" in low or "sudo: not found" in low:
        return HelperUnavailable(f"The helper is not installed at {HELPER_PATH}.", code="missing")
    return HelperUnavailable(
        f"The helper failed: {err[:300] or f'exit status {r.exit_status}'}", code="error"
    )


async def call(
    conn: asyncssh.SSHClientConnection, cmd: Cmd, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    r = await transport.run(
        conn, cmd, TIMEOUTS[cmd], stdin=json.dumps(payload) if payload is not None else None
    )
    data: Any = None
    lines = r.stdout.strip().splitlines()
    if lines:
        try:
            data = json.loads(lines[-1])
        except ValueError:
            data = None
    if not isinstance(data, dict):
        raise _unavailable(r)
    if not data.get("ok"):
        err = data.get("error") or {}
        raise HelperRefused(
            str(err.get("message") or "The helper refused the request."),
            code=str(err.get("code") or "refused"),
        )
    return data


def helper_version_state(data: dict[str, Any]) -> tuple[str, str]:
    """(state, message) for a successful `version` answer."""
    version = data.get("version")
    if version != HELPER_VERSION:
        return (
            "outdated",
            f"The server has helper version {version}; this Lanvex needs version {HELPER_VERSION}.",
        )
    if data.get("sha256") != sha256():
        return (
            "outdated",
            "The helper on the server is a different build from the one this Lanvex ships. Install it again.",
        )
    tools = data.get("tools") or {}
    if not tools.get("systemd_run"):
        return "error", "systemd-run is missing; the automatic rollback timer needs systemd."
    return "ok", f"Helper version {version} on Python {data.get('python')}."
