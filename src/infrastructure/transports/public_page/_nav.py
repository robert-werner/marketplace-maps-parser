"""Mixin for PublicPageTransport."""
from __future__ import annotations
import random
from typing import Any
import infrastructure.transports.public_page as _mod


class NavigationMixin:
    # ------------------------------------------------------------------
    # Antibot detection & human-like navigation
    # ------------------------------------------------------------------
    # Titles of Ozon/Cloudflare challenge and error pages. Real
    # review pages have titles like "304 отзыв на … OZON" — none of
    # these markers appear there.
    _ANTIBOT_TITLE_MARKERS = (
        "antibot",           # Ozon "Antibot Challenge Page"
        "нет соединения",    # Ozon «Похоже, нет соединения»
        "доступ ограничен",  # Ozon geo/ISP block page
        "cloudflare",        # Cloudflare block pages
        "just a moment",     # Cloudflare interstitial
        "attention required",
    )

    # HTML markers that NEVER appear on a real reviews page. NOTE:
    # the plain string "antibot" is NOT here — a real reviews page
    # contains it too (measured 2026-09: 6 occurrences in a page
    # that rendered 30 cards), so it cannot discriminate.
    _ANTIBOT_HTML_MARKERS = (
        "Выключите VPN",          # Cloudflare "Выключите VPN…" block
        "__cf_chl",               # Cloudflare challenge
        "cf-chl",                 # Cloudflare challenge (css/js)
        "challenge-platform",     # Cloudflare challenge script
    )

    async def _page_is_antibot(self, page) -> bool:
        """True when the loaded page is a challenge/block/error page
        rather than real content. Title first (cheap, reliable);
        fall back to unambiguous Cloudflare markers in the HTML."""
        try:
            title = (await page.title()).lower()
        except Exception:
            title = ""
        for marker in self._ANTIBOT_TITLE_MARKERS:
            if marker in title:
                return True
        try:
            html = await page.content()
        except Exception:
            return False
        return any(
            marker in html for marker in self._ANTIBOT_HTML_MARKERS
        )

    async def _warmup_goto(
        self,
        page,
        *,
        product_path: str,
        retry_async,
        retryable_errors,
        label: str,
    ) -> None:
        """Land on the product page so Ozon/Cloudflare set their
        cookies for this browser session, then pause like a human
        reading the product card. No-op when ``self.warmup`` is
        False; a failed warmup is logged and never kills the fetch.

        The pause is full-length (0.8-2.2 s) for the FIRST warmup
        of a run and shorter (0.4-1.0 s) for every subsequent one —
        a repeat visitor spends less time on the product card each
        time, and in per-page-browser modes the warmup is paid once
        per page, so the saving adds up (~1 s per rotated page).
        """
        if not self.warmup:
            return
        warmup_url = self._absolute_url(product_path)
        try:
            await retry_async(
                lambda: page.goto(
                    warmup_url,
                    wait_until="domcontentloaded",
                    timeout=self.timeout_ms,
                ),
                attempts=2,
                base_delay=2.0,
                max_delay=10.0,
                factor=2.0,
                jitter=0.3,
                retry_on=retryable_errors(),
                label=f"{label} warmup",
            )
            warmups_done = getattr(self, "_warmup_count", 0)
            self._warmup_count = warmups_done + 1
            if warmups_done == 0:
                pause_ms = random.randint(800, 2_200)
            else:
                pause_ms = random.randint(400, 1_000)
            await page.wait_for_timeout(pause_ms)
        except Exception as exc:
            print(
                f"Ozon (public): warmup не удался "
                f"({type(exc).__name__}) — иду напрямую на отзывы"
            )

    async def _goto_reviews_like_human(
        self,
        page,
        *,
        product_path: str,
        reviews_url: str,
        referer_url: str | None,
        retry_async,
        retryable_errors,
        label: str,
        goto_attempts: int = 3,
        do_warmup: bool = True,
    ) -> None:
        """Navigate to the reviews page the way a real visitor does:
        optionally warm up on the product page (``do_warmup`` — skip
        when the session was already warmed), then go to the reviews
        URL with an HTTP referer. A cold referer-less hit on
        /reviews is a strong bot signal."""
        if do_warmup:
            await self._warmup_goto(
                page,
                product_path=product_path,
                retry_async=retry_async,
                retryable_errors=retryable_errors,
                label=label,
            )

        await retry_async(
            lambda: page.goto(
                reviews_url,
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
                **({"referer": referer_url} if referer_url else {}),
            ),
            attempts=goto_attempts,
            base_delay=3.0,
            max_delay=30.0,
            factor=2.0,
            jitter=0.3,
            retry_on=retryable_errors(),
            label=label,
        )

    # ------------------------------------------------------------------
    # Page lifecycle / stealth
    # ------------------------------------------------------------------
    async def _new_page_with_stealth(self, browser):
        """Create a new page.

        NOTE: no JS stealth init script is applied here, and that
        is deliberate. Measured 2026-09-15 on this exact product
        page: WITH the playwright-stealth-style script Ozon serves
        its «Похоже, нет соединения» error page (0 cards); WITHOUT
        it the same proxy/fingerprint gets HTTP 200 and 30 review
        cards — on both the invisible-playwright (Firefox) and the
        plain Chromium engines. invisible-playwright already
        provides the real stealth (patched engine, fingerprint,
        humanized input); layering the JS patch on top only breaks
        the page's own JS (fake ``navigator.plugins`` lacks the
        PluginArray methods Ozon's code expects, and a fake
        ``window.chrome`` contradicts a Firefox engine).

        The ``stealth`` constructor flag is kept for CLI
        compatibility but no longer injects anything."""
        page = await browser.new_page()
        await self._install_resource_blocker(page)
        if self.cookies:
            # Logged-in session cookies — must land in the context
            # BEFORE the first navigation. page.context works for
            # both a BrowserContext-backed and a browser.new_page()
            # page.
            try:
                await page.context.add_cookies(self.cookies)
            except Exception as exc:
                print(
                    "Ozon (public): WARNING — не удалось подставить "
                    f"cookies ({type(exc).__name__}: {exc})"
                )
        return page

    async def _fetch_page_cards(
        self,
        *,
        reviews_url: str,
        product_path: str,
        page_number: int,
        retry_attempts: int,
    ) -> tuple[list[dict[str, Any]] | None, bool]:
        """Load one reviews page in a fresh randomized browser.

        Returns ``(cards, antibot)``:

        - ``cards`` — parsed card dicts (possibly empty when the
          page genuinely has no reviews), or ``None`` when the
          review cards never appeared.
        - ``antibot`` — True when the no-cards state looks like an
          antibot/challenge page; the caller should cool down,
          rotate the proxy and retry the page instead of treating
          it as empty.
        """
        retry_async = _mod._import_retry_async()
        retryable_errors = _mod._import_retryable_errors()

        # Fresh browser per fetch: new random fingerprint (seed
        # defaults to None → secrets.randbits(31)) and, when a
        # proxy_pool is active, the next proxy in the rotation.
        page_proxy = await self._get_proxy_for_page()
        if page_proxy is not None and self.proxy_pool is not None:
            print(
                f"Ozon (public-rand): page {page_number} — "
                f"proxy: {page_proxy.get('server', 'unknown')}"
            )

        # The whole browser session is one attempt: a rotating
        # proxy gateway can fail it at ANY point — refused CONNECT
        # at launch ("could not discover the egress IP"), or the
        # exit IP drifting mid-session (invisible-playwright's
        # ProxyEgressDrifted, measured on proxys.io: the gateway
        # re-rolls the exit every few minutes). None of these say
        # anything about the PAGE — so any session-level failure is
        # converted to "rotate the proxy and retry the page".
        try:
            return await self._fetch_page_cards_in_session(
                reviews_url=reviews_url,
                product_path=product_path,
                page_number=page_number,
                page_proxy=page_proxy,
                retry_async=retry_async,
                retryable_errors=retryable_errors,
                retry_attempts=retry_attempts,
            )
        except Exception as exc:
            print(
                f"Ozon (public-rand): page {page_number} — сессия "
                f"не удалась ({type(exc).__name__}: "
                f"{str(exc)[:120]}) — ротирую proxy"
            )
            self._mark_proxy_blocked(page_proxy)
            return None, True

    async def _fetch_page_cards_in_session(
        self,
        *,
        reviews_url: str,
        product_path: str,
        page_number: int,
        page_proxy: dict[str, str] | None,
        retry_async,
        retryable_errors,
        retry_attempts: int,
    ) -> tuple[list[dict[str, Any]] | None, bool]:
        """One page fetch inside a single fresh browser session.

        Returns ``(cards, antibot)`` — see ``_fetch_page_cards``.
        Raises on session-level failures (launch, egress drift);
        the caller converts those into a proxy rotation."""
        async with _mod._import_invisible_playwright()(
            proxy=page_proxy,
            seed=None,  # randomize on every fetch
            pin=self.pin,
            humanize=self.humanize,
        ) as browser:
            page = await self._new_page_with_stealth(browser)

            await self._goto_reviews_like_human(
                page,
                product_path=product_path,
                reviews_url=reviews_url,
                referer_url=self._absolute_url(product_path),
                retry_async=retry_async,
                retryable_errors=retryable_errors,
                label=f"Ozon public-rand page {page_number}",
                goto_attempts=retry_attempts,
                do_warmup=True,
            )

            if self.settle_ms > 0:
                await page.wait_for_timeout(self.settle_ms)

            review_locator = page.locator("[data-review-uuid]")
            try:
                await review_locator.first.wait_for(
                    state="attached",
                    timeout=self.card_wait_ms,
                )
            except Exception:
                antibot = await self._page_is_antibot(page)
                await self._save_debug_page(
                    page=page,
                    page_number=page_number,
                )
                if antibot:
                    # The proxy's exit IP just got flagged — take it
                    # out of the rotation so the next attempt uses a
                    # different IP.
                    self._mark_proxy_blocked(page_proxy)
                return None, antibot

            cards = await self._read_review_cards(review_locator)
            await self._save_debug_page(
                page=page,
                page_number=page_number,
                cards=cards,
            )
            return cards, False

    # Images/fonts/media are the bulk of the reviews page's bytes
    # (~880KB); review photos are never rendered by us — their src
    # urls stay in the DOM untouched. resource_type-based routing
    # keeps document/script/xhr/stylesheet untouched.
    _BLOCKED_RESOURCE_TYPES = frozenset(
        {"image", "font", "media"}
    )

    async def _install_resource_blocker(self, page) -> None:
        from infrastructure.transports.browser_common import (
            install_resource_blocker,
        )
        await install_resource_blocker(
            page, enabled=self.block_assets,
        )
