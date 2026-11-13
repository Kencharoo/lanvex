"""The /core/ proxy to Lanvex Core: who gets through, and what Lanvex Core is told about them."""

from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from lanvex.auth.models import Role
from lanvex.auth.service import create_user
from lanvex.core.config import Settings, get_settings
from lanvex.core.db import Database

SECRET = "proxy-secret-for-tests-0123456789abcdef"


class NetworkBody(httpx.AsyncByteStream):
    """A response body that arrives like one from the network (httpx pre-reads in-memory bodies)."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    async def __aiter__(self):  # type: ignore[override]
        yield self.data


def respond(status: int, headers: list[tuple[str, str]] | None = None, body: bytes = b"") -> httpx.Response:
    headers = [*(headers or []), ("Content-Length", str(len(body)))]
    return httpx.Response(status, headers=headers, stream=NetworkBody(body))


class FakeCore:
    """Lanvex Core as seen from the proxy: records each request, answers with a scripted response."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.response = lambda: respond(
            200, [("Content-Type", "text/html")], b"<title>Home | Lanvex Core</title>"
        )
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused", request=request)
        request.read()
        self.requests.append(request)
        return self.response()

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


@pytest.fixture
def core_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    s = get_settings()
    static = tmp_path / "static"
    (static / "lanvex_core").mkdir(parents=True)
    (static / "lanvex_core" / "lanvex-core.css").write_text("body{}")
    (tmp_path / "secret.txt").write_text("not for the browser")
    monkeypatch.setattr(s, "core_upstream_url", "http://core.test:8080")
    monkeypatch.setattr(s, "core_proxy_secret", SecretStr(SECRET))
    monkeypatch.setattr(s, "core_static_dir", str(static))
    return s


@pytest.fixture
async def core(app, core_settings: Settings):
    fake = FakeCore()
    app.state.core_client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    yield fake
    await app.state.core_client.aclose()


async def test_off_unless_configured(client: httpx.AsyncClient, users: dict[str, str]) -> None:
    r = await client.get("/core/")
    assert r.status_code == 404 and "not part of this installation" in r.text


async def test_signed_out_browser_is_sent_to_sign_in_and_api_gets_401(
    client: httpx.AsyncClient, core: FakeCore
) -> None:
    r = await client.get("/core/ipam/prefixes/?q=10.", headers={"Accept": "text/html"})
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2Fcore%2Fipam%2Fprefixes%2F%3Fq%3D10."
    r = await client.get("/core/api/ipam/prefixes/", headers={"Authorization": "Token stolen"})
    assert r.status_code == 401
    assert core.requests == []


async def test_signed_in_admin_is_forwarded_as_themselves(admin: httpx.AsyncClient, core: FakeCore) -> None:
    admin.cookies.set("lanvex_core_session", "abc")
    admin.cookies.set("lanvex_core_csrftoken", "def")
    r = await admin.get(
        "/core/ipam/prefixes/?q=10.",
        headers={
            "Accept": "text/html",
            "X-Lanvex-User": "someone-else",
            "X-Lanvex-Groups": "lanvex-admin",
            "X-Lanvex-Proxy-Secret": "guessed",
            "Authorization": "Bearer nbt_stolen.token",
            "X-Forwarded-Host": "evil.example",
        },
    )
    assert r.status_code == 200 and "Lanvex Core" in r.text
    sent = core.last
    assert str(sent.url) == "http://core.test:8080/core/ipam/prefixes/?q=10."
    assert sent.headers.get_list("x-lanvex-user") == ["admin"]
    assert sent.headers.get_list("x-lanvex-groups") == ["lanvex-admin"]
    assert sent.headers.get_list("x-lanvex-proxy-secret") == [SECRET]
    assert "authorization" not in sent.headers
    assert sent.headers["x-forwarded-host"] == "lanvex.test"
    assert sent.headers["x-forwarded-proto"] == "http"
    cookie = sent.headers["cookie"]
    assert "lanvex_core_session=abc" in cookie and "lanvex_core_csrftoken=def" in cookie
    assert get_settings().session_cookie_name not in cookie  # Lanvex's own session never leaves Lanvex


async def test_read_only_users_arrive_in_the_read_only_group(
    reader: httpx.AsyncClient, core: FakeCore
) -> None:
    await reader.get("/core/")
    assert core.last.headers["x-lanvex-groups"] == "lanvex-readonly"


async def test_display_name_is_sent_only_when_it_fits_a_header(
    client: httpx.AsyncClient, database: Database, core: FakeCore
) -> None:
    from tests.conftest import login

    async with database.sessionmaker() as s:
        await create_user(s, "jens", "jens-password-123", Role.READONLY, display_name="Jens Müller")
        await create_user(s, "li", "li-password-1234", Role.READONLY, display_name="李雷")
        await s.commit()
    await login(client, "jens", "jens-password-123")
    await client.get("/core/")
    assert core.last.headers.raw[
        [k.lower() for k, _ in core.last.headers.raw].index(b"x-lanvex-display-name")
    ][1] == ("Jens Müller".encode("latin-1"))
    client.cookies.clear()
    await login(client, "li", "li-password-1234")
    await client.get("/core/")
    assert "x-lanvex-display-name" not in core.last.headers
    assert core.last.headers["x-lanvex-user"] == "li"


async def test_form_posts_arrive_with_their_body_and_length(admin: httpx.AsyncClient, core: FakeCore) -> None:
    core.response = lambda: respond(302, [("Location", "/core/ipam/prefixes/1/")])
    r = await admin.post(
        "/core/ipam/prefixes/add/",
        content=b"prefix=10.9.0.0%2F24&csrfmiddlewaretoken=x",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 302 and r.headers["location"] == "/core/ipam/prefixes/1/"
    assert core.last.content == b"prefix=10.9.0.0%2F24&csrfmiddlewaretoken=x"
    assert core.last.headers["content-length"] == str(len(core.last.content))


async def test_responses_keep_every_cookie_and_header(admin: httpx.AsyncClient, core: FakeCore) -> None:
    core.response = lambda: respond(
        200,
        [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Set-Cookie", "lanvex_core_csrftoken=t1; Path=/core"),
            ("Set-Cookie", "lanvex_core_session=s1; Path=/core; HttpOnly"),
            ("X-Frame-Options", "SAMEORIGIN"),
        ],
        b"ok",
    )
    r = await admin.get("/core/")
    assert r.headers.get_list("set-cookie") == [
        "lanvex_core_csrftoken=t1; Path=/core",
        "lanvex_core_session=s1; Path=/core; HttpOnly",
    ]
    assert r.headers["x-frame-options"] == "SAMEORIGIN"
    assert r.headers["content-length"] == "2"


async def test_log_out_in_lanvex_core_means_signing_out_of_lanvex(
    admin: httpx.AsyncClient, core: FakeCore
) -> None:
    r = await admin.get("/core/logout/")
    assert r.status_code == 303 and r.headers["location"] == "/signout"
    assert core.requests == []


async def test_static_files_are_served_from_the_shared_folder_only(
    client: httpx.AsyncClient, core: FakeCore, core_settings: Settings
) -> None:
    r = await client.get("/core/static/lanvex_core/lanvex-core.css")
    assert r.status_code == 200 and r.text == "body{}"
    assert core.requests == []
    # Clients normalise "..", so ask the file lookup directly for what a crafted request could reach.
    from lanvex.core_proxy import _static

    assert _static(core_settings.core_static_dir, "../secret.txt").status_code == 404  # type: ignore[arg-type]
    assert _static(core_settings.core_static_dir, "lanvex_core/../../secret.txt").status_code == 404  # type: ignore[arg-type]
    assert _static(core_settings.core_static_dir, "/etc/passwd").status_code == 404  # type: ignore[arg-type]


async def test_core_not_answering_is_explained(admin: httpx.AsyncClient, core: FakeCore) -> None:
    core.down = True
    r = await admin.get("/core/")
    assert r.status_code == 502 and "not answering" in r.text


async def test_bare_core_path_gets_its_slash(admin: httpx.AsyncClient, core: FakeCore) -> None:
    r = await admin.get("/core")
    assert r.status_code == 308 and r.headers["location"] == "/core/"


async def test_system_info_names_lanvex_core(admin: httpx.AsyncClient, core: FakeCore) -> None:
    info = (await admin.get("/api/v1/system/info")).json()
    assert info["core_enabled"] is True and info["netbox_label"] == "Lanvex Core"


def test_netbox_links_are_rewritten_for_the_browser() -> None:
    s = Settings(netbox_url="http://core:8080/core", netbox_public_url="/core/")
    assert s.public_netbox_url("http://core:8080/core/ipam/prefixes/1/") == "/core/ipam/prefixes/1/"
    assert s.public_netbox_url("https://elsewhere.example/x/") == "https://elsewhere.example/x/"
    assert s.public_netbox_url(None) is None
    assert Settings(netbox_url="https://netbox.example").public_netbox_url("https://netbox.example/a/") == (
        "https://netbox.example/a/"
    )


def test_a_short_proxy_secret_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 32"):
        Settings(core_proxy_secret="short")
