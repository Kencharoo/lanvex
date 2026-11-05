import os

# Test configuration must be in place before lanvex modules read settings.
os.environ.setdefault(
    "LANVEX_TEST_DATABASE_URL", "postgresql+asyncpg://lanvex:lanvex-dev-only@localhost:5432/lanvex_test"
)
os.environ["LANVEX_DATABASE_URL"] = os.environ["LANVEX_TEST_DATABASE_URL"]
os.environ["LANVEX_ENV"] = "test"
os.environ["LANVEX_REDIS_URL"] = ""
os.environ["LANVEX_NETBOX_URL"] = ""
os.environ["LANVEX_NETBOX_TOKEN"] = ""
os.environ["LANVEX_LOG_LEVEL"] = "WARNING"

from collections.abc import AsyncIterator  # noqa: E402
from pathlib import Path  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
import respx  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import text  # noqa: E402

from lanvex.auth.models import Role  # noqa: E402
from lanvex.auth.service import create_user  # noqa: E402
from lanvex.core.config import get_settings  # noqa: E402
from lanvex.core.db import Database  # noqa: E402
from lanvex.core.ratelimit import MemoryRateLimiter  # noqa: E402
from lanvex.dashboard.router import clear_cache  # noqa: E402
from lanvex.main import create_app  # noqa: E402
from lanvex.netbox.client import NetBoxClient  # noqa: E402

get_settings.cache_clear()
NETBOX_API = "https://netbox.test/api/"
ADMIN_PW = "correct-horse-battery"
READER_PW = "reader-password-123"


def _alembic_config() -> Config:
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    return cfg


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    """Exercise the real migrations: downgrade to empty, then upgrade to head."""
    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
async def database() -> AsyncIterator[Database]:
    db = Database(os.environ["LANVEX_DATABASE_URL"])
    yield db
    await db.dispose()


@pytest.fixture(autouse=True)
async def clean_tables(database: Database) -> None:
    async with database.engine.begin() as conn:
        await conn.execute(text("TRUNCATE audit_events, sessions, users CASCADE"))
    clear_cache()


@pytest.fixture
def netbox_mock() -> respx.MockRouter:
    """TEST DOUBLE for NetBox. Every route a test needs must be declared explicitly."""
    return respx.MockRouter(base_url=NETBOX_API, assert_all_called=False, assert_all_mocked=True)


@pytest.fixture
async def app(database: Database, netbox_mock: respx.MockRouter):
    application = create_app(get_settings())
    application.state.db = database
    application.state.redis = None
    application.state.rate_limiter = MemoryRateLimiter()
    nb = NetBoxClient(
        "https://netbox.test",
        "nbt_testkey.testsecret",
        transport=httpx.MockTransport(netbox_mock.async_handler),
        max_retries=1,
    )
    application.state.netbox = nb
    yield application
    await nb.aclose()


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://lanvex.test") as c:
        yield c


@pytest.fixture
async def users(database: Database) -> dict[str, str]:
    async with database.sessionmaker() as s:
        await create_user(s, "admin", ADMIN_PW, Role.ADMIN)
        await create_user(s, "reader", READER_PW, Role.READONLY)
        await s.commit()
    return {"admin": ADMIN_PW, "reader": READER_PW}


async def login(client: httpx.AsyncClient, username: str, password: str) -> str:
    r = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    csrf = r.json()["csrf_token"]
    client.headers["X-CSRF-Token"] = csrf
    return csrf


@pytest.fixture
async def admin(client: httpx.AsyncClient, users: dict[str, str]) -> httpx.AsyncClient:
    await login(client, "admin", users["admin"])
    return client


@pytest.fixture
async def reader(client: httpx.AsyncClient, users: dict[str, str]) -> httpx.AsyncClient:
    await login(client, "reader", users["reader"])
    return client
