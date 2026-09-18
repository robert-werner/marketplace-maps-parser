"""Constants and helper functions for Yandex Market transport."""
from __future__ import annotations
import re
from typing import Any


YANDEX_MARKET_BASE = "https://market.yandex.ru"

_CAPTCHA_URL_MARKERS = (
    "showcaptcha",
    "checkcaptcha",
    "cloud-captcha",
    "smartcaptcha",
)

_CAPTCHA_HTML_MARKERS = (
    "Подтвердите, что запросы отправляли вы",
    "Докажите, что вы не робот",
    "Доступ к ресурсу ограничен",
    "Доступ ограничен",
    "smartcaptcha.yandex",
    "SmartCaptcha",
)

_CAPTCHA_SHELL_HTML_MARKERS = (
    "Вы не робот?",
    "captcha_smart",
    'action="/checkcaptcha',
)

_NOT_FOUND_HTML_MARKERS = (
    "Нет такой страницы",
    '"statusCode":404',
)

_SHELL_BODY_MAX_LEN = 700_000

_PAGE_HEALTHY = "healthy"
_PAGE_CAPTCHA = "captcha"
_PAGE_DEGRADED = "degraded"

_INT_RE = re.compile(r"\d+")
_FLOAT_RE = re.compile(r"\d+(?:[.,]\d+)?")


def _parse_int(value: Any) -> int | None:
    """Extract first integer from value string representation."""
    digits = _INT_RE.findall(str(value))
    return int(digits[0]) if digits else None


def _parse_float(value: Any) -> float | None:
    """Extract first float from value string representation."""
    match = _FLOAT_RE.search(str(value))
    if not match:
        return None
    return float(match.group().replace(",", "."))