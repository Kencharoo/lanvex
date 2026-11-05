import ipaddress
import secrets

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from structlog.contextvars import bind_contextvars, clear_contextvars

from lanvex.core.config import get_settings


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id, binds it to logs, and sets baseline security headers."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if incoming.isalnum() and len(incoming) <= 64 else secrets.token_hex(8)
        request.state.request_id = request_id
        clear_contextvars()
        bind_contextvars(request_id=request_id)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


def _from_trusted_proxy(request: Request) -> bool:
    trusted = get_settings().trusted_proxies
    peer = request.client.host if request.client else ""
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(peer_ip in ipaddress.ip_network(net, strict=False) for net in trusted)


def request_is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    return _from_trusted_proxy(request) and request.headers.get("x-forwarded-proto", "").lower() == "https"


def is_loopback_host(request: Request) -> bool:
    host = (request.url.hostname or "").strip("[]")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def client_ip(request: Request) -> str:
    """Client address, honouring X-Forwarded-For only when the direct peer is a trusted proxy."""
    peer = request.client.host if request.client else "unknown"
    trusted = get_settings().trusted_proxies
    if not trusted:
        return peer
    try:
        peer_ip = ipaddress.ip_address(peer)
        if not any(peer_ip in ipaddress.ip_network(net, strict=False) for net in trusted):
            return peer
    except ValueError:
        return peer
    forwarded = request.headers.get("x-forwarded-for", "")
    first = forwarded.split(",")[0].strip()
    try:
        return str(ipaddress.ip_address(first)) if first else peer
    except ValueError:
        return peer
