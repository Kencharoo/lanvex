"""Operator commands: `lanvex migrate`, `lanvex create-user`, `lanvex check-netbox`."""

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

from lanvex.auth.models import Role
from lanvex.core.config import get_settings
from lanvex.core.db import Database
from lanvex.core.errors import LanvexError


def migrate() -> int:
    from alembic import command
    from alembic.config import Config

    ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(ini.parent / "alembic"))
    command.upgrade(cfg, "head")
    print("Database is up to date.")
    return 0


async def _create_user(username: str, role: Role, password: str) -> None:
    from lanvex.auth.service import create_user

    db = Database(get_settings().database_url.get_secret_value())
    try:
        async with db.sessionmaker() as session:
            await create_user(session, username, password, role)
            await session.commit()
    finally:
        await db.dispose()


async def _check_netbox() -> int:
    from lanvex.main import build_netbox

    client = build_netbox(get_settings())
    if client is None:
        print("NetBox is not configured (LANVEX_NETBOX_URL / LANVEX_NETBOX_TOKEN).", file=sys.stderr)
        return 2
    try:
        s = await client.status()
    finally:
        await client.aclose()
    print(f"URL:           {client.base_url}")
    print(f"Reachable:     {s.reachable}")
    print(f"Authenticated: {s.authenticated}")
    print(f"Version:       {s.version or '-'} (supported: {s.supported})")
    if s.latency_ms is not None:
        print(f"Latency:       {s.latency_ms} ms")
    if s.message:
        print(f"Message:       {s.message}")
    return 0 if (s.reachable and s.authenticated and s.supported) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lanvex")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="Apply database migrations")
    cu = sub.add_parser("create-user", help="Create a Lanvex user")
    cu.add_argument("username")
    cu.add_argument("--role", choices=[r.value for r in Role], default=Role.READONLY.value)
    cu.add_argument("--password-stdin", action="store_true", help="Read the password from stdin")
    sub.add_parser("check-netbox", help="Test the NetBox connection")
    args = parser.parse_args(argv)

    if args.cmd == "migrate":
        return migrate()
    if args.cmd == "check-netbox":
        return asyncio.run(_check_netbox())
    if args.cmd == "create-user":
        if args.password_stdin:
            password = sys.stdin.readline().rstrip("\n")
        else:
            password = getpass.getpass("Password: ")
            if getpass.getpass("Repeat password: ") != password:
                print("Passwords do not match.", file=sys.stderr)
                return 1
        try:
            asyncio.run(_create_user(args.username, Role(args.role), password))
        except LanvexError as e:
            print(f"Error: {e.message}", file=sys.stderr)
            return 1
        print(f"Created {args.role} user '{args.username}'.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
