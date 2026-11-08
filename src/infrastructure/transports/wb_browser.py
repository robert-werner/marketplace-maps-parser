"""Wildberries review cards via observed browser JSON or live DOM.

Open the product first so the review route inherits the same browser session.
No guessed product-root/shard mapping; API URLs come from this page's traffic.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urljoin, urlsplit

from infrastructure.transports.browser_api import ResponseCapture
from infrastructure.transports.browser_common import (
    import_invisible_playwright,
)
from infrastructure.transports.browser_waits import (
    card_signature,
    wait_for_card_change,
)
from infrastructure.transports.wb_api import (
    feedback_cards,
    feedback_next_url,
    fetch_feedback_page,
    is_feedback_url,
)
from shared.async_iterators import closing_iterator
from shared.logging import get_logger
from shared.url_parsers import extract_nm_id

log = get_logger("infrastructure.transports.wb_browser")


class WildberriesBlockError(RuntimeError):
    """Raised when WB actively blocks access (498, 403, captcha).

    ``retryable`` distinguishes permanent IP/account blocks (False — the
    proxy is burned) from transient failures (True — back off and retry
    the same proxy without burning it).
    """

    retryable = False

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        self.retryable = retryable
        super().__init__(message)


class HTTP498Error(WildberriesBlockError):
    """Raised on HTTP 498 (browser challenge)."""


class HTTP403Error(WildberriesBlockError):
    """Raised on HTTP 403 (account blocked)."""


class CaptchaError(WildberriesBlockError):
    """Raised on captcha challenge."""


class IPReputationError(WildberriesBlockError):
    """WB passed the challenge but refuses this egress IP.

    After the anti-bot token is issued the SPA shows «Возможно, нужно
    выключить VPN» and loads no data (measured on an OVH datacenter IP).
    Permanent for this proxy: burn it and rotate.
    """


# WBAAS anti-bot cookie issued once the JS challenge is solved (measured:
# 10 s on a datacenter IP, ~21 s incl. SPA render on a residential one).
WB_TOKEN_COOKIE = "x_wbaas_token"
_VPN_BLOCK_MARKER = "выключить VPN"


class RetryableHTTPError(WildberriesBlockError):
    """Raised on a transient HTTP failure (429, 5xx).

    Not an IP/account block: the same proxy is fine again after a short
    pause, so it must not be burned.
    """

    retryable = True

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


def _wb_error(status: int) -> WildberriesBlockError:
    """Map an HTTP status to the matching WB block error.

    498/403 are permanent IP/account blocks; 429 and 5xx are transient
    and must be retried (with backoff) rather than burning the proxy.
    """
    if status == 498:
        return HTTP498Error(f"HTTP {status}")
    if status == 403:
        return HTTP403Error(f"HTTP {status}")
    if status == 429 or 500 <= status < 600:
        return RetryableHTTPError(f"HTTP {status}")
    return WildberriesBlockError(f"HTTP {status}")


_READ_PAGE_JS = """() => {
    const clean = (el) => el?.textContent?.trim() || null;
    const cards = [...document.querySelectorAll(
        '[data-testid="feedbacks-list-item"]'
    )].map(card => {
        const body = card.querySelector('[itemprop="reviewBody"]');
        const sections = [...(body?.children || [])].map(el => {
            const label = clean(el.querySelector('[class*="label"]'));
            return {label, value: clean(el)?.slice(label?.length || 0).trim()};
        });
        const labeled = sections.some(section => section.label);
        const ratingClass = card.querySelector('[class*="stars-line"]')
            ?.className || '';
        const rating = /(?:^|\\s)star([1-5])(?:\\s|$)/
            .exec(ratingClass)?.[1];
        const media = [...card.querySelectorAll(
            '[data-testid="feedbacks-list-photo-viewer-trigger"] img'
        )];
        return {
            author: card.querySelector('[itemprop="author"]')
                ?.getAttribute('content') || clean(card.querySelector(
                    '[class*="feedbackItemName"]')),
            date: clean(card.querySelector(
                '[class*="itemDateWrapper"]'))
                || [...card.querySelectorAll('span')].map(clean).find(
                    value => /^(?:Сегодня|Вчера|\\d{1,2}\\s+[а-яё]+)/i
                        .test(value || '')
                ) || null,
            rating: rating ? Number(rating) : null,
            text: labeled ? null : clean(body),
            sections: labeled ? sections : [],
            answer: clean(card.querySelector(
                '[class*="sellerReplyText"]'))?.replace(/ещё$/i, '').trim(),
            photos: media.filter(img => img.alt !== 'video preview')
                .map(img => img.getAttribute('data-src-pb') || img.src),
            videos: media.filter(img => img.alt === 'video preview')
                .map(img => img.getAttribute('data-src-pb') || img.src),
        };
    });
    const ratingCounter = [...document.querySelectorAll('span')].find(el =>
        /^\\d[\\d\\s ]*\\s+оцен(?:ка|ки|ок)$/i.test(clean(el) || '')
    );
    const score = [...document.querySelectorAll('h2')].find(h =>
        /^\\d[.,]\\d$/.test(clean(h) || '')
    );
    return {
        cards,
        total: ratingCounter ? Number((clean(ratingCounter)?.match(
            /([\\d\\s ]+)\\s+оцен(?:ка|ки|ок)/i
        ) || [])[1]?.replace(/\\s/g, '')) || null : null,
        average: score ? Number(clean(score).replace(',', '.')) : null,
    };
}"""


class WildberriesBrowserTransport:
    """Yield DOM review batches as the browser scrolls the reviews page."""

    def __init__(
        self,
        *,
        product_url: str,
        timeout_ms: int = 90_000,
        settle_ms: int = 4_000,
        proxy: dict[str, str] | None = None,
        humanize: bool = True,
        max_scrolls: int = 200,
        cookies: list[dict[str, Any]] | None = None,
        use_api: bool = True,
        max_pages: int | None = None,
        proxy_pool: Any = None,
        proxy_rotate_max_restarts: int = 3,
        cookie_pool: Any = None,
        cookie_rotate_max_restarts: int = 3,
        use_api_only: bool = False,
        use_stealth: bool = False,
        session_seconds: int = 0,
        save_cookies_path: str | None = None,
    ) -> None:
        self.product_url = product_url
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self.proxy = proxy
        self._proxy = proxy
        self.humanize = humanize
        self.max_scrolls = max_scrolls
        self.cookies = cookies
        self.use_api = use_api
        self.max_pages = max_pages
        self.proxy_pool = proxy_pool
        self.proxy_rotate_max_restarts = proxy_rotate_max_restarts
        self.cookie_pool = cookie_pool
        self.cookie_rotate_max_restarts = cookie_rotate_max_restarts
        self.use_api_only = use_api_only
        self.use_stealth = use_stealth
        self.session_seconds = session_seconds
        self.save_cookies_path = save_cookies_path
        self.collection_path = "dom"
        self.last_rating_only_count: int | None = None
        self._capture: ResponseCapture | None = None
        self.last_product_title: str | None = None
        self.last_total_count: int | None = None
        self.last_average_rating: float | None = None
        self.incomplete_reason: str | None = None
        self._page: Any = None
        self._browser_ctx: Any = None
        self._seen_review_keys: set[str] = set()

    def _next_proxy(self) -> dict[str, str] | None:
        """Pull the next proxy from the pool (None if no pool)."""
        if self.proxy_pool is None:
            return None
        return self.proxy_pool.next()

    async def _cleanup(self) -> None:
        """Close any existing browser context."""
        try:
            if self._capture is not None:
                self._capture.__exit__()
            if self._browser_ctx is not None:
                await self._browser_ctx.__aexit__(None, None, None)
        finally:
            self._page = None
            self._browser_ctx = None
            self._capture = None

    def _review_key(self, card: Any) -> str:
        """Stable key for cross-restart dedupe.

        API cards carry a stable ``id``; DOM cards do not, so fall back to a
        content-based composite (same key used for in-session DOM dedupe).
        """
        if isinstance(card, dict):
            rid = card.get("id")
            if rid is not None:
                return f"id:{rid}"
            return "c:" + repr(
                (
                    card.get("author"),
                    card.get("date"),
                    card.get("rating"),
                    card.get("text"),
                    card.get("sections"),
                )
            )
        return f"raw:{id(card)}"

    def _dedupe(self, cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return ``cards`` minus any already emitted this transport run.

        Keeps the per-transport ``_seen_review_keys`` set as the source of
        truth across browser-session restarts, so a channel-error restart can
        never emit the same review twice.
        """
        fresh: list[dict[str, Any]] = []
        for card in cards:
            key = self._review_key(card)
            if key not in self._seen_review_keys:
                self._seen_review_keys.add(key)
                fresh.append(card)
        return fresh

    async def _apply_stealth(self, page: Any) -> None:
        """Inject stealth overrides into the page context."""
        if not self.use_stealth:
            return
        try:
            await page.add_init_script(_WB_STEALTH_JS)
        except Exception as exc:
            import logging
            logging.getLogger("wb_browser").warning(
                f"Stealth injection failed: {exc}"
            )

    async def iter_review_batches(
        self, product_url: str,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Yield review batches, rotating proxy and cookies on WB
        block."""
        if self.use_api or self.use_api_only:
            # Public API first: no browser, no WBAAS token (the 498
            # challenge guards only www.wildberries.ru).
            public = await self._try_public_api(product_url)
            if public is not None:
                for offset in range(0, len(public), 100):
                    yield public[offset:offset + 100]
                return
        restarts = 0
        channel_restarts = 0
        current_cookies = (
            self.cookie_pool.next_jar()
            if self.cookie_pool is not None
            else self.cookies
        )

        while True:
            await self._cleanup()

            proxy = self._proxy
            if proxy is None and self.proxy_pool is not None:
                if self.proxy_pool.all_blocked():
                    raise WildberriesBlockError(
                        "proxy pool exhausted — every proxy in the pool "
                        "was already blocked; nothing left to try",
                    )
                proxy = self._next_proxy()
                self._proxy = proxy

            browser_cls = import_invisible_playwright()
            self._browser_ctx = browser_cls(
                proxy=proxy, seed=None, humanize=self.humanize,
            )
            browser = await self._browser_ctx.__aenter__()
            self._page = await browser.new_page()
            if self.use_stealth:
                await self._apply_stealth(self._page)
            if current_cookies:
                await self._page.context.add_cookies(current_cookies)
            self._capture = ResponseCapture(self._page, is_feedback_url)
            self._capture.__enter__()

            try:
                async for batch in self._iter_product_and_reviews(
                    product_url,
                ):
                    yield batch
                return
            except WildberriesBlockError as exc:
                restarts += 1
                failed_proxy = self._proxy
                is_retryable = getattr(exc, "retryable", False)
                max_restarts = max(
                    self.proxy_rotate_max_restarts,
                    self.cookie_rotate_max_restarts,
                )
                if (
                    self.proxy_pool is None
                    and self.cookie_pool is None
                ) or restarts > max_restarts:
                    raise
                # Mark the proxy as burned only for permanent blocks
                # (498/403). Transient errors (429, 5xx) leave the proxy
                # usable for the next attempt — that is exactly what a
                # rate-limit/server-blip means: retry it after a pause,
                # do not burn it.
                pool_stats = None
                if self.proxy_pool is not None:
                    if failed_proxy is not None and not is_retryable:
                        self.proxy_pool.mark_blocked(failed_proxy)
                    pool_stats = self.proxy_pool.get_stats()
                    if self.proxy_pool.all_blocked():
                        raise WildberriesBlockError(
                            f"{exc} (proxy pool exhausted — all "
                            f"{pool_stats['blocked']}/{pool_stats['total']} "
                            "proxies blocked)",
                        ) from exc
                if self.proxy_pool is not None and not is_retryable:
                    self._proxy = self._next_proxy()
                jar_available = (
                    self.cookie_pool is None
                    or not self.cookie_pool.all_blocked()
                )
                if (
                    self.proxy_pool is not None
                    and self._proxy is None
                    and not jar_available
                ):
                    raise WildberriesBlockError(
                        f"{exc} (proxy pool and cookie pool exhausted)",
                    ) from exc
                current_cookies = (
                    self.cookie_pool.next_jar()
                    if self.cookie_pool is not None
                    else None
                )
                proxy_part = (
                    f"proxy={self._proxy.get('server', '?')}"
                    if self._proxy is not None
                    else "proxy=none"
                )
                pool_part = ""
                if self.proxy_pool is not None and pool_stats is not None:
                    pool_part = (
                        f" pool={pool_stats['available']}/"
                        f"{pool_stats['total']} "
                        f"(blocked={pool_stats['blocked']})"
                    )
                jar_part = (
                    " + новые cookies"
                    if self.cookie_pool is not None
                    and jar_available
                    else ""
                )
                print(
                    f"WB: block — restart {proxy_part}{jar_part}"
                    f"{pool_part}, attempt {restarts}/{max_restarts},"
                    f"{' [transient]' if is_retryable else ''},",
                )
                # Back off so we do not hammer the same (possibly still
                # rate-limited) IP and extend the WAF window.
                await asyncio.sleep(2.0)
            except Exception as exc:
                # Browser/channel-level failure: the session is broken
                # (Playwright channel error, a crashed browser, a reset
                # connection mid-fetch). Re-create the session and retry.
                # A single proxy needs no rotation — just re-open the page
                # — which is what turns a flaky proxy connection into a
                # reliable run.
                channel_restarts += 1
                channel_max = self.proxy_rotate_max_restarts
                if channel_restarts > channel_max:
                    log.warning(
                        f"WB: channel error after {channel_max} retries, "
                        f"giving up: {exc}"
                    )
                    raise
                if self.proxy_pool is not None:
                    self._proxy = self._next_proxy()
                log.info(
                    f"WB: channel error — restart {channel_restarts}/"
                    f"{channel_max}, {str(exc)[:120]}"
                )
                await asyncio.sleep(2.0)
            finally:
                await self._cleanup()

    async def _try_public_api(
        self, product_url: str,
    ) -> list[dict[str, Any]] | None:
        """Collect via the public API; None means "use the browser".

        ``use_api_only`` turns a failure into an error instead of a
        browser fallback. A single ``--proxy`` is reused; a pool is not
        consumed (the public hosts accept datacenter IPs).
        """
        try:
            # Lazy: an image built without httpx (otzovik installs the
            # package with --no-deps) must still reach the browser path.
            from infrastructure.transports import wb_public_api

            result = await wb_public_api.collect(
                extract_nm_id(product_url),
                proxy=self.proxy,
                timeout_s=min(self.timeout_ms, 30_000) / 1000.0,
            )
        except Exception as exc:
            if self.use_api_only:
                raise WildberriesBlockError(
                    f"WB public API failed (--wb-api-only, no browser "
                    f"fallback): {exc}"
                ) from exc
            log.warning(
                f"WB public API failed, falling back to browser: {exc}"
            )
            return None
        self.collection_path = "public_api"
        self.last_product_title = result.title
        self.last_total_count = result.total_count
        self.last_average_rating = result.average_rating
        self.last_rating_only_count = result.rating_only_count
        if result.window_limited:
            self.incomplete_reason = (
                f"WB public API serves only the newest ~"
                f"{wb_public_api.FEEDBACK_WINDOW} reviews with text "
                "(the site shows the same window): "
                f"returned {result.returned} of {result.text_count} "
                "(imt-wide)"
            )
        return self._dedupe(result.cards)

    async def _wait_for_token(self, page: Any, timeout_ms: int) -> bool:
        """Wait until the WBAAS challenge has issued its cookie."""
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            try:
                cookies = await page.context.cookies()
            except Exception:
                return False
            if any(
                isinstance(c, dict) and c.get("name") == WB_TOKEN_COOKIE
                for c in cookies
            ):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.5)

    async def _ip_refused(self, page: Any) -> bool:
        """True when the SPA shows WB's «turn off VPN» refusal."""
        try:
            text = await page.evaluate(
                "document.body ? document.body.innerText : ''"
            )
        except Exception:
            return False
        return isinstance(text, str) and _VPN_BLOCK_MARKER in text

    async def _save_session_cookies(self, page: Any) -> None:
        """Persist the solved-challenge jar for reuse (--save-cookies)."""
        if not self.save_cookies_path:
            return
        try:
            cookies = await page.context.cookies()
            Path(self.save_cookies_path).write_text(
                json.dumps(cookies, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            log.info(
                f"WB: saved {len(cookies)} cookies to "
                f"{self.save_cookies_path}"
            )
        except Exception as exc:
            log.warning(f"WB: cookie save failed: {exc}")

    async def _iter_product_and_reviews(
        self, product_url: str,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Main iteration logic (one browser session)."""
        nm_id = extract_nm_id(product_url)
        page = self._page
        product_response = await page.goto(
            product_url, timeout=self.timeout_ms,
            wait_until='domcontentloaded',
        )
        # Wait for the route link rather than an unconditional page sleep.
        link = page.locator(f'a[href*="/catalog/{nm_id}/feedbacks"]').first
        # A 498 response means the JS challenge gate is up; it can take
        # tens of seconds to resolve, so track it explicitly.
        status = product_response.status if product_response else 0
        challenge = status == 498
        if challenge:
            # The challenge is solved when its cookie appears — judge the
            # stage by its own signal, not by a downstream selector.
            if not await self._wait_for_token(
                page, min(self.timeout_ms, 60_000),
            ):
                raise _wb_error(498)
        if self.settle_ms > 0 or status >= 400:
            try:
                await link.wait_for(
                    state="attached",
                    timeout=(
                        min(self.timeout_ms, 30_000) if status >= 400
                        else min(self.timeout_ms, 8_000)
                    ),
                )
            except Exception as exc:
                if await self._ip_refused(page):
                    raise IPReputationError(
                        "WB refused this egress IP after the challenge "
                        "(«выключить VPN»)"
                    ) from exc
                if challenge:
                    # Token issued, page did not render: a session-level
                    # failure (channel restart), not a burned proxy.
                    raise RuntimeError(
                        "WB: page did not render after the challenge"
                    ) from exc
                if status >= 400:
                    raise _wb_error(status) from exc
        if not urlsplit(page.url).path.rstrip('/').endswith('/feedbacks'):
            title = page.locator('h1').first
            if await title.count():
                self.last_product_title = (await title.inner_text()).strip()
            # The link supplies WB's own imtId/size parameters if needed.
            link = page.locator(f'a[href*="/catalog/{nm_id}/feedbacks"]').first
            feedback_url = (
                await link.get_attribute('href')
                if await link.count() else None
            )
            response = await page.goto(
                urljoin(
                    product_url,
                    feedback_url or f'/catalog/{nm_id}/feedbacks',
                ),
                timeout=self.timeout_ms,
                wait_until='domcontentloaded',
            )
            if response is not None and response.status >= 400:
                # Allow the site a bounded chance to finish a redirect, but
                # never mistake an empty challenge document for EOF. The
                # challenge gate can resolve slowly, so use a longer bound.
                try:
                    await page.locator(
                        '[data-testid="feedbacks-list-item"]',
                    ).first.wait_for(
                        state="attached",
                        timeout=min(30_000, self.timeout_ms),
                    )
                except Exception as exc:
                    if await self._ip_refused(page):
                        raise IPReputationError(
                            "WB refused this egress IP («выключить VPN»)"
                        ) from exc
                    raise _wb_error(response.status) from exc
        selector = '[data-testid="feedbacks-list-item"]'
        cards = page.locator(selector)
        try:
            await cards.first.wait_for(
                state='visible', timeout=self.timeout_ms,
            )
        except Exception as exc:
            if await self._ip_refused(page):
                raise IPReputationError(
                    "WB refused this egress IP («выключить VPN»)"
                ) from exc
            raise RuntimeError(
                'Wildberries: карточки отзывов не загрузились'
            ) from exc
        await self._save_session_cookies(page)
        if self.use_api and self._capture is not None:
            for response in reversed(self._capture.responses):
                try:
                    if response.status != 200:
                        continue
                    payload = await response.json()
                    initial = feedback_cards(payload, nm_id)
                except Exception:
                    continue
                if not initial:
                    continue
                self.collection_path = "api"
                seen_api: set[str] = set()
                seen_urls: set[str] = set()
                current = response.url
                for _ in range(self.max_pages or 100):
                    if current in seen_urls:
                        raise RuntimeError("WB repeated API cursor")
                    seen_urls.add(current)
                    api_cards = feedback_cards(payload, nm_id)
                    fresh = []
                    for card in api_cards:
                        if card["id"] not in seen_api:
                            seen_api.add(card["id"])
                            fresh.append(card)
                    # Cross-restart dedupe: the same review id could be
                    # re-fetched after a browser-session restart; the
                    # per-transport _seen_review_keys set filters it out.
                    fresh = self._dedupe(fresh)
                    # Small batches keep --max-reviews/cancellation cheap
                    # even if WB delivered a large cached response.
                    for offset in range(0, len(fresh), 100):
                        yield fresh[offset:offset + 100]
                    next_url = feedback_next_url(current, payload)
                    if not next_url or not fresh:
                        self.incomplete_reason = (
                            "WB API window: full review coverage unverified"
                        )
                        return
                    current = next_url
                    payload = await fetch_feedback_page(
                        page, current, timeout_ms=self.timeout_ms,
                    )
                self.incomplete_reason = "WB API page limit reached"
                return
        if not self.last_product_title:
            product_link = page.locator(
                f'a[class*="productLineName"]'
                f'[href*="/catalog/{nm_id}/detail.aspx"]'
            ).first
            if await product_link.count():
                self.last_product_title = (
                    await product_link.inner_text()
                ).strip() or None
        if not self.last_product_title:
            match = re.match(
                r'^Отзывы на (.+?) в интернет[\-–]магазине',
                await page.title(),
            )
            if match:
                self.last_product_title = match.group(1)

        seen: set[str] = set()
        stalled = 0
        session_start = time.monotonic()
        for _ in range(self.max_scrolls + 1):
            if (
                self.session_seconds
                and time.monotonic() - session_start
                >= self.session_seconds
            ):
                self.incomplete_reason = (
                    f"WB session cap {self.session_seconds}s reached "
                    "before scroll finished"
                )
                return
            snapshot = await page.evaluate(_READ_PAGE_JS)
            if not isinstance(snapshot, dict):
                raise RuntimeError('Wildberries: DOM отзывов недоступен')
            self.last_total_count = snapshot.get('total')
            self.last_average_rating = snapshot.get('average')
            dom_fresh: list[dict[str, Any]] = []
            for item in snapshot.get('cards') or []:
                if not isinstance(item, dict):
                    continue
                # Index only deduplicates repeated DOM snapshots; the adapter
                # generates a stable composite ID across separate runs.
                key = repr((item.get('author'), item.get('date'),
                            item.get('rating'), item.get('text'),
                            item.get('sections')))
                if key not in seen:
                    seen.add(key)
                    dom_fresh.append(item)
            if dom_fresh:
                # Cross-restart dedupe (per-transport _seen_review_keys).
                dom_fresh = self._dedupe(dom_fresh)
                stalled = 0
                yield dom_fresh
            else:
                stalled += 1
            if stalled >= 3:
                break
            try:
                before = await card_signature(page, selector)
            except (AttributeError, TypeError):
                before = ""
            count = await cards.count()
            if count:
                await cards.nth(count - 1).scroll_into_view_if_needed()
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            try:
                await wait_for_card_change(
                    page, selector=selector, previous=before,
                    timeout_ms=max(self.settle_ms, 500),
                )
            except (AttributeError, TypeError):
                await page.wait_for_timeout(max(self.settle_ms, 500))
        else:
            self.incomplete_reason = "Wildberries: max_scrolls reached"

    async def __aenter__(self) -> "WildberriesBrowserTransport":
        """Backward-compatible: enter creates a single browser context."""
        await self._cleanup()
        self._page = None
        browser_cls = import_invisible_playwright()
        self._browser_ctx = browser_cls(
            proxy=self.proxy, seed=None, humanize=self.humanize,
        )
        try:
            browser = await self._browser_ctx.__aenter__()
            self._page = await browser.new_page()
            if self.use_stealth:
                await self._apply_stealth(self._page)
            entry_cookies = (
                self.cookie_pool.next_jar()
                if self.cookie_pool is not None
                else self.cookies
            )
            if entry_cookies:
                await self._page.context.add_cookies(entry_cookies)
            self._capture = ResponseCapture(self._page, is_feedback_url)
            self._capture.__enter__()
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self._cleanup()

__all__ = ['IPReputationError', 'WildberriesBrowserTransport']

_WB_STEALTH_JS = """
(() => {
    const props = {
        'webdriver': false,
        'languages': ['ru', 'en'],
        'hardwareConcurrency': 16,
        'deviceMemory': 8,
    };
    for (const [key, val] of Object.entries(props)) {
        try {
            Object.defineProperty(
                navigator, key,
                { get: () => val, configurable: true }
            );
        } catch (e) { /* ignore */ }
    }
})();
"""
