"""Wildberries review cards via observed browser JSON or live DOM.

Open the product first so the review route inherits the same browser session.
No guessed product-root/shard mapping; API URLs come from this page's traffic.
"""
from __future__ import annotations

import re
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
from shared.url_parsers import extract_nm_id

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
    ) -> None:
        self.product_url = product_url
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self.proxy = proxy
        self.humanize = humanize
        self.max_scrolls = max_scrolls
        self.cookies = cookies
        self.use_api = use_api
        self.max_pages = max_pages
        self.collection_path = "dom"
        self._capture: ResponseCapture | None = None
        self.last_product_title: str | None = None
        self.last_total_count: int | None = None
        self.last_average_rating: float | None = None
        self.incomplete_reason: str | None = None
        self._page: Any = None
        self._browser_ctx: Any = None

    async def __aenter__(self) -> WildberriesBrowserTransport:
        browser_cls = import_invisible_playwright()
        self._browser_ctx = browser_cls(
            proxy=self.proxy, seed=None, humanize=self.humanize,
        )
        try:
            browser = await self._browser_ctx.__aenter__()
            self._page = await browser.new_page()
            if self.cookies:
                await self._page.context.add_cookies(self.cookies)
            self._capture = ResponseCapture(self._page, is_feedback_url)
            self._capture.__enter__()
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            if self._capture is not None:
                self._capture.__exit__()
            if self._browser_ctx is not None:
                await self._browser_ctx.__aexit__(exc_type, exc, tb)
        finally:
            self._page = None
            self._browser_ctx = None
            self._capture = None

    async def iter_review_batches(
        self, product_url: str,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        if self._page is None:
            async with self:
                async with closing_iterator(
                    self.iter_review_batches(product_url),
                ) as stream:
                    async for batch in stream:
                        yield batch
            return
        nm_id = extract_nm_id(product_url)
        page = self._page
        product_response = await page.goto(
            product_url, timeout=self.timeout_ms,
            wait_until='domcontentloaded',
        )
        # Wait for the route link rather than an unconditional page sleep.
        link = page.locator(f'a[href*="/catalog/{nm_id}/feedbacks"]').first
        if self.settle_ms > 0 or (
            product_response is not None and product_response.status >= 400
        ):
            try:
                # Don't replace an interstitial before the site completes
                # its own redirect. Ready pages pay no extra fixed delay.
                await link.wait_for(
                    state="attached",
                    timeout=min(self.timeout_ms, 8_000),
                )
            except Exception as exc:
                if product_response is not None and (
                    product_response.status >= 400
                ):
                    raise RuntimeError(
                        "Wildberries product blocked "
                        f"(HTTP {product_response.status})",
                    ) from exc
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
                # never mistake an empty challenge document for EOF.
                try:
                    await page.locator(
                        '[data-testid="feedbacks-list-item"]',
                    ).first.wait_for(
                        state="attached",
                        timeout=min(8_000, self.timeout_ms),
                    )
                except Exception as exc:
                    raise RuntimeError(
                        f"Wildberries blocked (HTTP {response.status})",
                    ) from exc
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
                r'^Отзывы на (.+?) в интернет[‑-]магазине',
                await page.title(),
            )
            if match:
                self.last_product_title = match.group(1)

        seen: set[str] = set()
        stalled = 0
        for _ in range(self.max_scrolls + 1):
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


__all__ = ['WildberriesBrowserTransport']
