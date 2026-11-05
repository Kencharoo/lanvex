"""Application error types and the single place they become HTTP responses.

Every error response has the shape:
    {"error": {"code": str, "message": str, "fields": {field: [msg, ...]} | null, "request_id": str}}
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from lanvex.core.logging import get_logger

log = get_logger(__name__)


class LanvexError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, fields: dict[str, list[str]] | None = None, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.fields = fields
        if code:
            self.code = code


class NotAuthenticated(LanvexError):
    status_code, code = 401, "not_authenticated"


class PermissionDenied(LanvexError):
    status_code, code = 403, "permission_denied"


class NotFound(LanvexError):
    status_code, code = 404, "not_found"


class Conflict(LanvexError):
    status_code, code = 409, "conflict"


class ValidationFailed(LanvexError):
    status_code, code = 422, "validation_failed"


class RateLimited(LanvexError):
    status_code, code = 429, "rate_limited"


class UpstreamUnavailable(LanvexError):
    """NetBox (or another integration) could not be reached or failed."""

    status_code, code = 502, "upstream_unavailable"


class NotConfigured(LanvexError):
    status_code, code = 503, "not_configured"


def _body(request: Request, code: str, message: str, fields: dict[str, list[str]] | None) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "fields": fields,
            "request_id": getattr(request.state, "request_id", None),
        }
    }


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(LanvexError)
    async def _lanvex(request: Request, exc: LanvexError) -> JSONResponse:
        if exc.status_code >= 500:
            log.warning("request_failed", code=exc.code, message=exc.message, path=request.url.path)
        return JSONResponse(_body(request, exc.code, exc.message, exc.fields), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields: dict[str, list[str]] = {}
        for err in exc.errors():
            loc = [str(p) for p in err.get("loc", []) if p not in ("body", "query", "path")]
            fields.setdefault(".".join(loc) or "__all__", []).append(err.get("msg", "Invalid value"))
        return JSONResponse(
            _body(request, "validation_failed", "The request contains invalid values.", fields),
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return JSONResponse(_body(request, code, str(exc.detail), None), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error", path=request.url.path)
        return JSONResponse(
            _body(request, "internal_error", "An unexpected error occurred.", None), status_code=500
        )
