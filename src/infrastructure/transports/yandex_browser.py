# src/infrastructure/transports/yandex_browser.py
"""Yandex.Market reviews transport: one stealth browser session
walking the public reviews page.

Flow (mirrors the hard-won Ozon lessons — see README):

1. **Warmup** — land on the product card page first, READ it
   briefly (1-2 wheel nudges: a user who jumps to reviews
   instantly is a thinner pattern), then open the ``/reviews``
   tab with the card page as the HTTP referer. A cold referer-less
   hit on a deep link is the classic bot signal. A stealth init
   script (the same one the Ozon transports use) is injected into
   every page before any site JS runs.
2. **Captcha / soft-block handling** — Yandex serves SmartCaptcha
   as a redirect (``/showcaptcha``) OR INLINE at the reviews URL
   itself (measured 2026-09-18: a ~16 KB shell with title
   «Вы не робот?», ``captcha_smart`` assets and a POST form to
   ``/checkcaptcha`` — no redirect, so URL checks alone are blind).
   A page is HEALTHY when SSR review data is present (JSON-LD
   reviews, the aggregate counter, or DOM cards); anything else is
   challenged until healthy or the attempt budget runs out.
   Escalation ladder per challenge: (a) a short auto-wait — the
   inline shell often resolves itself for a trusted fingerprint;
   (b) MANUAL solving — invisible-playwright runs a visible
   browser by default, so the transport prints a prompt and polls
   while the human clicks the checkbox in the window
   (``manual_captcha=False`` to disable, e.g. headless servers);
   (c) cooldown 5 s -> 30 s + re-navigation. Every challenge feeds
   ``AdaptivePacer.record_block`` so the whole run backs off.
   A confirmed captcha exhausts the budget with
   :class:`YandexCaptchaError`; a markerless degraded shell with
   :class:`YandexSoftBlockError` (the run stops but the already
   yielded batches stay valid).
3. **Pagination** — the reviews list pages via ``?page=N`` (the
   real page ships ``?page=2`` links; measured 2026-09-18). Page
   N+1 is reached by CLICKING the site's own pager link when one
   is present — the navigation a real user makes, carrying the
   page's natural referer and request context; the plain-goto
   fallback chains referers (page N refers to page N-1's URL —
   never the card for deep pages). Settle dwells are right-skewed
   with occasional «зачитался» pauses, scroll strides vary with
   reverse nudges — metronome-exact timing is its own fingerprint.
   Per page: one ``page.evaluate`` round-trip reads every card
   (``data-auto`` markers) + the schema.org JSON-LD block
   (per-review ratings — the DOM stars are unreadable obfuscated
   CSS — and the aggregate counter); within a page the list is
   EXPANDED by Show-More clicks and realistic downward scrolls
   (wheel strides wake the intersection-observer lazy loads; a
   read per round absorbs whatever arrived, deduplicated by the
   per-card ``seen`` key so re-reads are free), until
   ``scroll_max_idle_rounds`` rounds add zero new cards or the
   wheel sits at the document bottom with nothing arriving.
   Stray tabs popped by Show-More / pager clicks (ads,
   ``target=_blank`` wrappers) are closed immediately — the
   visible window keeps only the walk page. END OF LIST: past
   the last page Yandex re-serves old ground instead of an empty
   page, so the walk stops after ``dup_pages_stop`` consecutive
   pages that add ZERO new cards — TOTAL-AWARE: while the JSON-LD
   counter says reviews remain (the expansion often PRELOADS the
   upcoming windows — measured 2026-09-19: a stop at 110 of 250
   with fresh cards still beyond), the budget extends by the
   remaining pages, capped at ``_DUP_PAGES_MAX_EXTENSION`` so an
   unattainable counter (textless «оценки») cannot grind forever.
   ``0`` disables the stop (walk until a truly empty page
   or ``max_pages``).
4. **Own vs feed cards** — the reviews page mixes the product's
   own reviews with a cross-product feed (the scroll expansion
   reaches it). The ``ugc-element-offer-info`` chip's
   ``data-zone-data.oskuId`` is the discriminator: own cards carry
   the open product's id, feed cards a foreign one (on a live
   507-card walk the filter found zero foreign cards — kept as a
   guard). Cards without a readable oskuId fall back to the legacy
   heuristics: a JSON-LD match by (author, ISO-date) — JSON-LD
   describes ONLY the open product — or (loosely) a uuid / body /
   author+date. NOTE on counters (measured 2026-09-19): the JSON-LD
   ``reviewCount`` (250) counts a NARROWER scope than the walked
   list (507 stable cards, all own-osku, on-topic) — the list
   paginates the wider model corpus while the counter appears
   scoped to the current offer/sku, so ``collected > counter`` is
   normal and NOT a leak.
5. **Cookies** — injected before the first navigation and saved to
   ``cookies_path`` after EVERY healthy page (checkpoint), so an
   interrupted run keeps the captcha-warmed session.
6. **Realistic profile** — asset blocking is OFF by default for
   Yandex (unlike Ozon): a real browser loads images and fonts,
   and SmartCaptcha weighs exactly that. Opt back in with
   ``block_assets=True`` once the session is trusted.
7. **Debug-first** — every captcha page and the first reviews page
   are dumped into ``debug_dir`` for postmortems.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from infrastructure.transports.browser_common import (
    import_invisible_playwright,
    _STEALTH_INIT_SCRIPT,
    install_resource_blocker,
)
from shared.pacing import AdaptivePacer
from shared.url_parsers import extract_yandex_market_card_path

YANDEX_MARKET_BASE = "https://market.yandex.ru"

# --- captcha classification -------------------------------------------
#
# URL markers: Yandex redirects to its captcha host when it does not
# trust the session. Body markers: inline SmartCaptcha or the plain
# "access denied" interstitial. Markers must stay UNAMBIGUOUS — the
# Ozon README documents a reviews page that merely contained the
# string "antibot" in its HTML, and a naive marker flagged it.

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

# Inline SmartCaptcha shell (measured 2026-09-18): served at the
# reviews URL itself, no redirect — the page URL stays clean, so
# only body markers can catch it. The shell is ~16 KB, its title
# is «Вы не робот?», it loads ``captcha_smart``.css/js assets and
# renders a POST form to ``/checkcaptcha?key=…``.
_CAPTCHA_SHELL_HTML_MARKERS = (
    "Вы не робот?",
    "captcha_smart",
    'action="/checkcaptcha',
)

# A real 404 page («Нет такой страницы») — served with normal
# headers when the product slug is non-canonical (the card page
# redirects to the canonical slug, /reviews with a wrong slug
# just 404s). Distinguishable from soft-blocks by the explicit
# title / statusCode markers.
_NOT_FOUND_HTML_MARKERS = (
    "Нет такой страницы",
    '"statusCode":404',
)

# A healthy reviews page ships megabytes of SSR HTML; the captcha
# shell is ~16 KB. Body length alone is NOT a captcha verdict
# (markerless soft-blocks look similar) but it separates "big
# healthy-ish page" from "tiny degraded shell".
_SHELL_BODY_MAX_LEN = 700_000

# Page-state classification (see _goto_reviews).
_PAGE_HEALTHY = "healthy"
_PAGE_CAPTCHA = "captcha"
_PAGE_DEGRADED = "degraded"


class YandexCaptchaError(RuntimeError):
    """Raised when Yandex keeps challenging after all retries."""


class YandexSoftBlockError(YandexCaptchaError):
    """Markerless degraded shells after all retries: the run stops
    but the already-collected partial data is valid."""


class YandexNotFoundError(RuntimeError):
    """The reviews URL returned a real 404 («Нет такой страницы»).

    Typically a non-canonical product slug: the card page redirects
    to the canonical one, but ``/reviews`` with a wrong slug just
    404s. NOT retryable — the transport resolves the canonical path
    in the warmup, and this error means even that failed (product
    removed or the URL is not a product at all)."""


def _import_invisible_playwright() -> type:
    """Lazy import (same pattern as the Ozon transports); wrapped
    with GPU-safe software-rendering prefs (gpu_safety.py)."""
    from infrastructure.transports.browser_common import (
        import_invisible_playwright,
    )
    return import_invisible_playwright()


# One evaluate() round-trip per read: DOM cards + JSON-LD reviews +
# aggregate counters in a single payload (the per-attribute reader on
# Ozon measured ~1.6 s/page vs a batch read — see README).
#
# Measured 2026-09-18 against a live page:
# - cards: ``[data-auto="review-item"]``, the numeric review id lives
#   in the ROOT's ``id="review-item-<id>"`` attribute;
# - fields: ``nickname`` (author), ``created-date`` («28 января
#   2024»; rating-only cards ship a YEARLESS date like «26 июня»),
#   ``review-description`` (body; the legacy ``review-comment``
#   selector kept as fallback), ``review-pro`` / ``review-contra``,
#   photos inside ``thumbnail`` imgs (``get-market-ugc`` srcs);
# - the star rating is NOT readable from the DOM (filled/empty stars
#   differ only by obfuscated CSS classes), but the page ships a
#   schema.org JSON-LD ``Product`` block whose ``review[]`` carries
#   ``reviewRating.ratingValue`` — merged into the DOM cards by
#   (author, ISO-date) on the Python side (see _merge_ld_ratings).
_READ_CARDS_JS = """
() => {
    const out = {
        cards: [],
        ld_reviews: [],
        total_count: null,
        average_rating: null,
        product_name: null,
    };

    for (const s of document.querySelectorAll(
            'script[type="application/ld+json"]')) {
        let data;
        try { data = JSON.parse(s.textContent); }
        catch (e) { continue; }
        const items = Array.isArray(data) ? data : [data];
        for (const item of items) {
            if (!item || item['@type'] !== 'Product') continue;
            if (item.name && !out.product_name) {
                out.product_name = item.name;
            }
            const agg = item.aggregateRating || {};
            if (agg.reviewCount != null) {
                out.total_count = agg.reviewCount;
            }
            if (agg.ratingValue != null) {
                out.average_rating = agg.ratingValue;
            }
            for (const r of (item.review || [])) {
                if (!r) continue;
                out.ld_reviews.push({
                    author: r.author && r.author.name
                        ? r.author.name : null,
                    date: r.datePublished || null,
                    text: r.reviewBody || null,
                    rating: r.reviewRating
                        ? r.reviewRating.ratingValue : null,
                });
            }
        }
    }

    const roots = document.querySelectorAll(
        '[data-auto="review-item"]');
    roots.forEach((root, idx) => {
        // Skip seller replies: они помечены специальным data-атрибутом
        // или содержат класс/текст "ответ продавца" / "seller reply"
        if (root.hasAttribute('data-seller-reply') ||
            root.hasAttribute('data-auto-seller-reply') ||
            root.classList.contains('seller-reply') ||
            root.classList.contains('merchant-reply') ||
            root.querySelector('[data-auto="seller-reply"]') ||
            root.querySelector('[data-auto="merchant-reply"]') ||
            root.querySelector('[class*="seller-reply"]') ||
            root.querySelector('[class*="merchant-reply"]')) {
            return;
        }
        
        // Дополнительная проверка по тексту: если в корне есть бадж
        // "Ответ продавца" / "Продавец" в шапке отзыва — skip
        const headerText = (root.textContent || '').toLowerCase();
        if (headerText.indexOf('ответ продавца') !== -1 ||
            headerText.indexOf('комментарий продавца') !== -1 ||
            headerText.indexOf('seller reply') !== -1) {
            // Дополнительная эвристика: ответы продавца обычно короче
            // и не содержат поля pros/cons. Но мы уже пропускаем их выше.
            return;
        }
        
        const get = (sel) => {
            const el = root.querySelector(sel);
            if (!el) return null;
            const v = (el.textContent || '').trim();
            return v || null;
        };
        let uuid = root.getAttribute('id') || '';
        const m = uuid.match(/(\\d+)/);
        if (m) uuid = m[1];
        // The offer-info chip's data-zone-data carries oskuId — the
        // product the review belongs to. Own cards match the open
        // product; cross-product FEED cards carry a foreign oskuId
        // (the Python filter drops them — see _filter_own_cards).
        let offerId = null;
        const chip = root.querySelector(
            '[data-auto="ugc-element-offer-info"]');
        if (chip) {
            const zone = chip.getAttribute('data-zone-data');
            if (zone) {
                try {
                    const z = JSON.parse(zone);
                    if (z && z.oskuId != null) {
                        offerId = String(z.oskuId);
                    }
                } catch (e) {}
            }
        }
        const photos = [];
        root.querySelectorAll(
            '[data-auto="thumbnail"] img',
        ).forEach((img) => {
            let src = img.getAttribute('src') || '';
            if (src.startsWith('//')) src = 'https:' + src;
            if (src && src.indexOf('get-market-ugc') !== -1) {
                photos.push(src);
            }
        });
        const body = get('[data-auto="review-description"]')
            || get('[data-auto="review-comment"]');
        out.cards.push({
            uuid: uuid || ('dom-' + idx),
            author: get('[data-auto="nickname"]'),
            date: get('[data-auto="created-date"]'),
            text: body,
            pros: get('[data-auto="review-pro"]'),
            cons: get('[data-auto="review-contra"]'),
            offer_id: offerId,
            photos: photos.slice(0, 8),
        });
    });

    out.body_len = document.body
        ? document.body.innerHTML.length : 0;

    // Scroll metrics for the expansion loop: when the wheel is at
    // the document bottom and a round added no new cards, there is
    // nothing left to lazy-load on this page.
    out.scroll_y = window.scrollY || 0;
    out.scroll_bottom_gap = Math.max(
        0,
        (document.documentElement.scrollHeight || 0)
        - ((window.scrollY || 0) + (window.innerHeight || 0)),
    );

    return out;
}
"""

_SHOW_MORE_SELECTORS = (
    "[data-auto='showMore']",
    "[data-auto='show-more']",
    "button:has-text('Показать ещё')",
)

# Expansion loop bounds (see _iter_with_browser): the idle counter
# ends the loop normally; the round cap only guards against a
# pathological DOM that always yields one more card.
_EXPAND_ROUNDS_HARD_CAP = 200

# "At the document bottom" tolerance, px: a review card is a few
# hundred px tall, so a gap smaller than this means the next lazy
# chunk (if any) is already on screen.
_BOTTOM_GAP_EPSILON_PX = 200

# Total-aware duplicate tolerance. While the site's counter says
# reviews remain, a dupe streak may just mean the in-page expansion
# PRELOADED the upcoming windows (or the pager re-served them —
# measured 2026-09-19: a run stopped at 110 of 250 after three
# all-dup pages, with fresh cards still beyond). Allow enough dupe
# pages to cover the remainder — CAPPED, because the counter also
# counts textless rating-only «оценки» the list never shows, so it
# is often unattainable and must not grind the walk forever.
_DUP_PAGES_MAX_EXTENSION = 25

# DOM cards per ?page=N (measured 2026-09-18) — mirrors
# parallel_sessions.estimate_review_pages' per_page.
_CARDS_PER_PAGE = 10

# /card/<slug>/<id> (slug optional) — the current product's id,
# matched against each card's offer-info oskuId.
_CANONICAL_CARD_ID_RE = re.compile(
    r"^/card/(?:[^/]+/)?(\d+)",
)


class YandexBrowserTransport:
    """Streams batches of raw review-card dicts from the public
    Yandex.Market reviews page (see module docstring)."""

    def __init__(
        self,
        *,
        timeout_ms: int = 90_000,
        settle_ms: int = 1_500,
        debug_dir: str = "debug_yandex",
        proxy: dict[str, str] | None = None,
        cookies: list[dict[str, Any]] | None = None,
        humanize: bool = True,
        cookies_path: str | None = "yandex_cookies.json",
        seed: int | None = None,
        captcha_max_attempts: int = 5,
        scroll_step: int = 1_600,
        scroll_pause_ms: int = 700,
        scroll_max_idle_rounds: int = 5,
        page_delay_seconds: float = 1.0,
        block_assets: bool = False,
        manual_captcha: bool = True,
        manual_captcha_timeout_s: float = 180.0,
        auto_captcha_wait_s: float = 6.0,
        proxy_pool: Any | None = None,
        proxy_rotate_max_restarts: int = 3,
        start_page: int = 1,
        max_pages: int | None = None,
        dup_pages_stop: int = 3,
    ) -> None:
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self.debug_dir = Path(debug_dir)
        self.proxy = proxy
        self.cookies = cookies
        self.humanize = humanize
        self.cookies_path = cookies_path
        self.seed = seed
        self.captcha_max_attempts = captcha_max_attempts
        self.scroll_step = scroll_step
        self.scroll_pause_ms = scroll_pause_ms
        self.scroll_max_idle_rounds = scroll_max_idle_rounds
        self.page_delay_seconds = page_delay_seconds
        self.block_assets = block_assets
        self.manual_captcha = manual_captcha
        self.manual_captcha_timeout_s = manual_captcha_timeout_s
        self.auto_captcha_wait_s = auto_captcha_wait_s
        self.proxy_pool = proxy_pool
        self.proxy_rotate_max_restarts = proxy_rotate_max_restarts
        #: Page-range bounds (--parallel-sessions splits, targeted
        #: re-walks): the walk starts at ``start_page`` and stops
        #: after ``max_pages`` productive pages even without an
        #: empty page to end it.
        self.start_page = max(1, start_page)
        self.max_pages = max_pages
        #: Stop after this many consecutive pages that yielded ZERO
        #: new cards (the pager wraps and re-serves old ground past
        #: the last page — an empty page never comes). One all-dup
        #: page is normal (Show-More preloads the next page's SSR
        #: cards); a streak means the end. 0 = walk to the bitter
        #: end (empty page / max_pages) — the old, slow behaviour.
        self.dup_pages_stop = max(0, int(dup_pages_stop))

        self.last_total_count: int | None = None
        self.last_average_rating: float | None = None
        #: Product name from the JSON-LD ``Product`` block (the
        #: unified output's ``product_title``).
        self.last_product_name: str | None = None
        self.captcha_hits = 0

        self._first_warmup_done = False
        self._last_card_url: str | None = None
        self._pacer: AdaptivePacer | None = None
        #: Product id of the walk's canonical card path — the
        #: offer-id (oskuId) filter's reference (set after warmup).
        self._current_product_id: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def iter_review_batches(
        self,
        product_url: str,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Yield batches of NEW review-card dicts, one batch per
        reviews page (``?page=N``), until a page adds no new cards,
        the ``max_pages`` range runs out, or a captcha kills the
        run.

        When a captcha survives the whole escalation ladder and a
        ``proxy_pool`` is available, the browser is RESTARTED with
        the next proxy and a FRESH cookie jar (a jar that already
        saw a challenge is a liability on a new IP) — up to
        ``proxy_rotate_max_restarts`` times.
        """
        card_path = extract_yandex_market_card_path(product_url)

        browser_cls = _import_invisible_playwright()
        self.debug_dir.mkdir(parents=True, exist_ok=True)

        restarts = 0
        state: dict[str, Any] = {
            "seen": set(), "page_no": self.start_page,
            "dup_pages": 0,
        }
        while True:
            # A restart rotates the egress IP; cookies are NOT
            # carried over on rotation (see docstring). On the
            # first launch self.cookies is the user-supplied jar.
            restart_cookies = None if restarts else self.cookies
            try:
                async for batch in self._iter_with_browser(
                    browser_cls, card_path, restart_cookies, state,
                ):
                    yield batch
                return
            except YandexCaptchaError as exc:
                restarts += 1
                if (
                    self.proxy_pool is None
                    or restarts > self.proxy_rotate_max_restarts
                ):
                    raise
                proxy = await self._next_proxy()
                if proxy is None:
                    raise YandexCaptchaError(
                        f"{exc} (proxy pool exhausted)"
                    ) from exc
                print(
                    f"Я.Маркет: капча пережила эскалацию — "
                    f"рестарт браузера с новым proxy "
                    f"({proxy.get('server', '?')}), "
                    f"попытка {restarts}/"
                    f"{self.proxy_rotate_max_restarts}"
                )

    async def _next_proxy(self) -> dict[str, str] | None:
        """Pull the next proxy from the pool (async-aware)."""
        if self.proxy_pool is None:
            return None
        try:
            pool_next = getattr(self.proxy_pool, "next_async", None)
            if pool_next is not None:
                return await pool_next()
            return self.proxy_pool.next()
        except Exception:
            return None

    async def fetch_total_count(
        self,
        product_url: str,
    ) -> int | None:
        """One-shot probe of the review counter — no walk, no cards.

        Warms up on the card page, opens reviews page 1 and reads
        the JSON-LD ``aggregateRating.reviewCount`` through the
        SAME captcha ladder as the walk (:meth:`_goto_reviews`).
        The CLI uses this to size ``--parallel-sessions`` page
        ranges before launching the sessions. Returns ``None``
        when the healthy page carries no counter; raises the
        ladder's errors when the page stays challenged.
        """
        card_path = extract_yandex_market_card_path(product_url)
        browser_cls = _import_invisible_playwright()

        proxy = self.proxy
        if proxy is None and self.proxy_pool is not None:
            proxy = await self._next_proxy()

        async with browser_cls(
            proxy=proxy,
            seed=self.seed,
            humanize=self.humanize,
        ) as browser:
            page = await browser.new_page()
            try:
                if self.cookies:
                    try:
                        await page.context.add_cookies(self.cookies)
                    except Exception as exc:
                        print(
                            "Я.Маркет: не удалось внедрить cookies: "
                            f"{exc}"
                        )
                await self._install_stealth(page)
                await self._install_resource_blocker(page)

                canonical = await self._warmup(page, card_path)
                self._set_current_product(canonical)
                url = (
                    f"{YANDEX_MARKET_BASE}{canonical}/reviews?page=1"
                )
                snapshot = await self._goto_reviews(
                    page,
                    url,
                    referer=self._last_card_url,
                    page_no=1,
                )
                await self._save_cookies(page)
                self._update_totals(snapshot)
                return self.last_total_count
            finally:
                await self._save_cookies(page)
                try:
                    await page.close()
                except Exception:
                    pass

    async def _iter_with_browser(
        self,
        browser_cls: type,
        card_path: str,
        cookies: list[dict[str, Any]] | None,
        state: dict[str, Any],
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """One browser session's walk over the reviews pages.

        ``state`` carries ``seen`` / ``page_no`` across restarts so
        a proxy rotation resumes where the captcha killed the run
        instead of re-walking from page 1.
        """
        proxy = self.proxy
        if proxy is None and self.proxy_pool is not None:
            proxy = await self._next_proxy()
            if proxy is not None:
                print(
                    "Я.Маркет: один proxy на сессию — "
                    f"{proxy.get('server', '?')}"
                )

        async with browser_cls(
            proxy=proxy,
            seed=self.seed,
            humanize=self.humanize,
        ) as browser:
            page = await browser.new_page()
            if cookies:
                try:
                    await page.context.add_cookies(cookies)
                except Exception as exc:
                    print(
                        "Я.Маркет: не удалось внедрить cookies: "
                        f"{exc}"
                    )
            await self._install_stealth(page)
            await self._install_resource_blocker(page)

            pacer = AdaptivePacer(
                base_delay=self.page_delay_seconds,
            )
            self._pacer = pacer

            try:
                canonical = await self._warmup(page, card_path)
                self._set_current_product(canonical)

                seen: set[str] = state["seen"]
                page_no: int = state["page_no"]
                dup_pages: int = state.get("dup_pages", 0)
                first_dump_done = state.get("first_dump_done", False)
                # Cards of page N-1 held back until page N is read:
                # the JSON-LD block paginates independently of the
                # DOM (measured 2026-09-18: page 2's LD carries the
                # ratings for four page-1 cards), so a batch gets
                # the NEXT page's ratings merged in before it is
                # yielded. Survives a proxy-rotation restart.
                pending: list[dict[str, Any]] = (
                    list(state.get("pending") or [])
                )
                rating_store: dict[str, Any] = (
                    state.get("rating_store") or {}
                )
                state["rating_store"] = rating_store

                if pending:
                    yield pending
                    state["pending"] = []

                while True:
                    if (
                        self.max_pages is not None
                        and page_no - self.start_page
                        >= self.max_pages
                    ):
                        # Range exhausted without an empty page:
                        # flush the held-back batch — it only
                        # forgoes the NEXT page's LD ratings,
                        # which will never arrive.
                        if pending:
                            self._apply_rating_store(
                                pending, rating_store,
                            )
                            pacer.record_success()
                            yield pending
                            state["pending"] = []
                        return
                    url = (
                        f"{YANDEX_MARKET_BASE}{canonical}/reviews"
                        f"?page={page_no}"
                    )
                    # Referer chain: page N was «opened from» page
                    # N-1 — every page coming from the CARD would
                    # read as direct hits on deep links. Page 1 (and
                    # a parallel session's cold start) keeps the
                    # card referer.
                    referer = (
                        f"{YANDEX_MARKET_BASE}{canonical}/reviews"
                        f"?page={page_no - 1}"
                        if page_no > 1
                        else self._last_card_url
                    )
                    snapshot = await self._goto_reviews(
                        page, url, referer=referer, page_no=page_no,
                    )
                    # The session just proved itself healthy —
                    # checkpoint the (possibly captcha-warmed) cookies NOW,
                    # not at exit: an interrupted run must keep
                    # them.
                    await self._save_cookies(page)

                    # Within one page: read the initial cards, then
                    # EXPAND the list — Show-More clicks AND
                    # realistic downward scrolls (wheel events wake
                    # the intersection-observer lazy loads; one
                    # evaluate read per round absorbs whatever
                    # arrived). The loop runs while cards keep
                    # ARRIVING; re-reads are free because
                    # _absorb_new_cards drops every card already
                    # claimed by an earlier round or page (the
                    # per-card seen-key dedup).
                    page_cards: list[dict[str, Any]] = []

                    if not first_dump_done:
                        # The healthy snapshot skips page.content()
                        # (see _read_cards) — fetch it only for the
                        # one-time debug dump.
                        await self._fill_html(page, snapshot)
                        self._dump_html(
                            "reviews_page", snapshot["html"],
                        )
                        first_dump_done = True
                    self._update_totals(snapshot)

                    # Initial read: absorb the not-yet-seen cards.
                    self._absorb_new_cards(
                        snapshot.get("cards") or [],
                        seen,
                        page_cards,
                    )

                    # Expansion rounds: a Show-More click when the
                    # AB variant paginates with a button, otherwise
                    # 1-3 realistic scroll strides (the first round
                    # takes one stride — on a page with no lazy
                    # loading it just checks the bottom and stops).
                    # A page with ZERO cards has nothing to expand —
                    # the empty-page check below ends the walk.
                    idle_rounds = 0
                    rounds = 0
                    idle_limit = max(1, self.scroll_max_idle_rounds)
                    while (
                        snapshot.get("cards")
                        and idle_rounds < idle_limit
                        and rounds < _EXPAND_ROUNDS_HARD_CAP
                    ):
                        rounds += 1
                        clicked = await self._click_show_more(page)
                        if clicked:
                            await page.wait_for_timeout(
                                random.randint(800, 1500)
                            )
                        else:
                            strides = (
                                1 if rounds == 1
                                else random.randint(2, 3)
                            )
                            for _stride in range(strides):
                                await self._scroll_once(page)
                        snapshot = await self._read_cards(page)
                        fresh = self._absorb_new_cards(
                            snapshot.get("cards") or [],
                            seen,
                            page_cards,
                        )
                        if fresh:
                            # Cards are still arriving — keep
                            # expanding this page.
                            idle_rounds = 0
                            continue
                        if clicked:
                            # The button no longer expands anything
                            # — no more Show-More on this page.
                            break
                        idle_rounds += 1
                        if (
                            snapshot.get("scroll_bottom_gap") or 0
                        ) <= _BOTTOM_GAP_EPSILON_PX:
                            # At the document bottom with nothing
                            # new arriving: nothing left to
                            # lazy-load — skip the idle budget.
                            break

                    # Accumulate this page's LD ratings, then let
                    # them backfill the held-back batch.
                    self._merge_rating_store(
                        rating_store,
                        snapshot.get("ld_reviews") or [],
                    )

                    # Check if there are ANY cards on the page
                    # (regardless of whether they're new)
                    total_cards_on_page = len(snapshot.get("cards") or [])

                    # Stop when the page has NO cards at all (empty
                    # page = end of list) OR when dup_pages_stop pages
                    # in a row added zero new cards: past the last
                    # page Yandex re-serves old ground instead of an
                    # empty page, so without the streak counter the
                    # walk grinds through duplicate pages forever.
                    if total_cards_on_page == 0:
                        print(f"Я.Маркет: страница {page_no} пуста, конец списка")
                        # Debug-first: the walk-ending empty page is
                        # worth a postmortem dump (the healthy read
                        # skipped page.content()).
                        await self._fill_html(page, snapshot)
                        self._dump_html(
                            f"empty_page_{page_no}",
                            snapshot["html"],
                        )
                        if pending:
                            self._apply_rating_store(
                                pending, rating_store,
                            )
                            pacer.record_success()
                            yield pending
                            state["pending"] = []
                        return

                    if page_cards:
                        dup_pages = 0
                    else:
                        dup_pages += 1
                        # Total-aware tolerance: while the counter
                        # says reviews remain, extend the dupe
                        # budget enough to walk over the preloaded
                        # / re-served windows (capped — see
                        # _DUP_PAGES_MAX_EXTENSION).
                        total = self.last_total_count
                        if total and len(seen) < total:
                            remaining_pages = -(
                                -(
                                    total - len(seen)
                                ) // _CARDS_PER_PAGE
                            )
                            effective_stop = (
                                self.dup_pages_stop
                                + min(
                                    remaining_pages,
                                    _DUP_PAGES_MAX_EXTENSION,
                                )
                            )
                        else:
                            effective_stop = self.dup_pages_stop
                        if (
                            self.dup_pages_stop > 0
                            and dup_pages >= effective_stop
                        ):
                            verdict = (
                                f"; по счётчику сайта "
                                f"{len(seen)}/{total} — остаток, "
                                f"вероятно, безтекстовые оценки "
                                f"либо пейджер больше не отдаёт"
                                if total and len(seen) < total
                                else ""
                            )
                            print(
                                f"Я.Маркет: {dup_pages} страниц подряд "
                                f"без новых отзывов (последняя — "
                                f"{page_no}) — конец списка; всего "
                                f"уникальных карточек: {len(seen)}"
                                f"{verdict}"
                            )
                            if pending:
                                self._apply_rating_store(
                                    pending, rating_store,
                                )
                                pacer.record_success()
                                yield pending
                                state["pending"] = []
                            return
                        if dup_pages == 1:
                            # One line per streak, not per page —
                            # the old per-page spam is what made the
                            # run look stuck.
                            if self.dup_pages_stop > 0 and total:
                                if len(seen) < total:
                                    print(
                                        f"Я.Маркет: страница "
                                        f"{page_no}: "
                                        f"{total_cards_on_page} "
                                        f"отзывов, все уже собраны; "
                                        f"по счётчику сайта "
                                        f"{len(seen)}/{total} — "
                                        f"иду дальше, лимит "
                                        f"{effective_stop} таких "
                                        f"страниц подряд"
                                    )
                                else:
                                    print(
                                        f"Я.Маркет: страница "
                                        f"{page_no}: "
                                        f"{total_cards_on_page} "
                                        f"отзывов, все уже собраны "
                                        f"(счётчик сайта "
                                        f"{len(seen)}/{total}); "
                                        f"остановка после "
                                        f"{self.dup_pages_stop} "
                                        f"таких страниц подряд"
                                    )
                            elif self.dup_pages_stop > 0:
                                print(
                                    f"Я.Маркет: страница {page_no}: "
                                    f"{total_cards_on_page} отзывов, "
                                    f"все уже собраны; остановка после "
                                    f"{self.dup_pages_stop} таких "
                                    f"страниц подряд"
                                )
                            else:
                                print(
                                    f"Я.Маркет: страница {page_no}: "
                                    f"{total_cards_on_page} отзывов, "
                                    f"все уже собраны (дубликаты), "
                                    f"продолжаем"
                                )
                        elif (
                            dup_pages % 5 == 0
                            and self.dup_pages_stop > 0
                        ):
                            # Progress line on long (extended)
                            # streaks so the run never looks stuck.
                            counter = (
                                f"/{total}" if total else ""
                            )
                            print(
                                f"Я.Маркет: страница {page_no}: "
                                f"дубликаты ({dup_pages} подряд); "
                                f"собрано {len(seen)}{counter}, "
                                f"лимит {effective_stop}"
                            )

                    if pending:
                        self._apply_rating_store(
                            pending, rating_store,
                        )
                        pacer.record_success()
                        yield pending

                    pending = page_cards
                    state["pending"] = pending
                    page_no += 1
                    state["page_no"] = page_no
                    state["dup_pages"] = dup_pages
                    state["first_dump_done"] = first_dump_done
                    await pacer.wait()
            finally:
                await self._save_cookies(page)
                try:
                    await page.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Navigation, warmup, captcha
    # ------------------------------------------------------------------

    async def _warmup(self, page: Any, card_path: str) -> str:
        """Land on the product card first: referer + cookies for the
        reviews navigation, and a human-like reading pause.

        Returns the CANONICAL card path: the card page redirects a
        non-canonical slug (``/card/<wrong-slug>/<id>``) to the
        canonical one, while ``/reviews`` with a wrong slug just
        404s — so the reviews URLs must be built from where the
        card page ACTUALLY landed, not from the user's input.
        """
        url = f"{YANDEX_MARKET_BASE}{card_path}"
        try:
            await page.goto(url, timeout=self.timeout_ms)
        except Exception as exc:
            print(f"[warn] Я.Маркет: warmup не удался: {exc}")
            return card_path

        # Capture the post-redirect URL and rebuild the card path.
        landed = str(getattr(page, "url", "") or "")
        canonical = card_path
        match = re.match(
            r"https?://[^/]+(/card/[^/]+/\d+)",
            landed,
        )
        if match:
            canonical = match.group(1)
            if canonical != card_path:
                print(
                    "Я.Маркет: канонический путь товара — "
                    f"{canonical}"
                )
        self._last_card_url = (
            f"{YANDEX_MARKET_BASE}{canonical}"
        )

        # A brief read of the card: 2-3 wheel nudges with realistic
        # mouse movements and pauses. A user who lands on a product
        # and INSTANTLY jumps to reviews is a thinner pattern than
        # one who scrolls naturally and reads.
        await self._simulate_reading_behavior(page)

        pause_s = (
            random.uniform(0.8, 2.2)
            if not self._first_warmup_done
            else random.uniform(0.4, 1.0)
        )
        self._first_warmup_done = True
        await page.wait_for_timeout(int(pause_s * 1000))
        return canonical

    async def _simulate_reading_behavior(self, page: Any) -> None:
        """Simulate realistic human reading: random mouse movements,
        scrolling with pauses, occasional micro-scrolls back up.
        
        This pattern breaks bot detection that looks for mechanical
        scrolling without cursor activity."""
        viewport = page.viewport_size
        width = viewport.get("width", 1280) if viewport else 1280
        height = viewport.get("height", 720) if viewport else 720
        
        # Initial mouse movement to random position
        try:
            await page.mouse.move(
                random.randint(100, width - 100),
                random.randint(100, height - 100),
            )
        except Exception:
            pass
        
        # 2-3 scroll rounds with mouse movements
        for _ in range(random.randint(2, 3)):
            # Move mouse to a new reading position before scrolling
            try:
                await page.mouse.move(
                    random.randint(200, width - 200),
                    random.randint(150, height - 150),
                )
                await page.wait_for_timeout(random.randint(120, 380))
            except Exception:
                pass
            
            # Main scroll down
            try:
                delta = random.randint(350, 1100)
                await page.mouse.wheel(0, delta)
                await page.wait_for_timeout(random.randint(400, 950))
            except Exception:
                pass
            
            # 30% chance of micro-scroll back (reading previous line)
            if random.random() < 0.3:
                try:
                    await page.mouse.wheel(0, -random.randint(50, 200))
                    await page.wait_for_timeout(random.randint(200, 500))
                except Exception:
                    pass

    async def _goto_reviews(
        self,
        page: Any,
        url: str,
        *,
        referer: str | None,
        page_no: int,
    ) -> dict[str, Any]:
        """Navigate to the reviews URL until a HEALTHY page loads.

        The FIRST navigation prefers clicking the site's own link
        (:meth:`_click_reviews_link`) — the request then carries
        the page's natural referer and context; retries (and a
        missing link) fall back to a plain goto with the chained
        referer. Returns the snapshot of the first healthy read.
        Raises :class:`YandexCaptchaError` /
        :class:`YandexSoftBlockError` when the attempt budget runs
        out (see the module docstring for the escalation ladder).
        """
        attempts = 0
        cooldown_s = 5.0
        snapshot: dict[str, Any] = {}
        tried_click = False

        while True:
            if not tried_click:
                tried_click = True
                if not await self._click_reviews_link(
                    page, page_no,
                ):
                    await page.goto(
                        url,
                        timeout=self.timeout_ms,
                        referer=referer,
                    )
            else:
                # A challenged page has no pager left to click —
                # retries goto directly.
                await page.goto(
                    url,
                    timeout=self.timeout_ms,
                    referer=referer,
                )
            # Human dwell, right-skewed: a tight ±20% band around
            # the base settle is a metronome of its own; most pages
            # read faster, some much slower («зачитался»).
            settle = self.settle_ms
            if settle > 0:
                factor = random.uniform(0.6, 1.8)
                if random.random() < 0.15:
                    factor += random.uniform(0.8, 2.2)
                await page.wait_for_timeout(
                    int(settle * factor),
                )

            snapshot = await self._read_cards(page)
            page_url = str(getattr(page, "url", "") or "")

            kind = self._classify(
                page_url, snapshot,
            )
            if kind != _PAGE_HEALTHY and not snapshot.get("html"):
                # A page without SSR data can only be told apart
                # (captcha shell vs 404 vs soft-block) by its
                # markup — ONLY challenged pages pay for the
                # multi-megabyte page.content() serialization.
                await self._fill_html(page, snapshot)
                kind = self._classify(page_url, snapshot)

            if kind == _PAGE_HEALTHY:
                return snapshot

            # A real 404 («Нет такой страницы»): NOT retryable —
            # re-navigating a dead URL 5 times only burns the
            # session. (The warmup already resolved the canonical
            # slug; a 404 here means the product/review page is
            # genuinely gone.)
            html = snapshot.get("html", "") or ""
            if (
                kind == _PAGE_DEGRADED
                and any(
                    marker in html
                    for marker in _NOT_FOUND_HTML_MARKERS
                )
            ):
                self._dump_html("not_found", html)
                raise YandexNotFoundError(
                    "Я.Маркет: страница не найдена (404): "
                    f"{url}"
                )

            # --- challenged page --------------------------------
            attempts += 1
            self.captcha_hits += 1
            self._notify_block()
            self._dump_html(
                f"{'captcha' if kind == _PAGE_CAPTCHA else 'degraded'}"
                f"_{attempts}",
                snapshot.get("html", ""),
            )
            print(
                f"Я.Маркет: "
                f"{'капча' if kind == _PAGE_CAPTCHA else 'софт-блок'}"
                f" (попытка {attempts}/"
                f"{self.captcha_max_attempts}): {url}"
            )

            if kind == _PAGE_CAPTCHA:
                # (a) auto-wait: the inline shell often resolves
                # itself for a trusted fingerprint.
                snapshot = await self._wait_auto(page, url)
                if snapshot is not None:
                    return snapshot

                # (b) programmatic checkbox click — humanize=True
                # drives the cursor along a realistic trajectory.
                if await self._try_click_captcha_checkbox(page):
                    snapshot = await self._wait_auto(page, url)
                    if snapshot is not None:
                        await self._save_cookies(page)
                        print(
                            "Я.Маркет: капча пройдена кликом, "
                            "cookies сохранены"
                        )
                        return snapshot

                # (c) manual solve in the visible browser window.
                snapshot = await self._wait_manual(page, url)
                if snapshot is not None:
                    await self._save_cookies(page)
                    print(
                        "Я.Маркет: капча пройдена вручную, cookies "
                        "сохранены"
                    )
                    return snapshot

            if attempts >= self.captcha_max_attempts:
                if kind == _PAGE_CAPTCHA:
                    raise YandexCaptchaError(
                        "Я.Маркет: капча не пройдена за "
                        f"{attempts} попыток: {url}"
                    )
                raise YandexSoftBlockError(
                    "Я.Маркет: софт-блок (страницы без SSR-данных) "
                    f"после {attempts} попыток: {url}"
                )

            delay_s = cooldown_s * random.uniform(0.7, 1.3)
            print(
                f"Я.Маркет: кулдаун {delay_s:.1f} с перед повтором"
            )
            await asyncio.sleep(delay_s)
            cooldown_s = min(30.0, cooldown_s * 2)

    async def _click_reviews_link(
        self,
        page: Any,
        page_no: int,
    ) -> bool:
        """Click the site's own way into page ``page_no``:

        - page 1 — goto directly (no click needed);
        - page N>1 — the pager link whose ``page`` query param is
          EXACTLY N (``page=2`` must not match the ``page=20``
          link).

        False (the caller gots with the chained referer) when the
        link is missing or the click does not navigate in time.
        
        CRITICAL: Only clicks pagination links, not arbitrary page elements."""
        if page_no <= 1:
            # First page - no click needed, just goto
            return False
        
        # Page 2+ - click only pagination links with exact page number
        return await self._click_site_link(
            page,
            href_substr="page=",
            expect_substr=f"page={page_no}",
            exact_param=("page", str(page_no)),
        )

    async def _click_site_link(
        self,
        page: Any,
        *,
        href_substr: str,
        expect_substr: str,
        exact_param: tuple[str, str] | None = None,
        timeout_ms: int = 10_000,
    ) -> bool:
        """Click the first visible ``a[href*=…]`` link and wait for
        the URL to reach ``expect_substr``.

        ``exact_param`` additionally requires the link's own query
        to carry ``(key, value)`` exactly. Any failure — no link,
        hidden link, click error, no navigation within the timeout
        — returns False so the caller can fall back to goto."""
        try:
            links = page.locator(
                f'a[href*="{href_substr}"]',
            )
            for i in range(await links.count()):
                link = links.nth(i)
                href = (
                    await link.get_attribute("href") or ""
                )
                if exact_param is not None:
                    key, value = exact_param
                    _, _, _, query, _ = urlsplit(href)
                    if (
                        dict(parse_qsl(query)).get(key)
                        != value
                    ):
                        continue
                try:
                    if not await link.is_visible():
                        continue
                except Exception:
                    pass
                await link.click(timeout=3_000)
                # A pager link occasionally rides a target=_blank
                # wrapper — close any tab it popped.
                await self._close_stray_tabs(page)
                deadline = (
                    asyncio.get_event_loop().time()
                    + timeout_ms / 1000
                )
                while (
                    asyncio.get_event_loop().time() < deadline
                ):
                    url = str(
                        getattr(page, "url", "") or "",
                    )
                    if expect_substr in url:
                        return True
                    await page.wait_for_timeout(400)
                return False
        except Exception:
            return False
        return False

    async def _wait_auto(
        self,
        page: Any,
        url: str,
    ) -> dict[str, Any] | None:
        """Give the inline SmartCaptcha shell time to auto-resolve
        (it reloads the target page itself). Returns the healthy
        snapshot or None."""
        deadline = asyncio.get_event_loop().time() + (
            self.auto_captcha_wait_s
        )
        while (
            asyncio.get_event_loop().time() < deadline
        ):
            await page.wait_for_timeout(1_000)
            snapshot = await self._read_cards(page)
            page_url = str(getattr(page, "url", "") or "")
            if self._classify(page_url, snapshot) == _PAGE_HEALTHY:
                return snapshot
        return None

    # SmartCaptcha checkbox selectors — ONLY for actual captcha frames.
    # These selectors are tried ONLY inside iframes or when captcha
    # markers are present in the page HTML to avoid clicking random
    # checkboxes (filters, settings, etc).
    _CAPTCHA_CHECKBOX_SELECTORS = (
        "input[type=checkbox]",
        ".Checkbox",
        "[class*=Checkbox]",
        "[class*=Captcha] input",
        "[class*=captcha] input",
        "label",
    )

    async def _try_click_captcha_checkbox(
        self,
        page: Any,
    ) -> bool:
        """Try to click the «Подтвердите, что вы не робот» checkbox
        programmatically.

        With ``humanize=True`` the click moves the cursor along a
        realistic human trajectory — exactly what SmartCaptcha's
        behavioural model wants to see. Returns True when a click
        landed (the caller re-checks page health afterwards).
        
        SAFETY: Only clicks checkboxes in iframes OR when explicit
        captcha markers are present in page HTML. This prevents
        accidentally clicking filters, sort options, etc.
        """
        # First, check if this is actually a captcha page
        try:
            html = await page.content()
        except Exception:
            html = ""
        
        has_captcha_markers = any(
            marker in html.lower()
            for marker in (
                "smartcaptcha", "captcha_smart", "showcaptcha",
                "checkcaptcha", "подтвердите, что вы не робот",
                "докажите, что вы не робот",
            )
        )
        
        # Try iframes first (captcha often lives there)
        frames = []
        try:
            extra = page.frames or []
            if extra:
                # ONLY try frames, not the main page yet
                frames.extend(extra)
        except Exception:
            pass

        for frame in frames:
            for selector in self._CAPTCHA_CHECKBOX_SELECTORS:
                try:
                    locator = frame.locator(selector).first
                    if not await locator.count():
                        continue
                    if not await locator.is_visible():
                        continue
                    await locator.click(timeout=5_000)
                    print(
                        "Я.Маркет: чекбокс капчи кликнут в iframe "
                        "(humanize-траектория)"
                    )
                    return True
                except Exception:
                    continue
        
        # If no iframe checkbox worked AND we have explicit captcha
        # markers, try the main page (inline captcha)
        if has_captcha_markers:
            for selector in self._CAPTCHA_CHECKBOX_SELECTORS:
                try:
                    locator = page.locator(selector).first
                    if not await locator.count():
                        continue
                    if not await locator.is_visible():
                        continue
                    await locator.click(timeout=5_000)
                    print(
                        "Я.Маркет: чекбокс капчи кликнут на главной странице "
                        "(humanize-траектория)"
                    )
                    return True
                except Exception:
                    continue
        
        return False

    async def _wait_manual(
        self,
        page: Any,
        url: str,
    ) -> dict[str, Any] | None:
        """Prompt the human to solve the captcha in the (visible)
        browser window and poll until the page turns healthy.

        Disabled (``manual_captcha=False``) or timed out → None.
        """
        if not self.manual_captcha:
            return None

        print(
            "\n"
            "================================================\n"
            "  Я.Маркет: СМАРТКАПЧА. Откройте окно браузера и\n"
            "  пройдите проверку — сбор продолжится сам.\n"
            f"  Ждём до {self.manual_captcha_timeout_s:.0f} с…\n"
            "================================================"
        )
        deadline = asyncio.get_event_loop().time() + (
            self.manual_captcha_timeout_s
        )
        while (
            asyncio.get_event_loop().time() < deadline
        ):
            await page.wait_for_timeout(1_500)
            snapshot = await self._read_cards(page)
            page_url = str(getattr(page, "url", "") or "")
            if self._classify(page_url, snapshot) == _PAGE_HEALTHY:
                return snapshot
        print("Я.Маркет: ручное решение не дождались — ретраи")
        return None

    def _classify(
        self,
        page_url: str,
        snapshot: dict[str, Any],
    ) -> str:
        """Classify a page read as healthy / captcha / degraded.

        The decisive signal is SSR data: a captcha shell (redirect
        or inline) NEVER contains review cards, JSON-LD product
        reviews or the aggregate counter. So captcha markers are
        only trusted on a page WITHOUT SSR data — a healthy
        megabyte page may legitimately mention «SmartCaptcha» in
        its own scripts (the Ozon README's "antibot" lesson).
        """
        page_url = page_url.lower()
        if any(
            marker in page_url
            for marker in _CAPTCHA_URL_MARKERS
        ):
            return _PAGE_CAPTCHA

        html = snapshot.get("html", "") or ""
        has_ssr_data = bool(
            snapshot.get("cards")
            or snapshot.get("ld_reviews")
            or snapshot.get("total_count") is not None
        )

        if has_ssr_data:
            return _PAGE_HEALTHY

        # No SSR data: a shell of some kind. Which flavour?
        if any(
            marker in html
            for marker in _CAPTCHA_HTML_MARKERS
        ):
            return _PAGE_CAPTCHA
        body_len = int(snapshot.get("body_len") or 0)
        if (
            any(
                marker in html
                for marker in _CAPTCHA_SHELL_HTML_MARKERS
            )
            and body_len < _SHELL_BODY_MAX_LEN
        ):
            return _PAGE_CAPTCHA

        # No captcha markers and no SSR data: a markerless
        # degraded shell (soft block).
        return _PAGE_DEGRADED

    def _notify_block(self) -> None:
        if self._pacer is not None:
            self._pacer.record_block()

    # ------------------------------------------------------------------
    # Card reading and scroll
    # ------------------------------------------------------------------

    async def _read_cards(
        self,
        page: Any,
        *,
        include_html: bool = False,
    ) -> dict[str, Any]:
        """Single evaluate round-trip + JSON-LD rating merge +
        own/feed card split.

        ``include_html=False`` (the default) skips ``page.content()``:
        serializing a multi-megabyte healthy page just to classify
        it as healthy is pure overhead on the hot path (the same
        lesson as Ozon's one-evaluate-per-page read). The snapshot
        then carries ``html=None``; callers that need the markup
        (challenged-page classification, debug dumps) fill it via
        :meth:`_fill_html`.
        """
        try:
            raw = await page.evaluate(_READ_CARDS_JS)
        except Exception:
            raw = None
        html = ""
        if include_html:
            try:
                html = await page.content()
            except Exception:
                html = ""

        if not isinstance(raw, dict):
            raw = {}

        cards = [
            card
            for card in raw.get("cards") or []
            if isinstance(card, dict)
        ]
        ld_reviews = [
            item
            for item in raw.get("ld_reviews") or []
            if isinstance(item, dict)
        ]

        self._merge_ld_ratings(cards, ld_reviews)
        cards = self._filter_own_cards(
            cards,
            ld_reviews,
            getattr(self, "_current_product_id", None),
        )

        if not cards and ld_reviews:
            # Layout-drift fallback: the DOM selectors found
            # nothing, but the JSON-LD block still carries the
            # product's own reviews.
            cards = [
                {
                    "uuid": None,
                    "author": item.get("author"),
                    "date": item.get("date"),
                    "text": item.get("text"),
                    "rating": item.get("rating"),
                    "pros": None,
                    "cons": None,
                    "photos": [],
                }
                for item in ld_reviews
            ]

        return {
            "cards": cards,
            "ld_reviews": ld_reviews,
            "total_count": raw.get("total_count"),
            "average_rating": raw.get("average_rating"),
            "product_name": raw.get("product_name"),
            "body_len": raw.get("body_len") or len(html),
            "scroll_bottom_gap": raw.get("scroll_bottom_gap"),
            "html": html or None,
        }

    async def _fill_html(
        self,
        page: Any,
        snapshot: dict[str, Any],
    ) -> None:
        """Fetch the page markup into a snapshot that was read
        without it (:meth:`_read_cards` skips ``page.content()`` on
        the hot path — only challenged pages and debug dumps pay
        for the serialization)."""
        try:
            html = await page.content()
        except Exception:
            html = ""
        snapshot["html"] = html or None
        if html and not snapshot.get("body_len"):
            snapshot["body_len"] = len(html)

    @staticmethod
    def _absorb_new_cards(
        cards: list[dict[str, Any]],
        seen: set[str],
        sink: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Append the not-yet-seen ``cards`` to ``sink``, claim
        their keys in ``seen`` (ONE key computation per card) and
        return the freshly absorbed ones."""
        fresh: list[dict[str, Any]] = []
        for card in cards:
            key = YandexBrowserTransport._card_key(card)
            if key in seen:
                continue
            seen.add(key)
            fresh.append(card)
        sink.extend(fresh)
        return fresh

    @staticmethod
    def _merge_rating_store(
        store: dict[str, Any],
        ld_reviews: list[dict[str, Any]],
    ) -> None:
        """Accumulate JSON-LD (author, ISO-date) → rating pairs seen
        on ANY page — LD pagination is offset from the DOM one, so
        page N+1 carries ratings for page N's cards."""
        from infrastructure.marketplaces.yandex import (
            parse_yandex_date,
        )

        for item in ld_reviews:
            rating = item.get("rating")
            if rating is None:
                continue
            created = parse_yandex_date(item.get("date"))
            iso = created.date().isoformat() if created else ""
            if not iso:
                continue
            store[f"{item.get('author') or ''}|{iso}"] = rating

    @staticmethod
    def _apply_rating_store(
        cards: list[dict[str, Any]],
        store: dict[str, Any],
    ) -> None:
        """Backfill ratings into cards from the accumulated store."""
        from infrastructure.marketplaces.yandex import (
            parse_yandex_date,
        )

        for card in cards:
            if card.get("rating") is not None:
                continue
            created = parse_yandex_date(card.get("date"))
            iso = created.date().isoformat() if created else ""
            key = f"{card.get('author') or ''}|{iso}"
            if key in store:
                card["rating"] = store[key]

    @staticmethod
    def _merge_ld_ratings(
        cards: list[dict[str, Any]],
        ld_reviews: list[dict[str, Any]],
    ) -> None:
        """DOM cards carry no rating (filled/empty stars differ only
        by obfuscated CSS classes); the JSON-LD reviews do. Match by
        (author, ISO-date) — JSON-LD ships ``2024-01-28`` while the
        DOM card ships «28 января 2024»."""
        from infrastructure.marketplaces.yandex import (
            parse_yandex_date,
        )

        ratings: dict[str, Any] = {}
        for item in ld_reviews:
            rating = item.get("rating")
            if rating is None:
                continue
            key = (
                f"{item.get('author') or ''}"
                f"|{item.get('date') or ''}"
            )
            ratings[key] = rating

        for card in cards:
            if card.get("rating") is not None:
                continue
            created = parse_yandex_date(card.get("date"))
            iso = created.date().isoformat() if created else ""
            key = f"{card.get('author') or ''}|{iso}"
            if key in ratings:
                card["rating"] = ratings[key]

    def _set_current_product(self, canonical: str) -> None:
        """Remember the walk's product id (from the canonical card
        path) — the reference for the offer-id feed filter."""
        match = _CANONICAL_CARD_ID_RE.match(canonical or "")
        self._current_product_id = (
            match.group(1) if match else None
        )

    @staticmethod
    def _filter_own_cards(
        cards: list[dict[str, Any]],
        ld_reviews: list[dict[str, Any]],
        product_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Drop cross-product feed cards and seller responses.

        The PRIMARY discriminator (measured 2026-09-19) is the
        offer-info chip's ``oskuId`` (``data-zone-data`` on
        ``ugc-element-offer-info``): own cards carry the open
        product's id, cross-product FEED cards a foreign one — the
        scroll expansion reaches the feed section, and without this
        rule a run collected 504 cards for a 250-review product.
        Card without a readable oskuId fall back to the legacy
        heuristics:

        1. It matches a JSON-LD review by (author, ISO-date) — JSON-LD
           describes ONLY the open product's reviews
        2. OR it has UUID (DOM review ID) — these are always product-specific
        3. OR it has content (text/pros/cons) — accept most reviews by default
        4. OR it has author + any date string — real review pattern

        EXCLUDE seller responses (official answers from the store).
        """
        from infrastructure.marketplaces.yandex import (
            parse_yandex_date,
        )

        ld_keys = {
            f"{item.get('author') or ''}"
            f"|{item.get('date') or ''}"
            for item in ld_reviews
        }

        own: list[dict[str, Any]] = []
        for card in cards:
            # Skip seller responses / official answers
            author = str(card.get("author") or "").lower()
            if any(marker in author for marker in (
                "официальный ответ", "ответ продавца",
                "ответ магазина", "представитель",
            )):
                continue

            # Offer-id rule: a readable oskuId is authoritative —
            # the open product's id means OWN, any other id means
            # a cross-product feed card.
            offer_id = str(card.get("offer_id") or "")
            if offer_id and product_id:
                if offer_id != product_id:
                    continue
                own.append(card)
                continue

            # Match by JSON-LD key (strongest legacy signal)
            created = parse_yandex_date(card.get("date"))
            iso = created.date().isoformat() if created else ""
            key = f"{card.get('author') or ''}|{iso}"
            if key in ld_keys:
                own.append(card)
                continue

            # Cards with UUID are always product-specific
            uuid = card.get("uuid")
            if uuid and str(uuid).strip():
                own.append(card)
                continue

            # ACCEPT if has any meaningful content
            has_content = any(
                card.get(field)
                for field in ("text", "pros", "cons")
            )
            if has_content:
                own.append(card)
                continue
            
            # ACCEPT if has author + date string (even unparseable)
            has_author = bool(card.get("author"))
            has_date_string = bool(card.get("date"))
            if has_author and has_date_string:
                own.append(card)
                continue
            
            # ACCEPT if has rating (indicates real review interaction)
            if card.get("rating") is not None:
                own.append(card)

        return own

    async def _scroll_once(self, page: Any) -> None:
        """Realistic scroll with variable speed, micro-pauses,
        occasional reverse nudges and smooth acceleration.
        
        Yandex's SmartCaptcha tracks scroll velocity patterns:
        - Instant 1600px jumps = bot
        - Smooth acceleration/deceleration = human
        - Occasional reverse scrolls = engaged user
        - Micro-pauses mid-scroll = reading
        """
        step = self.scroll_step
        if step > 0:
            # Variable stride with occasional reverse nudge
            delta = random.randint(
                int(step * 0.5), int(step * 1.4),
            )
            
            # 15% chance of reverse scroll (user scrolled too far)
            if random.random() < 0.15:
                delta = -random.randint(120, 420)
            
            # Split large scrolls into 2-4 smaller steps with
            # micro-pauses (simulates reading while scrolling)
            if abs(delta) > 600:
                num_steps = random.randint(2, 4)
                step_size = delta / num_steps
                for i in range(num_steps):
                    try:
                        # Add jitter to each micro-step
                        jittered_step = int(
                            step_size * random.uniform(0.8, 1.2)
                        )
                        await page.mouse.wheel(0, jittered_step)
                        # Micro-pause between steps (20-80ms)
                        if i < num_steps - 1:
                            await page.wait_for_timeout(
                                random.randint(20, 80)
                            )
                    except Exception:
                        pass
            else:
                # Small scroll in one go
                try:
                    await page.mouse.wheel(0, delta)
                except Exception:
                    pass
        else:
            delta = step
            try:
                await page.mouse.wheel(0, delta)
            except Exception:
                pass
        
        # Main pause after scroll (variable, right-skewed)
        if self.scroll_pause_ms > 0:
            pause = int(
                self.scroll_pause_ms
                * random.uniform(0.5, 1.6)
            )
            # 10% chance of longer "reading" pause
            if random.random() < 0.10:
                pause = int(pause * random.uniform(1.8, 3.2))
            try:
                await page.wait_for_timeout(pause)
            except Exception:
                pass

    async def _click_show_more(self, page: Any) -> bool:
        """Some AB variants paginate with a button instead of
        lazy-append; click it if present.

        A click sometimes pops a STRAY TAB (an ad or a
        ``target=_blank`` wrapper riding the button) — they are
        closed right away (see :meth:`_close_stray_tabs`) so the
        visible window keeps only the walk page."""
        for selector in _SHOW_MORE_SELECTORS:
            try:
                locator = page.locator(selector)
                if await locator.count():
                    await locator.first.click()
                    await page.wait_for_timeout(800)
                    await self._close_stray_tabs(page)
                    return True
            except Exception:
                continue
        return False

    async def _close_stray_tabs(self, page: Any) -> None:
        """Close every tab of the context EXCEPT the walk page.

        Show-More / pager clicks occasionally open extra tabs (ads,
        ``target=_blank`` wrappers); they clutter the visible
        browser window (the human solving a captcha sees them) and
        waste resources. Best-effort: any failure is ignored."""
        try:
            tabs = list(page.context.pages)
        except Exception:
            return
        for tab in tabs:
            if tab is page:
                continue
            try:
                await tab.close()
            except Exception:
                pass

    @staticmethod
    def _card_key(card: dict[str, Any]) -> str:
        uuid = card.get("uuid")
        if uuid:
            return str(uuid)
        return "|".join(
            (
                str(card.get("author") or ""),
                str(card.get("date") or ""),
                str(card.get("rating") or ""),
                str(card.get("text") or "")[:80],
            )
        )

    def _update_totals(self, snapshot: dict[str, Any]) -> None:
        total = _parse_int(snapshot.get("total_count"))
        if total is not None:
            self.last_total_count = total
        avg = _parse_float(snapshot.get("average_rating"))
        if avg is not None:
            self.last_average_rating = avg
        name = snapshot.get("product_name")
        if isinstance(name, str) and name.strip():
            self.last_product_name = name.strip()

    # ------------------------------------------------------------------
    # Browser plumbing
    # ------------------------------------------------------------------

    async def _install_stealth(self, page: Any) -> None:
        """The same stealth init script the Ozon transports use:
        patches navigator.webdriver, plugins, languages and the
        other classic headless tells before any site JS runs."""
        from infrastructure.transports.browser_common import (
            _STEALTH_INIT_SCRIPT,
        )
        try:
            await page.add_init_script(_STEALTH_INIT_SCRIPT)
        except Exception:
            # Some builds do not support add_init_script; the
            # invisible-playwright fingerprint still carries most
            # of the stealth load.
            pass

    async def _install_resource_blocker(self, page: Any) -> None:
        """Abort image/font/media requests when ``block_assets``.

        OFF by default for Yandex (unlike Ozon): a real browser
        loads images and fonts, and SmartCaptcha weighs exactly
        that. Opt in once the session is trusted.
        """
        from infrastructure.transports.browser_common import (
            install_resource_blocker,
        )
        await install_resource_blocker(
            page, enabled=self.block_assets,
        )

    async def _save_cookies(self, page: Any) -> None:
        """Persist the (possibly captcha-warmed) session cookies so
        the next run starts where this one ended."""
        if not self.cookies_path:
            return
        try:
            cookies = await page.context.cookies()
        except Exception:
            return
        try:
            path = Path(self.cookies_path)
            if path.parent != Path("."):
                path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(cookies, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            print(f"[warn] Я.Маркет: cookies не сохранены: {exc}")

    def _dump_html(self, name: str, html: str) -> None:
        try:
            (self.debug_dir / f"{name}.html").write_text(
                html, encoding="utf-8",
            )
        except Exception:
            pass


_INT_RE = re.compile(r"\d+")
_FLOAT_RE = re.compile(r"\d+(?:[.,]\d+)?")


def _parse_int(value: Any) -> int | None:
    """'1 234 отзыва' / '1234' / 1234 -> 1234."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    digits = _INT_RE.findall(str(value))
    if not digits:
        return None
    return int("".join(digits))


def _parse_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _FLOAT_RE.search(str(value))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


__all__ = [
    "YANDEX_MARKET_BASE",
    "YandexBrowserTransport",
    "YandexCaptchaError",
    "YandexSoftBlockError",
    "YandexNotFoundError",
]
