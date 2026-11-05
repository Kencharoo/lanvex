"""NetBox client behaviour against a TEST DOUBLE (respx). Covers error mapping, retries, pagination."""

import httpx
import pytest
import respx

from lanvex.core.errors import NotFound, PermissionDenied, UpstreamUnavailable, ValidationFailed
from lanvex.netbox.client import NetBoxClient, auth_header, parse_version

from .netbox_payloads import page, prefix

API = "https://netbox.test/api/"


def make(router: respx.MockRouter, retries: int = 2) -> NetBoxClient:
    return NetBoxClient(
        "https://netbox.test/",
        "abc123",
        transport=httpx.MockTransport(router.async_handler),
        max_retries=retries,
    )


@pytest.fixture
def router() -> respx.MockRouter:
    return respx.MockRouter(base_url=API, assert_all_called=False)


def test_auth_header_versions() -> None:
    assert auth_header("nbt_key123.secret") == "Bearer nbt_key123.secret"
    assert auth_header("0123456789abcdef") == "Token 0123456789abcdef"


def test_parse_version() -> None:
    assert parse_version("4.7.4") == (4, 7)
    assert parse_version("4.2.0-beta1") == (4, 2)
    assert parse_version("garbage") is None
    assert parse_version(None) is None


async def test_sends_auth_and_params(router: respx.MockRouter) -> None:
    route = router.get("ipam/prefixes/").respond(200, json=page([]))
    nb = make(router)
    await nb.page("ipam/prefixes/", {"vrf_id": "null", "status": ["active", "reserved"], "q": None}, limit=10)
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Token abc123"
    assert req.url.params.get_list("status") == ["active", "reserved"]
    assert req.url.params["vrf_id"] == "null"
    assert "q" not in req.url.params  # empty values are not sent
    assert req.url.params["limit"] == "10"


async def test_retries_idempotent_on_503(router: respx.MockRouter) -> None:
    route = router.get("status/").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"netbox-version": "4.7.4"})]
    )
    data = await make(router).get("status/")
    assert data["netbox-version"] == "4.7.4"
    assert route.call_count == 2


async def test_does_not_retry_post(router: respx.MockRouter) -> None:
    route = router.post("ipam/prefixes/").respond(503)
    with pytest.raises(UpstreamUnavailable):
        await make(router).post("ipam/prefixes/", {"prefix": "10.0.0.0/24"})
    assert route.call_count == 1


async def test_connect_error_becomes_unavailable(router: respx.MockRouter) -> None:
    router.get("status/").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(UpstreamUnavailable) as e:
        await make(router, retries=0).get("status/")
    assert e.value.code == "netbox_unreachable"


async def test_post_timeout_reports_unknown_outcome(router: respx.MockRouter) -> None:
    router.post("ipam/prefixes/").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(UpstreamUnavailable) as e:
        await make(router).post("ipam/prefixes/", {})
    assert e.value.code == "netbox_timeout_unknown_outcome"


async def test_validation_errors_are_flattened(router: respx.MockRouter) -> None:
    router.post("ipam/prefixes/").respond(
        400,
        json={"prefix": ["Duplicate prefix found in global table: 10.0.0.0/24"], "__all__": ["Bad combo"]},
    )
    with pytest.raises(ValidationFailed) as e:
        await make(router).post("ipam/prefixes/", {})
    assert e.value.fields == {
        "prefix": ["Duplicate prefix found in global table: 10.0.0.0/24"],
        "__all__": ["Bad combo"],
    }


@pytest.mark.parametrize("status,exc", [(401, PermissionDenied), (403, PermissionDenied), (404, NotFound)])
async def test_status_mapping(router: respx.MockRouter, status: int, exc: type[Exception]) -> None:
    router.get("ipam/prefixes/1/").respond(status, json={"detail": "nope"})
    with pytest.raises(exc):
        await make(router).get("ipam/prefixes/1/")


async def test_redirect_is_an_error(router: respx.MockRouter) -> None:
    router.get("status/").respond(301, headers={"Location": "https://elsewhere/"})
    with pytest.raises(UpstreamUnavailable) as e:
        await make(router).get("status/")
    assert e.value.code == "netbox_redirect"


async def test_list_all_follows_pagination(router: respx.MockRouter) -> None:
    items = [prefix(i, f"10.{i}.0.0/16") for i in range(1, 6)]

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        return httpx.Response(200, json=page(items[offset : offset + 2], count=len(items)))

    route = router.get("ipam/prefixes/").mock(side_effect=handler)
    nb = make(router)
    # The server returns 2 per page regardless of `limit` (like NetBox's MAX_PAGE_SIZE); walk them all.
    got = await nb.list_all("ipam/prefixes/")
    assert [p["id"] for p in got] == [1, 2, 3, 4, 5]
    assert route.call_count == 3
    # A cap is never exceeded, even if the server over-delivers.
    assert len(await nb.list_all("ipam/prefixes/", cap=3)) == 3


async def test_status_ok(router: respx.MockRouter) -> None:
    router.get("status/").respond(200, json={"netbox-version": "4.7.4"})
    router.get("authentication-check/").respond(200, json={"id": 1, "username": "lanvex"})
    s = await make(router).status()
    assert s.reachable and s.authenticated and s.supported and s.version == "4.7.4"


async def test_status_bad_token(router: respx.MockRouter) -> None:
    router.get("status/").respond(200, json={"netbox-version": "4.7.4"})
    router.get("authentication-check/").respond(403, json={"detail": "Invalid token"})
    s = await make(router).status()
    assert s.reachable and not s.authenticated
    assert "Invalid token" in (s.message or "")


async def test_status_unsupported_version(router: respx.MockRouter) -> None:
    router.get("status/").respond(200, json={"netbox-version": "3.7.8"})
    router.get("authentication-check/").respond(200, json={"id": 1})
    s = await make(router).status()
    assert s.supported is False and "not supported" in (s.message or "")


async def test_status_unreachable(router: respx.MockRouter) -> None:
    router.get("status/").mock(side_effect=httpx.ConnectError("refused"))
    s = await make(router, retries=0).status()
    assert not s.reachable and not s.authenticated
