"""Wildberries review cards from the live browser DOM, not JSON APIs.

Open the product first so the review route inherits the same browser session.
Only visible page markup is read; scrolling triggers the site's own loading.
"""
from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urljoin, urlsplit

from infrastructure.transports.browser_common import (
    import_invisible_playwright,
)
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
    ) -> None:
        self.product_url = product_url
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self.proxy = proxy
        self.humanize = humanize
        self.max_scrolls = max_scrolls
        self.last_product_title: str | None = None
        self.last_total_count: int | None = None
        self.last_average_rating: float | None = None
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
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            if self._browser_ctx is not None:
                await self._browser_ctx.__aexit__(exc_type, exc, tb)
        finally:
            self._page = None
            self._browser_ctx = None

    async def iter_review_batches(
        self, product_url: str,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        if self._page is None:
            raise RuntimeError('Wildberries browser is not open')
        nm_id = extract_nm_id(product_url)
        page = self._page
        await page.goto(
            product_url, timeout=self.timeout_ms,
            wait_until='domcontentloaded',
        )
        await page.wait_for_timeout(self.settle_ms)
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
            await page.goto(
                urljoin(
                    product_url,
                    feedback_url or f'/catalog/{nm_id}/feedbacks',
                ),
                timeout=self.timeout_ms,
                wait_until='domcontentloaded',
            )
            await page.wait_for_timeout(self.settle_ms)
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
            fresh: list[dict[str, Any]] = []
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
                    fresh.append(item)
            if fresh:
                stalled = 0
                yield fresh
            else:
                stalled += 1
            if stalled >= 3:
                break
            count = await cards.count()
            if count:
                await cards.nth(count - 1).scroll_into_view_if_needed()
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await page.wait_for_timeout(
                max(self.settle_ms, 500),
            )


__all__ = ['WildberriesBrowserTransport']
