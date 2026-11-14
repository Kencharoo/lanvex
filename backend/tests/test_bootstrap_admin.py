"""The first administrator from LANVEX_BOOTSTRAP_ADMIN_*: created once, however many workers start at once."""

import asyncio

from pydantic import SecretStr
from sqlalchemy import func, select

from lanvex.auth.models import User
from lanvex.core.config import get_settings
from lanvex.core.db import Database
from lanvex.main import bootstrap_admin


async def test_workers_starting_together_create_the_admin_once(database: Database) -> None:
    settings = get_settings().model_copy(
        update={
            "bootstrap_admin_username": "first-admin",
            "bootstrap_admin_password": SecretStr("first-admin-password"),
        }
    )
    # uvicorn --workers 2 runs the startup in each worker at the same moment.
    await asyncio.gather(*(bootstrap_admin(database, settings) for _ in range(4)))
    async with database.sessionmaker() as s:
        assert await s.scalar(select(func.count()).select_from(User)) == 1
        assert await s.scalar(select(User.username)) == "first-admin"


async def test_nothing_is_created_once_users_exist(database: Database, users: dict[str, str]) -> None:
    settings = get_settings().model_copy(
        update={
            "bootstrap_admin_username": "late-admin",
            "bootstrap_admin_password": SecretStr("late-admin-password"),
        }
    )
    await bootstrap_admin(database, settings)
    async with database.sessionmaker() as s:
        assert (
            await s.scalar(select(func.count()).select_from(User).where(User.username == "late-admin")) == 0
        )
