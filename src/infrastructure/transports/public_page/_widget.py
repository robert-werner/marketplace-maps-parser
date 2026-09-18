"""Mixin for PublicPageTransport."""
from __future__ import annotations
from collections.abc import AsyncIterator
from typing import Any
import asyncio
import re
import time
from collections.abc import AsyncIterator
from typing import Any
import infrastructure.transports.public_page as _mod


class WidgetFlowMixin:
    # ------------------------------------------------------------------
    # Widget flow — combined pagination + infinite scroll
    # ------------------------------------------------------------------
    _NEXT_BUTTON_SELECTOR = (
        'button:has-text("Дальше"), a:has-text("Дальше")'
    )

    # Wait until the cards' rating SVGs have hydrated instead of a
    # fixed 1.2s sleep: the star glyphs are the last thing to
    # render, and reading before them yields rating=None (measured
    # ~38% nulls without any wait). Returns as soon as the first
    # svg path appears; readers that cannot evaluate (fakes) fall
    # through immediately.
    _HYDRATION_PROBE_JS = (
        "() => document.querySelectorAll("
        "'[data-review-uuid] svg path').length"
    )

    # Push-based card-replacement detection for the widget flow:
    # a MutationObserver resolves the promise as soon as the
    # rendered card set contains a uuid absent from ``prevUuids``.
    # Resolves ``{"uuids": [...]}`` on change, ``{"timeout": true}``
    # when ``timeoutMs`` elapses. The 250 ms debounce avoids
    # resolving on a mid-render partial set; the 500 ms interval
    # floor guards against a quiet DOM that never mutates; the
    # nudge near the deadline mirrors the legacy poll loop's
    # "scroll a bit to wake the widget" trick.
    _CARD_REPLACEMENT_OBSERVER_JS = """
async ({ prevUuids, timeoutMs }) => {
    const prev = new Set(prevUuids);
    const uuids = () => Array.from(
        document.querySelectorAll("[data-review-uuid]"),
        c => c.getAttribute("data-review-uuid"),
    );
    const changed = () => {
        const cur = uuids();
        if (cur.length === 0) return null;
        for (const u of cur) {
            if (u && !prev.has(u)) return cur;
        }
        return null;
    };
    return await new Promise(resolve => {
        const immediate = changed();
        if (immediate) {
            return resolve({ uuids: immediate });
        }
        let done = false;
        let settleTimer = null;
        const finish = result => {
            if (done) return;
            done = true;
            observer.disconnect();
            clearInterval(floorTimer);
            clearTimeout(deadlineTimer);
            clearTimeout(nudgeTimer);
            if (settleTimer) clearTimeout(settleTimer);
            resolve(result);
        };
        const check = () => {
            const cur = changed();
            if (cur) finish({ uuids: cur });
        };
        const observer = new MutationObserver(() => {
            if (done) return;
            if (settleTimer) clearTimeout(settleTimer);
            settleTimer = setTimeout(check, 250);
        });
        observer.observe(document.body, {
            childList: true,
            subtree: true,
        });
        const floorTimer = setInterval(check, 500);
        const deadlineTimer = setTimeout(
            () => finish({ timeout: true }),
            timeoutMs,
        );
        const nudgeTimer = setTimeout(
            () => window.scrollBy(0, 900),
            Math.max(0, timeoutMs - 15000),
        );
    });
}
"""

    async def _wait_for_cards_hydrated(
        self, page, timeout_s: float = 3.0,
    ) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                n = await page.evaluate(self._HYDRATION_PROBE_JS)
            except Exception:
                return
            if n is None:
                # reader without evaluate support — nothing to wait
                return
            if isinstance(n, int) and n > 0:
                return
            await asyncio.sleep(0.15)

    async def _scroll_and_collect_lazy_cards(
        self,
        page,
        known_uuids: set[str],
    ) -> list[dict[str, Any]]:
        """Scroll the page like a reader, then pick up any cards the
        widget lazy-appended on the way down (some AB variants
        append on scroll instead of paginating).

        Returns cards whose uuid is not in ``known_uuids`` (caller
        passes seen ∪ the page's own cards) — possibly empty."""
        if not self.widget_scroll:
            return []
        try:
            for _ in range(3):
                await page.mouse.wheel(0, 1_600)
                await asyncio.sleep(0.4)
        except Exception:
            return []

        if self.lazy_wait_ms <= 0:
            return []

        # Wait until the uuid set stops changing (two consecutive
        # stable polls) or the lazy-wait budget runs out.
        before = None
        stable = 0
        deadline = time.monotonic() + self.lazy_wait_ms / 1000
        while time.monotonic() < deadline:
            try:
                current = frozenset(
                    await self._page_uuids_fast(page)
                )
            except Exception:
                break
            if before is None:
                before = current
                continue
            if current != before:
                before = current
                stable = 0
            else:
                stable += 1
                if stable >= 2:
                    break
            await asyncio.sleep(0.3)

        cards = await self._read_cards_fast(page)
        return [
            c for c in cards
            if c.get("uuid") and c["uuid"] not in known_uuids
        ]

    async def _wait_for_card_replacement(
        self,
        page,
        locator,
        prev_uuids: set[str],
    ) -> set[str] | None:
        """Wait until the widget replaces the card set.

        The reviews widget paginates by REPLACING the 30 rendered
        cards, so — unlike a lazy-load scroll — the card count does
        not grow.

        Fast path: a single ``page.evaluate`` installs a
        MutationObserver that resolves the moment the card set
        changes (debounced 250 ms so a mid-render partial set is
        not mistaken for the final one) — zero per-poll round-trips
        versus the old 300 ms Python-side poll loop. Falls back to
        the original polling when ``evaluate`` is unavailable or
        returns an unexpected shape (unit-test fakes, exotic
        engines).
        """
        try:
            result = await page.evaluate(
                self._CARD_REPLACEMENT_OBSERVER_JS,
                {
                    "prevUuids": sorted(
                        u for u in prev_uuids if u
                    ),
                    "timeoutMs": self.card_wait_ms,
                },
            )
        except Exception:
            result = None
        if isinstance(result, dict):
            got = {
                u
                for u in (result.get("uuids") or [])
                if u
            }
            if got:
                return got
            # {"timeout": true} — no replacement within the budget
            return None

        # --- legacy polling fallback ---
        deadline = time.monotonic() + self.card_wait_ms / 1000
        nudged = False
        while time.monotonic() < deadline:
            try:
                current = set(await self._page_uuids_fast(page))
            except Exception:
                current = None
            if current and current != prev_uuids:
                return current
            if not nudged and time.monotonic() > deadline - 15:
                try:
                    await page.mouse.wheel(0, 900)
                except Exception:
                    pass
                nudged = True
            await asyncio.sleep(0.3)
        return None

    async def iter_ozon_reviews_by_widget(
        self,
        product_path: str,
        *,
        max_reviews: int | None = None,
        retry_attempts: int = 3,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Deep review collection via the reviews widget's own
        «Дальше» control — pagination and scroll combined.

        Why this exists (measured 2026-09-15 on a 5814-review
        product): naked ``/reviews?page=N`` URLs serve anonymous
        sessions ~150 unique reviews and then repeat themselves,
        while the widget's own next-page URLs carry a ``page_key``
        token whose pagination goes deep — 930 unique reviews by
        page 31 in one session, with the token rotating every few
        pages and the numbering continuing.

        Flow per session: warm up on the product page, open the
        reviews (resuming from the last widget URL when restarting
        after a session failure), read the cards, then click
        «Дальше» and wait for the card set to be replaced — the
        "infinite scroll" built from the widget's pagination. Any
        session-level failure rotates the proxy and resumes from
        the last good widget URL. Stops when the button disappears,
        the content stops changing, ``max_reviews`` is reached, or
                        the restart budget is exhausted.

        With ``workers > 1`` the flow runs in parallel: several
        browser TABS in the SAME session walk disjoint page
        segments (the widget's page_key URLs are directly
        addressable), coordinated through a frontier queue — see
        ``_iter_widget_parallel``.
        """
        if self.workers > 1:
            async for batch in self._iter_widget_parallel(
                product_path,
                max_reviews=max_reviews,
                retry_attempts=retry_attempts,
            ):
                yield batch
            return

        retry_async = _mod._import_retry_async()
        retryable_errors = _mod._import_retryable_errors()
        sleep_with_jitter = _mod._import_sleep_with_jitter()

        product_url = self._absolute_url(product_path)
        resume_url = f"{product_url}/reviews?page=1"
        seen_uuids: set[str] = set()
        restarts_left = max(3, min(retry_attempts, 10))

        while True:
            if (
                max_reviews is not None
                and len(seen_uuids) >= max_reviews
            ):
                return

            page_proxy = await self._get_proxy_for_page()
            if page_proxy is not None and self.proxy_pool is not None:
                print(
                    "Ozon (public-widget): proxy: "
                    f"{page_proxy.get('server', 'unknown')}"
                )
            try:
                async with _mod._import_invisible_playwright()(
                    proxy=page_proxy,
                    seed=None,
                    pin=self.pin,
                    humanize=self.humanize,
                ) as browser:
                    page = await self._new_page_with_stealth(browser)

                    await self._goto_reviews_like_human(
                        page,
                        product_path=product_path,
                        reviews_url=resume_url,
                        referer_url=product_url,
                        retry_async=retry_async,
                        retryable_errors=retryable_errors,
                        label="Ozon public-widget",
                        goto_attempts=retry_attempts,
                        do_warmup=True,
                    )

                    locator = page.locator("[data-review-uuid]")
                    if not await self._wait_for_card_replacement(
                        page, locator, set(),
                    ):
                        if await self._page_is_antibot(page):
                            self._mark_proxy_blocked(page_proxy)
                            raise RuntimeError("antibot/challenge")
                        print(
                            "Ozon (public-widget): карточки не "
                            "появились — виджет недоступен"
                        )
                        return

                    idle_pages = 0
                    while True:
                        if (
                            max_reviews is not None
                            and len(seen_uuids) >= max_reviews
                        ):
                            return

                        # The card uuids appear before the star SVGs
                        # hydrate — a short settle keeps the rating
                        # extractor from reading half-rendered cards
                        # (measured: without it ~38% of deep-page
                        # cards came out rating=None).
                        await self._wait_for_cards_hydrated(page)
                        cards = await self._read_cards_fast(page)
                        # Scroll-mix: прокрутить страницу читателем и
                        # подобрать карточки, которые виджет догрузил
                        # по ходу; затем в любом случае — следующая
                        # страница.
                        known = {
                            c.get("uuid") for c in cards
                        } | seen_uuids
                        lazy = await (
                            self._scroll_and_collect_lazy_cards(
                                page, known,
                            )
                        )
                        if lazy:
                            cards = cards + lazy
                        new_cards = [
                            c for c in cards
                            if c.get("uuid")
                            and c["uuid"] not in seen_uuids
                        ]
                        for c in new_cards:
                            seen_uuids.add(c["uuid"])
                        if new_cards:
                            idle_pages = 0
                            print(
                                "Ozon (public-widget): страница "
                                f"{page.url.split('page=')[-1][:4]}"
                                f" — {len(new_cards)} новых отзывов "
                                f"(всего {len(seen_uuids)})"
                            )
                            yield new_cards
                        else:
                            idle_pages += 1

                        resume_url = page.url

                        next_btn = page.locator(
                            self._NEXT_BUTTON_SELECTOR
                        )
                        if await next_btn.count() == 0:
                            print(
                                "Ozon (public-widget): кнопка "
                                "«Дальше» исчезла — отзывов больше нет"
                            )
                            return
                        if idle_pages >= 2:
                            print(
                                "Ozon (public-widget): контент не "
                                "меняется — конец списка"
                            )
                            return

                        prev_uuids = set(
                            await self._locator_uuids(locator)
                        )
                        await next_btn.first.click()
                        replaced = await (
                            self._wait_for_card_replacement(
                                page, locator, prev_uuids,
                            )
                        )
                        if replaced is None:
                            if await self._page_is_antibot(page):
                                self._mark_proxy_blocked(page_proxy)
                                raise RuntimeError("antibot/challenge")
                            continue  # idle_pages increments next round

            except Exception as exc:
                if restarts_left <= 0:
                    print(
                        "Ozon (public-widget): сессии исчерпаны "
                        f"({type(exc).__name__}: {str(exc)[:90]}) — "
                        f"собрано {len(seen_uuids)}"
                    )
                    return
                restarts_left -= 1
                self._mark_proxy_blocked(page_proxy)
                print(
                    "Ozon (public-widget): сессия не удалась "
                    f"({type(exc).__name__}: {str(exc)[:90]}) — "
                    "ротирую proxy, продолжаю с последней страницы"
                )
                await sleep_with_jitter(5.0)

    # Pages one worker walks before handing the frontier to the
    # pool — small enough to balance workers, large enough to keep
    # the frontier queue overhead negligible.
    _WIDGET_STRIDE = 3

    @staticmethod
    def _widget_page_no(url: str) -> int:
        m = re.search(r"page=(\d+)", url)
        return int(m.group(1)) if m else 0

    async def _iter_widget_parallel(
        self,
        product_path: str,
        *,
        max_reviews: int | None = None,
        retry_attempts: int = 3,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Multi-tab widget flow: N pages of ONE session walk
        disjoint page segments in parallel.

        The widget's page_key URLs are directly addressable, so the
        page space is sharded through a frontier queue: a worker
        takes the next unread URL, walks ``_WIDGET_STRIDE`` pages
        (read → click «Дальше» → wait for replacement), then hands
        the URL it landed on back to the queue. Whoever meets the
        end (button gone / content stuck / ``max_reviews``) sets a
        done flag and no URL is pushed, so the remaining workers
        wind down within one segment. Session-level failures rotate
        the proxy and restart from the deepest page reached.
        """
        retry_async = _mod._import_retry_async()
        retryable_errors = _mod._import_retryable_errors()
        sleep_with_jitter = _mod._import_sleep_with_jitter()

        product_url = self._absolute_url(product_path)
        resume_url = f"{product_url}/reviews?page=1"
        seen: set[str] = set()
        restarts_left = max(3, min(retry_attempts, 10))

        while True:
            if (
                max_reviews is not None
                and len(seen) >= max_reviews
            ):
                return

            page_proxy = await self._get_proxy_for_page()
            if page_proxy is not None and self.proxy_pool is not None:
                print(
                    "Ozon (public-widget): proxy: "
                    f"{page_proxy.get('server', 'unknown')} "
                    f"({self.workers} воркера)"
                )

            ctx: dict[str, Any] = {
                "frontier": asyncio.Queue(),
                "out": asyncio.Queue(),
                "done": asyncio.Event(),
                "error": None,
                "seen": seen,
                "active": 0,
                "max_reviews": max_reviews,
                "product_url": product_url,
                "page_proxy": page_proxy,
                "deepest": {"n": 0, "url": None},
            }
            tasks: list[asyncio.Task] = []
            try:
                async with _mod._import_invisible_playwright()(
                    proxy=page_proxy,
                    seed=None,
                    pin=self.pin,
                    humanize=self.humanize,
                ) as browser:
                    pages = [
                        await self._new_page_with_stealth(browser)
                        for _ in range(self.workers)
                    ]
                    # One warmup for the whole session; the tabs
                    # share the browser context (and cookies).
                    await self._warmup_goto(
                        pages[0],
                        product_path=product_path,
                        retry_async=retry_async,
                        retryable_errors=retryable_errors,
                        label="Ozon public-widget",
                    )
                    ctx["frontier"].put_nowait(resume_url)
                    for i, tab in enumerate(pages):
                        tasks.append(asyncio.create_task(
                            self._widget_worker(tab, i, ctx)
                        ))

                    finished = 0
                    try:
                        while finished < len(tasks):
                            batch = await ctx["out"].get()
                            if batch is None:
                                finished += 1
                                continue
                            yield batch
                        await asyncio.gather(
                            *tasks, return_exceptions=True,
                        )
                    finally:
                        ctx["done"].set()
                        for t in tasks:
                            t.cancel()
                        await asyncio.gather(
                            *tasks, return_exceptions=True,
                        )

                if ctx["error"] is not None:
                    raise ctx["error"]
                return  # end of list / max_reviews — done
            except Exception as exc:
                if restarts_left <= 0:
                    print(
                        "Ozon (public-widget): сессии исчерпаны "
                        f"({type(exc).__name__}: {str(exc)[:90]}) — "
                        f"собрано {len(seen)}"
                    )
                    return
                restarts_left -= 1
                self._mark_proxy_blocked(page_proxy)
                if ctx["deepest"]["url"]:
                    resume_url = ctx["deepest"]["url"]
                print(
                    "Ozon (public-widget): сессия не удалась "
                    f"({type(exc).__name__}: {str(exc)[:90]}) — "
                    "ротирую proxy, продолжаю с глубже пройденной "
                    "страницы"
                )
                await sleep_with_jitter(5.0)

    async def _widget_worker(self, page, index: int, ctx) -> None:
        """One widget-flow tab: repeatedly take a frontier URL,
        walk a segment of pages, hand back the next unread URL.

        Frontier handoff discipline: after pushing the next URL the
        worker yields once (``sleep(0)``) so a worker ALREADY
        blocked on ``frontier.get()`` receives it — without this,
        the pushing worker re-takes its own URL in the same event
        loop step and starves the others (measured live: 3 workers,
        w0 walked all 34 pages). Shutdown travels as a ``None``
        sentinel through the queue: every exiting worker puts one,
        and every receiver re-puts it, so all blocked waiters wake.
        """
        label = f"w{index}"
        try:
            while not ctx["done"].is_set():
                url = await ctx["frontier"].get()
                if url is None:
                    # pass the shutdown sentinel on, then exit
                    ctx["frontier"].put_nowait(None)
                    break

                ctx["active"] += 1
                try:
                    await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=self.timeout_ms,
                        referer=ctx["product_url"],
                    )
                    locator = page.locator("[data-review-uuid]")
                    if not await self._wait_for_card_replacement(
                        page, locator, set(),
                    ):
                        if await self._page_is_antibot(page):
                            self._mark_proxy_blocked(
                                ctx["page_proxy"]
                            )
                            raise RuntimeError("antibot/challenge")
                        print(
                            f"Ozon (public-widget[{label}]): "
                            "карточки не появились — конец списка"
                        )
                        ctx["done"].set()
                        break

                    walked = 0
                    idle = 0
                    while (
                        walked < self._WIDGET_STRIDE
                        and not ctx["done"].is_set()
                    ):
                        if (
                            ctx["max_reviews"] is not None
                            and len(ctx["seen"])
                            >= ctx["max_reviews"]
                        ):
                            ctx["done"].set()
                            break
                        await self._wait_for_cards_hydrated(page)
                        cards = await self._read_cards_fast(page)
                        known = {
                            c.get("uuid") for c in cards
                        } | ctx["seen"]
                        lazy = await (
                            self._scroll_and_collect_lazy_cards(
                                page, known,
                            )
                        )
                        if lazy:
                            cards = cards + lazy
                        new_cards = [
                            c for c in cards
                            if c.get("uuid")
                            and c["uuid"] not in ctx["seen"]
                        ]
                        for c in new_cards:
                            ctx["seen"].add(c["uuid"])
                        if new_cards:
                            idle = 0
                            n = self._widget_page_no(page.url)
                            if n > ctx["deepest"]["n"]:
                                ctx["deepest"]["n"] = n
                                ctx["deepest"]["url"] = page.url
                            print(
                                f"Ozon (public-widget[{label}]): "
                                f"страница "
                                f"{page.url.split('page=')[-1][:4]}"
                                f" — {len(new_cards)} новых отзывов "
                                f"(всего {len(ctx['seen'])})"
                            )
                            await ctx["out"].put(new_cards)
                        else:
                            idle += 1

                        if ctx["done"].is_set():
                            break
                        next_btn = page.locator(
                            self._NEXT_BUTTON_SELECTOR
                        )
                        if await next_btn.count() == 0:
                            print(
                                f"Ozon (public-widget[{label}]): "
                                "кнопка «Дальше» исчезла — конец "
                                "списка"
                            )
                            ctx["done"].set()
                            break
                        if idle >= 2:
                            ctx["done"].set()
                            break

                        prev = set(await self._page_uuids_fast(page))
                        await next_btn.first.click()
                        replaced = await (
                            self._wait_for_card_replacement(
                                page, locator, prev,
                            )
                        )
                        if replaced is None:
                            if await self._page_is_antibot(page):
                                self._mark_proxy_blocked(
                                    ctx["page_proxy"]
                                )
                                raise RuntimeError(
                                    "antibot/challenge"
                                )
                            continue
                        walked += 1

                    # Hand the next unread page to the pool — the
                    # last click landed on it and it was not read
                    # yet. The sleep(0) lets a waiting worker take
                    # it before this one can re-take it.
                    if not ctx["done"].is_set():
                        ctx["frontier"].put_nowait(page.url)
                        await asyncio.sleep(0)
                finally:
                    ctx["active"] -= 1
        except Exception as exc:
            if ctx["error"] is None:
                ctx["error"] = exc
            ctx["done"].set()
        finally:
            # wake one blocked peer; the None chain unblocks the rest
            ctx["frontier"].put_nowait(None)
            await ctx["out"].put(None)
