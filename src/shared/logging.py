"""Structured logging setup for marketplace-maps-parser.

Provides a single ``get_logger(name)`` factory that returns a configured
``loguru`` logger. All transports, adapters, and services should use this
instead of bare ``print()`` so that log levels and output destinations
can be reconfigured in one place.

Configuration via environment variables:

- ``MARKETPLACE_LOG_LEVEL`` — minimum level (default: ``INFO``)
- ``MARKETPLACE_LOG_FORMAT`` — ``"pretty"`` (default, colorized) or
  ``"json"`` (machine-readable, one log per line)
- ``MARKETPLACE_LOG_FILE`` — optional path; if set, logs are also
  written there in addition to stderr
"""
from __future__ import annotations

import os
import sys
from typing import Any

from loguru import logger as _loguru_logger

_CONFIGURED = False


def _configure() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    level = os.environ.get(
        "MARKETPLACE_LOG_LEVEL", "INFO"
    ).upper()
    fmt = os.environ.get(
        "MARKETPLACE_LOG_FORMAT", "pretty"
    ).lower()
    file_path = os.environ.get("MARKETPLACE_LOG_FILE")

    _loguru_logger.remove()

    if fmt == "json":
        # Single-line JSON for log aggregators.
        serializer = (
            lambda record: _serialize_record(record)
        )
        _loguru_logger.add(
            sys.stderr,
            level=level,
            serialize=True,
            backtrace=False,
            diagnose=False,
        )
    else:
        _loguru_logger.add(
            sys.stderr,
            level=level,
            colorize=True,
            backtrace=True,
            diagnose=False,
            format=(
                "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green>"
                " | <level>{level: <8}</level>"
                " | <cyan>{name}</cyan>:<cyan>{function}</cyan>"
                ":<cyan>{line}</cyan>"
                " - <level>{message}</level>"
            ),
        )

    if file_path:
        _loguru_logger.add(
            file_path,
            level=level,
            rotation="10 MB",
            retention="7 days",
            compression="gz",
            format=(
                "{time:YYYY-MM-DD HH:mm:ss.SSS}"
                " | {level: <8}"
                " | {name}:{function}:{line}"
                " - {message}"
            ),
        )

    _CONFIGURED = True


def _serialize_record(record: Any) -> str:
    """loguru serialize=True already produces JSON; this is a no-op hook
    kept for future custom field enrichment."""
    return record["message"]


def get_logger(name: str | None = None) -> Any:
    """Return a loguru logger bound to ``name``.

    The first call configures the global sink. Subsequent calls just
    return a bound logger, so it's cheap to call ``get_logger(__name__)``
    at the top of every module.
    """
    _configure()
    if name:
        return _loguru_logger.bind(component=name)
    return _loguru_logger
