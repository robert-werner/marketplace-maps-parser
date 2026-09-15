# src/infrastructure/transports/cookie_loader.py
"""Load browser cookies for a logged-in Ozon session.

Two formats are accepted:

- Playwright/DevTools JSON — a list of objects with at least
  ``name``, ``value`` and ``domain`` (export a logged-in session
  with any cookie-editor extension, or dump
  ``context.cookies()`` after a manual login).
- Netscape cookie file — tab-separated lines with ``#`` comments,
  as written by curl/wget and most cookie exporters.

Cookies are injected into every browser page the public_page
transport opens. Measured 2026-09-15: anonymous sessions get ~33
review pages (≈990 reviews) from the reviews widget; a logged-in
session unlocks the full list.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Playwright's add_cookies rejects anything else.
_SAMESITE_ALLOWED = {"Strict", "Lax", "None"}


def _normalize_json_cookie(raw: dict[str, Any]) -> dict[str, Any] | None:
    name = raw.get("name")
    value = raw.get("value")
    domain = raw.get("domain")
    if not (name and value and domain):
        return None
    cookie: dict[str, Any] = {
        "name": name,
        "value": value,
        "domain": domain,
        "path": raw.get("path") or "/",
    }
    # cookie-editor extensions use "expirationDate"; DevTools and
    # Playwright use "expires". Both are unix timestamps.
    expires = raw.get("expires", raw.get("expirationDate"))
    if isinstance(expires, (int, float)) and expires > 0:
        cookie["expires"] = expires
    if raw.get("secure") is not None:
        cookie["secure"] = bool(raw["secure"])
    if raw.get("httpOnly") is not None:
        cookie["httpOnly"] = bool(raw["httpOnly"])
    same_site = raw.get("sameSite")
    if same_site in _SAMESITE_ALLOWED:
        cookie["sameSite"] = same_site
    else:
        # Optional in Playwright; an invalid value would crash
        # add_cookies, so normalize instead of dropping the cookie.
        cookie["sameSite"] = "Lax"
    return cookie


def _normalize_netscape_line(
    line: str,
) -> dict[str, Any] | None:
    # domain  include-subdomains  path  secure  expiry  name  value
    parts = line.split("\t")
    if len(parts) != 7:
        return None
    domain, _flag, path, secure, expiry, name, value = parts
    if not (domain and name):
        return None
    cookie: dict[str, Any] = {
        "name": name,
        "value": value,
        "domain": domain,
        "path": path or "/",
        "secure": secure.strip().lower() in ("true", "1"),
    }
    try:
        expires = float(expiry)
        if expires > 0:
            cookie["expires"] = expires
    except ValueError:
        pass  # session cookie
    return cookie


def load_cookies_file(path: str | Path) -> list[dict[str, Any]]:
    """Parse a cookie file into Playwright's add_cookies format.

    Raises ``ValueError`` when the file exists but yields no valid
    cookies; lets ``FileNotFoundError`` propagate for a wrong path.
    """
    text = Path(path).read_text(encoding="utf-8-sig")

    try:
        data = json.loads(text)
    except ValueError:
        data = None

    if isinstance(data, list):
        cookies = [
            normalized
            for item in data
            if isinstance(item, dict)
            for normalized in [_normalize_json_cookie(item)]
            if normalized is not None
        ]
    else:
        cookies = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            normalized = _normalize_netscape_line(stripped)
            if normalized is not None:
                cookies.append(normalized)

    if not cookies:
        raise ValueError(
            f"не найдено ни одного валидного cookie в {path} "
            "(ожидается JSON-список Playwright или Netscape-файл)"
        )
    return cookies
