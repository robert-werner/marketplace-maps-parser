"""Wildberries review cards via observed browser JSON or live DOM.

Open the product first so the review route inherits the same browser session.
No guessed product-root/shard mapping; API URLs come from this page's traffic.
"""
from __future__ import annotations

import asyncio
import re
import time
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
    """Raised when WB actively blocks access (498, 403, captcha)."""


class HTTP498Error(WildberriesBlockError):
    """Raised on HTTP 498 (browser challenge)."""


class HTTP403Error(WildberriesBlockError):
    """Raised on HTTP 403 (account blocked)."""


class CaptchaError(WildberriesBlockError):
    """Raised on captcha challenge."""


def _wb_error(status: int) -> WildberriesBlockError:
    """Map HTTP status to a specific WB block error type."""
    if status == 498:
        return HTTP498Error(f"HTTP {status}")
    if status == 403:
        return HTTP403Error(f"HTTP {status}")
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
        use_stealth: bool = True,
        session_seconds: int = 0,
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
        self.collection_path = "dom"
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

    async def _check_api_available(self, product_url: str) -> bool:
        """Anti-bot gate: is the WB reviews API reachable right now?

        API-only mode is only safe *after* the anti-bot check passes. A
        quick probe to the reviews endpoint confirms it; a 403/498/5xx
        (or any network failure) means the WAF has this proxy blocked,
        so the browser path (which can solve the challenge) is preferred.
        """
        try:
            import httpx
        except ImportError:
            return False
        nm_id = extract_nm_id(product_url)
        proxy_str = None
        if self.proxy and isinstance(self.proxy, dict):
            proxy_str = self.proxy.get("server")
        url = (
            f"https://www.wildberries.ru/api/v2/catalog/"
            f"{nm_id}/reviews?size=1"
        )
        try:
            with httpx.Client(
                proxy=proxy_str, timeout=min(self.timeout_ms, 20_000) / 1000.0,
                follow_redirects=True,
            ) as client:
                resp = client.get(url, follow_redirects=True)
        except Exception as exc:
            log.warning(
                f"WB API pre-check failed (network): {exc}"
            )
            return False
        return resp.status_code == 200

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
        if self.use_api_only:
            # API-only is only used AFTER the anti-bot check passes.
            if await self._check_api_available(product_url):
                async for batch in self._iter_api_only(product_url):
                    yield batch
                return
            log.warning(
                "WB API unavailable (anti-bot blocked); "
                "falling back to browser path"
            )
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
                max_restarts = max(
                    self.proxy_rotate_max_restarts,
                    self.cookie_rotate_max_restarts,
                )
                if (
                    self.proxy_pool is None
                    and self.cookie_pool is None
                ) or restarts > max_restarts:
                    raise
                # Mark the proxy that just got blocked so future rotations
                # skip it (the pool's round-robin only cycles through
                # unblocked proxies). Without this, a single pinned proxy
                # that is already 498'd is retried endlessly.
                pool_stats = None
                if self.proxy_pool is not None:
                    if failed_proxy is not None:
                        self.proxy_pool.mark_blocked(failed_proxy)
                    pool_stats = self.proxy_pool.get_stats()
                    if self.proxy_pool.all_blocked():
                        raise WildberriesBlockError(
                            f"{exc} (proxy pool exhausted — all "
                            f"{pool_stats['blocked']}/{pool_stats['total']} "
                            "proxies blocked)",
                        ) from exc
                if self.proxy_pool is not None:
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
                    f"{pool_part}, attempt {restarts}/{max_restarts},",
                )
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
        challenge = (
            product_response is not None
            and product_response.status >= 400
        )
        if self.settle_ms > 0 or challenge:
            try:
                # Don't replace an interstitial before the site completes
                # its own redirect. Ready pages pay no extra fixed delay;
                # a challenge gate may take up to tens of seconds, so use a
                # longer bound to avoid misfiring HTTP498 on slow solves.
                challenge_wait = min(self.timeout_ms, 30_000)
                await link.wait_for(
                    state="attached",
                    timeout=(
                        challenge_wait if challenge
                        else min(self.timeout_ms, 8_000)
                    ),
                )
            except Exception as exc:
                if product_response is not None and (
                    product_response.status >= 400
                ):
                    raise _wb_error(product_response.status) from exc
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
                    raise _wb_error(response.status) from exc
        selector = '[data-testid="feedbacks-list-item"]'
        cards = page.locator(selector)
        try:
            await cards.first.wait_for(
                state='visible', timeout=self.timeout_ms,
            )
        except Exception as exc:
            raise RuntimeError(
                'Wildberries: карточки отзывов не загрузились'
            ) from exc
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

    async def _iter_api_only(
        self, product_url: str,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Direct httpx call to WB reviews API; no browser."""
        nm_id = extract_nm_id(product_url)
        size = 200
        seen_api: set[str] = set()
        seen_urls: set[str] = set()
        try:
            import httpx
        except ImportError:
            raise RuntimeError("httpx not installed for API-only mode")

        proxy_str = None
        if self.proxy and isinstance(self.proxy, dict):
            proxy_str = self.proxy.get("server")

        with httpx.Client(
            proxy=proxy_str,
            timeout=self.timeout_ms / 1000.0,
            headers={"User-Agent": "Mozilla/5.0"},
            follow_redirects=True,
        ) as client:
            current = (
                f"https://www.wildberries.ru/api/v2/catalog/"
                f"{nm_id}/reviews?size={size}"
            )
            while True:
                if current in seen_urls:
                    raise RuntimeError("WB API repeated cursor")
                seen_urls.add(current)
                resp = client.get(current, follow_redirects=True)
                if resp.status_code >= 400:
                    if self.proxy_pool and self.proxy_pool.all_blocked():
                        raise _wb_error(resp.status_code)
                    raise _wb_error(resp.status_code)
                payload = resp.json()
                self.collection_path = "api_only"
                api_cards = feedback_cards(payload, nm_id)
                fresh = []
                for card in api_cards:
                    if card["id"] not in seen_api:
                        seen_api.add(card["id"])
                        fresh.append(card)
                for offset in range(0, len(fresh), 100):
                    yield fresh[offset:offset + 100]
                next_url = feedback_next_url(current, payload)
                if not next_url or not fresh:
                    self.incomplete_reason = (
                        "WB API window: full review coverage unverified"
                    )
                    return
                current = next_url
        self.incomplete_reason = "WB API page limit reached"

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

__all__ = ['WildberriesBrowserTransport']

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
