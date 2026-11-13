"""
Lanvex Core under /core/: a reverse proxy that signs Lanvex users in.

Lanvex Core (the NetBox-based application bundled with Lanvex) is not reachable on its own. Every
request to /core/... needs a valid Lanvex session; Lanvex then forwards it with the user's identity
and role in X-Lanvex-* headers and the proxy secret shared with Lanvex Core, which only trusts
identities that arrive with that secret (lanvex_core/auth.py in Lanvex Core).

What never reaches Lanvex Core: the Lanvex session cookie, any X-Lanvex-* or Authorization header
the browser sends (so nobody can claim another identity or bypass the Lanvex session with a token),
and hop-by-hop headers.
"""

from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from lanvex.auth.models import Role, User
from lanvex.auth.service import resolve_session
from lanvex.core.config import Settings, get_settings
from lanvex.core.http import client_ip, request_is_https
from lanvex.core.logging import get_logger

log = get_logger(__name__)

PREFIX = "/core"
GROUPS = {Role.ADMIN: "lanvex-admin", Role.READONLY: "lanvex-readonly"}
METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]

# RFC 9110 section 7.6.1, plus headers whose value would be wrong after forwarding.
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}
NEVER_FORWARD = {
    "authorization",
    "cookie",
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
}


def install(app: FastAPI) -> None:
    """Route /core and /core/... to Lanvex Core. Must run before the SPA's catch-all route."""

    @app.api_route(PREFIX, methods=METHODS, include_in_schema=False)
    async def core_root() -> Response:
        return RedirectResponse(f"{PREFIX}/", status_code=308)

    @app.api_route(PREFIX + "/{path:path}", methods=METHODS, include_in_schema=False)
    async def core(request: Request, path: str) -> Response:
        settings = get_settings()
        if not settings.core_enabled:
            return PlainTextResponse("Lanvex Core is not part of this installation.", status_code=404)
        if path.startswith("static/") and settings.core_static_dir and request.method in ("GET", "HEAD"):
            return _static(settings.core_static_dir, path.removeprefix("static/"))

        user = await _signed_in_user(request, settings)
        if user is None:
            return _sign_in_first(request)
        if _header_value(user.username) is None:
            return PlainTextResponse("This username cannot be used with Lanvex Core.", status_code=403)
        if path == "logout/":
            # Signing out of Lanvex Core alone would sign the user straight back in; sign out of Lanvex.
            return RedirectResponse("/signout", status_code=303)
        return await _forward(request, settings, user)


async def _signed_in_user(request: Request, settings: Settings) -> User | None:
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        return None
    async with request.app.state.db.sessionmaker() as db:
        session = await resolve_session(db, token, settings)
        return session.user if session is not None else None


def _sign_in_first(request: Request) -> Response:
    if request.method in ("GET", "HEAD") and "text/html" in request.headers.get("accept", ""):
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
    return JSONResponse(
        {"error": {"code": "not_authenticated", "message": "Sign in to Lanvex to use Lanvex Core."}},
        status_code=401,
    )


def _static(root: str, relative: str) -> Response:
    base = Path(root).resolve()
    candidate = (base / relative).resolve()
    if base not in candidate.parents or not candidate.is_file():
        return PlainTextResponse("Not found.", status_code=404)
    return FileResponse(candidate, headers={"Cache-Control": "public, max-age=3600"})


def _cookie_header(request: Request, settings: Settings) -> str | None:
    """The browser's cookies minus Lanvex's own session cookie."""
    kept = [f"{k}={v}" for k, v in request.cookies.items() if k != settings.session_cookie_name]
    return "; ".join(kept) or None


def _header_value(value: str | None) -> bytes | None:
    """
    A header value as Latin-1 bytes, which is how Django reads headers ("Jens Müller" arrives intact).
    Values that cannot travel as Latin-1, or contain control characters, are left out rather than mangled.
    """
    if not value or not value.isprintable():
        return None
    try:
        return value.encode("latin-1")
    except UnicodeEncodeError:
        return None


def outgoing_headers(request: Request, settings: Settings, user: User) -> list[tuple[str, str | bytes]]:
    headers: list[tuple[str, str | bytes]] = [
        (k, v)
        for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP
        and k.lower() not in NEVER_FORWARD
        and not k.lower().startswith("x-lanvex-")
    ]
    cookies = _cookie_header(request, settings)
    if cookies:
        headers.append(("cookie", cookies))
    headers += [
        ("x-forwarded-host", request.headers.get("host", "")),
        ("x-forwarded-proto", "https" if request_is_https(request) else "http"),
        ("x-forwarded-for", client_ip(request)),
        ("x-lanvex-user", _header_value(user.username) or b""),
        ("x-lanvex-groups", GROUPS[user.role]),
        ("x-lanvex-proxy-secret", settings.core_proxy_secret.get_secret_value()),  # type: ignore[union-attr]
    ]
    display_name = _header_value(user.display_name)
    if display_name:
        headers.append(("x-lanvex-display-name", display_name))
    return headers


def _client(request: Request) -> httpx.AsyncClient:
    client: httpx.AsyncClient | None = getattr(request.app.state, "core_client", None)
    if client is None:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=300.0, write=300.0, pool=10.0), follow_redirects=False
        )
        request.app.state.core_client = client
    return client


async def _forward(request: Request, settings: Settings, user: User) -> Response:
    upstream = str(settings.core_upstream_url).rstrip("/") + request.url.path
    if request.url.query:
        upstream += "?" + request.url.query
    client = _client(request)
    # Bodies are sent whole: Django reads a request body only when Content-Length says how long it is.
    body = await request.body() if request.method not in ("GET", "HEAD") else None
    outgoing = client.build_request(
        request.method, upstream, headers=outgoing_headers(request, settings, user), content=body
    )
    try:
        response = await client.send(outgoing, stream=True)
    except httpx.HTTPError as exc:
        log.warning("core_unreachable", error=type(exc).__name__, path=request.url.path)
        return PlainTextResponse(
            "Lanvex Core is not answering. It may still be starting; try again in a minute.", status_code=502
        )
    headers = [(k, v) for k, v in response.headers.multi_items() if k.lower() not in HOP_BY_HOP]
    out = StreamingResponse(
        response.aiter_raw(), status_code=response.status_code, background=BackgroundTask(response.aclose)
    )
    for k, v in headers:
        out.headers.append(k, v)
    if "content-length" in response.headers:
        out.headers["content-length"] = response.headers["content-length"]
    return out


async def aclose(app: FastAPI) -> None:
    client: httpx.AsyncClient | None = getattr(app.state, "core_client", None)
    if client is not None:
        await client.aclose()
        app.state.core_client = None
