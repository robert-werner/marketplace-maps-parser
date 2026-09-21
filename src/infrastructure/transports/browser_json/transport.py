"""Backward-compatible exports for the former single-file transport."""
from infrastructure.transports.browser_common import _STEALTH_INIT_SCRIPT
from infrastructure.transports.browser_json import BrowserJsonTransport
from infrastructure.transports.browser_json._errors import (
    CloudflareChallengeError,
)

__all__ = (
    "BrowserJsonTransport",
    "CloudflareChallengeError",
    "_STEALTH_INIT_SCRIPT",
)
