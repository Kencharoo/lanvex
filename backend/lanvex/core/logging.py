import logging
import re
import sys
from typing import Any

import structlog

_SENSITIVE_KEYS = re.compile(r"(pass(word)?|token|secret|authorization|cookie|csrf|api[_-]?key)", re.I)


def _redact(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Never let credentials reach the log stream, whatever a caller passes."""
    for key in list(event_dict):
        if key != "event" and _SENSITIVE_KEYS.search(key):
            event_dict[key] = "***"
    return event_dict


def configure_logging(level: str = "INFO", json: bool = False) -> None:
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer(colors=False),
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )
    # Quiet libraries that would otherwise log request URLs/headers at INFO.
    logging.basicConfig(level=level.upper(), stream=sys.stdout, format="%(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
