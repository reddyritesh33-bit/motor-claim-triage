"""Structured JSON logging with a correlation id bound to every line (D5)."""
from __future__ import annotations

import logging
import sys

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars, merge_contextvars

_configured = False


def configure_logging(service: str, level: str = "INFO") -> None:
    global _configured
    if _configured:
        return
    logging.basicConfig(stream=sys.stdout, level=level.upper(), format="%(message)s")
    structlog.configure(
        processors=[
            merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    bind_contextvars(service=service)
    _configured = True


def get_logger(name: str | None = None):
    return structlog.get_logger(name)


def bind(**kwargs) -> None:
    bind_contextvars(**kwargs)


def reset_context(service: str) -> None:
    clear_contextvars()
    bind_contextvars(service=service)
