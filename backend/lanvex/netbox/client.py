"""The only module in Lanvex that speaks HTTP to NetBox.

Responsibilities: authentication header, timeouts, retries for idempotent requests, pagination,
and translating NetBox failures into Lanvex errors. It returns plain dicts; domain services map
them into Lanvex models so the rest of the app never depends on raw NetBox payloads.
"""

import asyncio
import random
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from lanvex import __version__
from lanvex.core.errors import (
    Conflict,
    LanvexError,
    NotFound,
    PermissionDenied,
    UpstreamUnavailable,
    ValidationFailed,
)
from lanvex.core.logging import get_logger

log = get_logger(__name__)

MIN_SUPPORTED_VERSION = (4, 2)
RETRY_STATUSES = {502, 503, 504}
IDEMPOTENT = {"GET", "HEAD", "OPTIONS"}
MAX_PAGE_SIZE = 1000

Params = Mapping[str, str | int | bool | list[str] | list[int] | None]


@dataclass(slots=True)
class Page:
    count: int
    results: list[dict[str, Any]]


@dataclass(slots=True)
class NetBoxStatus:
    reachable: bool
    authenticated: bool
    version: str | None = None
    supported: bool | None = None
    message: str | None = None
    latency_ms: int | None = None


def auth_header(token: str) -> str:
    """NetBox v2 tokens are 'nbt_<key>.<secret>' and use Bearer; v1 tokens use the Token keyword."""
    return f"Bearer {token}" if token.startswith("nbt_") else f"Token {token}"


def parse_version(raw: str | None) -> tuple[int, int] | None:
    if not raw:
        return None
    try:
        major, minor = raw.split("-")[0].split(".")[:2]
        return int(major), int(minor)
    except ValueError:
        return None


def _clean_params(params: Params | None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for key, value in (params or {}).items():
        if value is None or value == "":
            continue
        values = value if isinstance(value, list) else [value]
        for v in values:
            out.append((key, str(v).lower() if isinstance(v, bool) else str(v)))
    return out


def _flatten_errors(payload: Any, prefix: str = "") -> dict[str, list[str]]:
    """NetBox/DRF validation errors are nested dicts/lists of strings; flatten to field -> messages."""
    fields: dict[str, list[str]] = {}
    if isinstance(payload, dict):
        for key, val in payload.items():
            name = "__all__" if key in ("non_field_errors", "__all__") else f"{prefix}{key}"
            for sub_key, msgs in _flatten_errors(val, f"{name}.").items():
                fields.setdefault(sub_key, []).extend(msgs)
            if isinstance(val, (str, int)):
                fields.setdefault(name, []).append(str(val))
            elif isinstance(val, list) and all(isinstance(v, str) for v in val):
                fields.setdefault(name, []).extend(val)
    elif isinstance(payload, list):
        for item in payload:
            if not isinstance(item, str):
                for k, v in _flatten_errors(item, prefix).items():
                    fields.setdefault(k, []).extend(v)
    return {k: v for k, v in fields.items() if v}


class NetBoxClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10.0,
        verify_tls: bool = True,
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
        name: str = "NetBox",
    ):
        self.base_url = base_url.rstrip("/")
        # What users call it in messages: "Lanvex Core" when Lanvex runs its own, otherwise "NetBox".
        self.name = name
        self.max_retries = max_retries
        self._http = httpx.AsyncClient(
            base_url=f"{self.base_url}/api/",
            headers={
                "Authorization": auth_header(token),
                "Accept": "application/json",
                "User-Agent": f"Lanvex/{__version__}",
            },
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            verify=verify_tls,
            transport=transport,
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- core request -------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Params | None = None,
        json: Any = None,
    ) -> Any:
        path = path.lstrip("/")
        attempts = 1 + (self.max_retries if method.upper() in IDEMPOTENT else 0)
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = await self._http.request(method, path, params=_clean_params(params), json=json)
            except (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.ReadTimeout,
                httpx.RemoteProtocolError,
            ) as e:
                last_exc = e
                log.warning(
                    "netbox_request_error", method=method, path=path, attempt=attempt, error=type(e).__name__
                )
                if attempt < attempts:
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                if isinstance(e, httpx.ReadTimeout) and method.upper() not in IDEMPOTENT:
                    raise UpstreamUnavailable(
                        f"{self.name} did not respond in time. The change may or may not have been applied — "
                        "refresh to check before retrying.",
                        code="netbox_timeout_unknown_outcome",
                    ) from e
                raise UpstreamUnavailable(
                    f"{self.name} could not be reached ({type(e).__name__}).", code="netbox_unreachable"
                ) from e
            except httpx.HTTPError as e:
                raise UpstreamUnavailable(f"{self.name} request failed ({type(e).__name__}).") from e

            if response.status_code in RETRY_STATUSES and attempt < attempts:
                log.warning(
                    "netbox_retryable_status", status=response.status_code, path=path, attempt=attempt
                )
                await asyncio.sleep(self._backoff(attempt))
                continue
            return self._handle(response, method, path)
        raise UpstreamUnavailable(f"{self.name} request failed.") from last_exc  # pragma: no cover

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(0.25 * (2 ** (attempt - 1)), 2.0) + random.uniform(0, 0.1)  # noqa: S311

    def _handle(self, response: httpx.Response, method: str, path: str) -> Any:
        status = response.status_code
        if 200 <= status < 300:
            if status == 204 or not response.content:
                return None
            try:
                return response.json()
            except ValueError as e:
                raise UpstreamUnavailable(f"{self.name} returned a response that is not valid JSON.") from e
        if 300 <= status < 400:
            raise UpstreamUnavailable(
                f"{self.name} redirected the request. Check LANVEX_NETBOX_URL (scheme, host, and path prefix).",
                code="netbox_redirect",
            )
        try:
            body = response.json()
        except ValueError:
            body = None
        detail = body.get("detail") if isinstance(body, dict) else None
        log.info("netbox_error_response", method=method, path=path, status=status)
        if status == 400:
            fields = _flatten_errors(body)
            message = "; ".join(
                f"{k}: {' '.join(v)}" if k != "__all__" else " ".join(v) for k, v in fields.items()
            )
            raise ValidationFailed(
                message or f"{self.name} rejected the request.", fields=fields, code="netbox_validation"
            )
        if status in (401, 403):
            raise PermissionDenied(
                f"{self.name} denied the request: {detail or 'insufficient permissions for the configured token'}.",
                code="netbox_permission_denied",
            )
        if status == 404:
            raise NotFound(detail or f"The object was not found in {self.name}.")
        if status == 409:
            raise Conflict(detail or f"{self.name} reported a conflict.")
        if status >= 500:
            raise UpstreamUnavailable(
                f"{self.name} returned an error (HTTP {status}).", code="netbox_server_error"
            )
        raise LanvexError(detail or f"Unexpected {self.name} response (HTTP {status}).", code="netbox_error")

    # ---- helpers ------------------------------------------------------

    async def get(self, path: str, params: Params | None = None) -> Any:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, json: Any) -> Any:
        return await self.request("POST", path, json=json)

    async def patch(self, path: str, json: Any) -> Any:
        return await self.request("PATCH", path, json=json)

    async def delete(self, path: str) -> None:
        await self.request("DELETE", path)

    async def page(
        self, path: str, params: Params | None = None, *, limit: int = 50, offset: int = 0
    ) -> Page:
        merged = dict(params or {})
        merged.update(limit=min(max(limit, 1), MAX_PAGE_SIZE), offset=max(offset, 0))
        data = await self.get(path, merged)
        return Page(count=int(data.get("count", 0)), results=list(data.get("results", [])))

    async def list_all(
        self, path: str, params: Params | None = None, *, cap: int = 5000
    ) -> list[dict[str, Any]]:
        """Follow pagination up to `cap` objects. Callers must choose a sensible cap."""
        results: list[dict[str, Any]] = []
        offset = 0
        while len(results) < cap:
            page = await self.page(path, params, limit=min(MAX_PAGE_SIZE, cap - len(results)), offset=offset)
            results.extend(page.results[: cap - len(results)])
            offset += len(page.results)
            if not page.results or offset >= page.count:
                break
        return results

    async def count(self, path: str, params: Params | None = None) -> int:
        merged = dict(params or {})
        merged.update(limit=1, brief=True)
        data = await self.get(path, merged)
        return int(data.get("count", 0))

    async def status(self) -> NetBoxStatus:
        """Check reachability, version, and that the token is accepted."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            data = await self.get("status/")
        except PermissionDenied as e:
            return NetBoxStatus(reachable=True, authenticated=False, message=e.message)
        except LanvexError as e:
            return NetBoxStatus(reachable=False, authenticated=False, message=e.message)
        latency = int((loop.time() - started) * 1000)
        version = str(data.get("netbox-version") or "") or None
        parsed = parse_version(version)
        supported = parsed is not None and parsed >= MIN_SUPPORTED_VERSION
        # /api/status/ can be readable anonymously (LOGIN_REQUIRED=False); confirm the token itself works.
        try:
            await self.get("authentication-check/")
            authenticated = True
            message = None
        except PermissionDenied as e:
            authenticated, message = False, e.message
        except LanvexError as e:
            authenticated, message = False, e.message
        if not supported:
            message = message or (
                f"NetBox {version} is not supported. Lanvex requires NetBox "
                f"{MIN_SUPPORTED_VERSION[0]}.{MIN_SUPPORTED_VERSION[1]} or newer."
            )
        return NetBoxStatus(
            reachable=True,
            authenticated=authenticated,
            version=version,
            supported=supported,
            message=message,
            latency_ms=latency,
        )
