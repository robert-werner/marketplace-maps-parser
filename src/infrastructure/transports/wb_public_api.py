"""Wildberries public review API — no browser, no anti-bot token.

Measured 2026-11-08 (see ROADMAP.md):

* ``www.wildberries.ru`` answers 498 — the WBAAS JS challenge — to every
  client without an ``x_wbaas_token`` cookie, from any IP;
* ``basket-NN.wbbasket.ru/.../info/ru/card.json`` (nm -> imt_id, title) and
  ``feedbacks{1,2}.wb.ru/feedbacks/v2/{imt}`` answer 200 without a token,
  even from datacenter IPs;
* one ``/feedbacks/v2/{imt}`` response carries the newest ~1000 reviews with
  text for the whole imt (every variant); ``skip``/``take`` are ignored and
  rating-only reviews are never included; an imt without reviews returns
  ``"feedbacks": null`` and ``"valuation": ""``.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from infrastructure.transports.wb_api import feedback_cards
from shared.logging import get_logger

log = get_logger("infrastructure.transports.wb_public_api")

BASKET_MAX = 64
FEEDBACK_HOSTS = ("https://feedbacks1.wb.ru", "https://feedbacks2.wb.ru")
FEEDBACK_SHARD_URL = (
    "https://feedback-bt.wildberries.ru/feedback/api/v2/host?imt={imt}"
)
# Measured: imt 10283579 (142k text reviews) -> v2 returned 1015 items,
# v1 exactly 1000 — the API serves a window of the newest reviews.
FEEDBACK_WINDOW = 1000
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:151.0) "
    "Gecko/20100101 Firefox/151.0"
)

# vol -> basket base URL, filled by successful probes (process lifetime).
_basket_cache: dict[int, str] = {}


class WbPublicApiError(RuntimeError):
    """The public API path could not produce a result."""


@dataclass
class PublicReviews:
    nm_id: int
    imt_id: int
    title: str | None
    total_count: int | None
    text_count: int | None
    average_rating: float | None
    cards: list[dict[str, Any]] = field(default_factory=list)
    returned: int = 0
    window_limited: bool = False

    @property
    def rating_only_count(self) -> int | None:
        if self.total_count is None or self.text_count is None:
            return None
        return max(self.total_count - self.text_count, 0)


def proxy_url(proxy: dict[str, str] | None) -> str | None:
    """Playwright proxy dict -> httpx proxy URL, keeping the credentials."""
    if not proxy or not proxy.get("server"):
        return None
    server = proxy["server"]
    user = proxy.get("username")
    if not user or "@" in server:
        return server
    scheme, _, rest = server.partition("://")
    if not rest:
        scheme, rest = "http", server
    auth = quote(user, safe="")
    password = proxy.get("password")
    if password:
        auth += ":" + quote(password, safe="")
    return f"{scheme}://{auth}@{rest}"


def card_path(nm_id: int) -> str:
    return (
        f"/vol{nm_id // 100_000}/part{nm_id // 1000}/{nm_id}/info/ru/card.json"
    )


def parse_valuation(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


async def _get(
    client: httpx.AsyncClient, url: str, *, attempts: int = 3,
    backoff_s: float = 1.0,
) -> httpx.Response:
    """GET with retry on 429/5xx/transport errors; returns the last reply."""
    last: httpx.Response | None = None
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            last = await client.get(url)
            if last.status_code != 429 and last.status_code < 500:
                return last
        except httpx.TransportError as exc:
            error = exc
        if attempt + 1 < attempts:
            await asyncio.sleep(backoff_s * 2 ** attempt)
    if last is not None:
        return last
    raise WbPublicApiError(f"GET {url} failed: {error!r}")


async def resolve_card(
    client: httpx.AsyncClient, nm_id: int,
) -> dict[str, Any]:
    """Fetch ``card.json`` for ``nm_id``; the basket host is discovered."""
    vol = nm_id // 100_000
    path = card_path(nm_id)
    cached = _basket_cache.get(vol)
    if cached:
        try:
            resp = await client.get(cached + path)
            if resp.status_code == 200:
                return resp.json()
        except (httpx.HTTPError, ValueError):
            pass

    guess = _guess_baskets(vol)
    rest = [i for i in range(1, BASKET_MAX + 1) if i not in guess]
    # Neighbouring vols live on the same or an adjacent basket, so a warm
    # cache needs 1-4 requests instead of a 64-host fan-out (measured on
    # 541 products: 34 wasted 404s per product without the guess).
    for wave in (guess, rest):
        hit = await _first_hit(client, [_basket_base(i) for i in wave], path)
        if hit is not None:
            _basket_cache[vol] = hit[0]
            return hit[1]
    raise WbPublicApiError(
        f"card.json for nm {nm_id} not found on basket-01..{BASKET_MAX:02d}"
    )


def _basket_base(number: int) -> str:
    return f"https://basket-{number:02d}.wbbasket.ru"


def _guess_baskets(vol: int) -> list[int]:
    """Basket numbers to try first: the nearest cached vol's and its +-1/2."""
    if not _basket_cache:
        return []
    nearest = min(_basket_cache, key=lambda v: abs(v - vol))
    try:
        number = int(_basket_cache[nearest].split("basket-")[1].split(".")[0])
    except (IndexError, ValueError):
        return []
    order = (number, number + 1, number - 1, number + 2)
    return [n for n in order if 1 <= n <= BASKET_MAX]


async def _first_hit(
    client: httpx.AsyncClient, bases: list[str], path: str,
) -> tuple[str, dict[str, Any]] | None:
    """Probe ``bases`` concurrently; the first 200 wins, the rest cancel."""
    if not bases:
        return None

    async def probe(base: str) -> tuple[str, dict[str, Any]] | None:
        try:
            resp = await client.get(base + path, timeout=10.0)
            if resp.status_code == 200:
                return base, resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        return None

    tasks = [asyncio.create_task(probe(base)) for base in bases]
    try:
        for fut in asyncio.as_completed(tasks):
            hit = await fut
            if hit is not None:
                return hit
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return None


def _is_feedback_payload(payload: Any) -> bool:
    return isinstance(payload, dict) and "feedbacks" in payload


async def fetch_feedbacks(
    client: httpx.AsyncClient, imt_id: int,
) -> dict[str, Any]:
    """``/feedbacks/v2/{imt}`` from the mirrors, then the advertised shard."""
    errors: list[str] = []

    async def attempt(base: str) -> dict[str, Any] | None:
        url = f"{base.rstrip('/')}/feedbacks/v2/{imt_id}"
        try:
            resp = await _get(client, url)
            if resp.status_code == 200:
                payload = resp.json()
                if _is_feedback_payload(payload):
                    return payload
            errors.append(f"{url}: HTTP {resp.status_code}")
        except (WbPublicApiError, ValueError, httpx.HTTPError) as exc:
            errors.append(f"{url}: {exc!r}"[:200])
        return None

    for base in FEEDBACK_HOSTS:
        payload = await attempt(base)
        if payload is not None:
            return payload
    try:
        resp = await _get(client, FEEDBACK_SHARD_URL.format(imt=imt_id))
        shards = resp.json() if resp.status_code == 200 else []
    except (WbPublicApiError, ValueError, httpx.HTTPError):
        shards = []
    for base in shards if isinstance(shards, list) else []:
        if isinstance(base, str) and base.startswith("https://"):
            payload = await attempt(base)
            if payload is not None:
                return payload
    raise WbPublicApiError(
        "WB feedbacks unavailable: " + "; ".join(errors)[:500]
    )


async def collect(
    nm_id: int,
    *,
    proxy: dict[str, str] | None = None,
    timeout_s: float = 20.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> PublicReviews:
    """nm -> card.json -> imt -> every review the public API returns."""
    async with httpx.AsyncClient(
        proxy=proxy_url(proxy) if transport is None else None,
        transport=transport,
        timeout=timeout_s,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    ) as client:
        card = await resolve_card(client, nm_id)
        imt_id = card.get("imt_id")
        if not isinstance(imt_id, int):
            raise WbPublicApiError(f"card.json for nm {nm_id} has no imt_id")
        payload = await fetch_feedbacks(client, imt_id)
    items = payload.get("feedbacks") or []
    text_count = payload.get("feedbackCountWithText")
    result = PublicReviews(
        nm_id=nm_id,
        imt_id=imt_id,
        title=card.get("imt_name") or None,
        total_count=payload.get("feedbackCount"),
        text_count=text_count if isinstance(text_count, int) else None,
        average_rating=parse_valuation(payload.get("valuation")),
        cards=feedback_cards(payload, nm_id),
        returned=len(items),
    )
    result.window_limited = (
        result.returned >= FEEDBACK_WINDOW
        and result.text_count is not None
        and result.text_count > result.returned
    )
    log.info(
        f"WB public API: nm={nm_id} imt={imt_id} returned={result.returned} "
        f"for_nm={len(result.cards)} total={result.total_count} "
        f"with_text={result.text_count}"
    )
    return result


__all__ = [
    "FEEDBACK_WINDOW", "PublicReviews", "WbPublicApiError", "collect",
    "fetch_feedbacks", "proxy_url", "resolve_card",
]
