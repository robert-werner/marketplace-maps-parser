# src/infrastructure/transports/yandex_maps_browser.py
"""Yandex.Maps org-reviews transport: one stealth browser session
per organization, harvesting clean review JSON.

Flow (measured 2026-09-18 against a live org page):

1. **Warmup** — land on the org page first, then open ``/reviews/``
   with the org page as the HTTP referer (the same anti-bot pattern
   as the Yandex.Market transport). No captcha observed on Maps so
   far (unlike Market), so the SmartCaptcha escalation ladder is
   NOT ported: a challenge page simply raises.
2. **SSR state** — the reviews page ships the FIRST page of reviews
   (``pageSize`` 50) inside a ``<script class="state-view">`` JSON
   blob: ``reviewResults.reviews`` (same card shape as the API) +
   ``reviewResults.params`` (``count`` / ``totalPages`` /
   ``reviewsRemained``) + the org aggregate ``ratingData``
   (``ratingValue`` / ``reviewCount`` / ``ratingCount`` — the last
   one counts rating-only assessments that are never listed
   individually).
3. **Pagination by interception** — the remaining pages are loaded
   by the site's own XHR to
   ``/maps/api/business/fetchReviews?…&csrfToken=…&s=…``.
4. **Direct signed API (primary path)** — the ``s`` signature was
   reversed from the maps-front-maps base chunk (module 79409,
   2026-09-19): ``s = djb2_xor32(query_string_without_s)`` with a
   case-insensitive key sort (verified 96/96 against live URLs).
   The transport templates its params (reqId / sessionId /
   csrfToken) from the site's own first XHR, then pages every
   (ranking, aspectId) combination DIRECTLY — no scrolling, no UI
   clicks, and combinations the UI never offers (any ranking ×
   aspect). Falls back to the interception walk when the recipe
   stops validating.
5. **The ~600-review window** — the SERVER (not the frontend)
   caps every stream at the first 600 reviews by offset (measured
   2026-09-19: page 13 at pageSize=50 errors, page 12 works; at
   pageSize=25 the boundary sits at page 25 — offset 600 either
   way; ``params.totalPages`` advertises the true depth). The
   streams matrix is the coverage lever: 4 global rankings × 600
   + every aspect (from the state blob, biggest first) × 6
   rankings × 600, merged by ``reviewId`` until the union reaches
   ``params.count``. The UI-walk fallback covers the same windows
   minus the aspect × ranking combinations.
"""
from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qsl, quote, urlsplit

from infrastructure.transports.browser_common import (
    import_invisible_playwright,
)
from infrastructure.transports.yandex_browser import (
    YandexCaptchaError,
    YandexSoftBlockError,
)
from shared.url_parsers import extract_yandex_maps_org_path

# NOTE: extract_yandex_maps_org_path returns the FULL path
# (``/maps/org/<slug>/<id>``) — the base must NOT add another
# /maps segment.
YANDEX_MAPS_BASE = "https://yandex.ru"

FETCH_REVIEWS_MARKER = "/maps/api/business/fetchReviews"

API_URL = "https://yandex.ru/maps/api/business/fetchReviews"

# The four rankings the header dropdown offers (measured params:
# «По умолчанию» / «По новизне» / «Сначала положительные» /
# «Сначала отрицательные»); the enum has no other values (server
# validation error lists it as an enum).
_GLOBAL_RANKINGS = (
    "by_relevance_org",
    "by_time",
    "by_rating_desc",
    "by_rating_asc",
)

# Aspect streams additionally accept the two tone rankings (the
# chips use them automatically; the direct API accepts any
# ranking × aspectId combination — measured 2026-09-19).
_ASPECT_RANKINGS = _GLOBAL_RANKINGS + (
    "by_aspect_tone_desc",
    "by_aspect_tone_asc",
)

# The server window is OFFSET-based: the first 600 reviews per
# stream (measured: page 13 at pageSize=50 errors, page 12 works;
# at pageSize=25 page 24 works, page 25 errors). 20 pages is a
# safety cap — the error response ends a stream earlier.
_WINDOW_SIZE = 600
_MAX_API_PAGES = 20

# One fetch round-trip; the payload comes back whole (reviews +
# params), no DOM involved.
_API_FETCH_JS = """
async (url) => {
    const resp = await fetch(url, {
        headers: {'Accept': 'application/json, text/plain, */*'},
    });
    const text = await resp.text();
    let payload = null;
    try { payload = JSON.parse(text); } catch (e) {}
    return {status: resp.status, payload};
}
"""


class _DirectApiUnavailable(RuntimeError):
    """The signed-fetch recipe broke (site update): the transport
    falls back to the UI walk."""

# «Ещё» at the list bottom. NOTE: plain text «Ещё» also lives inside
# every long review (a text-expansion spoiler,
# ``business-review-view__expand``) — matching by class, not text.
# After the ~600-review window the control is REMOVED from the DOM
# entirely (measured 2026-09-18); the scroll trigger below is the
# primary loader anyway.
_MORE_SELECTOR = ".business-reviews-card-view__more"

# The ranking dropdown in the reviews header. Options live in
# ``rating-ranking-view__popup`` only while it is open; «По
# умолчанию» is the stream the page loads first, so the walk skips
# it. NOTE: the control ignores synthetic el.click() — a REAL
# locator click (CDP mouse events) is required to open the popup.
_RANKING_CONTROL_SELECTOR = ".rating-ranking-view"
_RANKING_LINE_SELECTOR = ".rating-ranking-view__popup-line"
_RANKING_OPTIONS = (
    "По новизне",
    "Сначала отрицательные",
    "Сначала положительные",
)

# Kept minimal: no captcha has been served on Maps yet (measured
# 2026-09-18: 4 probe sessions, incl. 2 curl_cffi loads, all clean).
# yandex_browser.py carries the full marker list if Maps starts
# challenging.
_CAPTCHA_URL_MARKERS = (
    "showcaptcha",
    "checkcaptcha",
    "smartcaptcha",
)
_CAPTCHA_HTML_MARKERS = (
    "Вы не робот?",
    "Подтвердите, что запросы отправляли вы",
    "Докажите, что вы не робот",
)

_STATE_JS = """
() => {
    const el = document.querySelector('script.state-view');
    return el ? el.textContent : null;
}
"""

# Scroll the reviews pane (an inner overflow container, NOT the
# window) to its bottom — measured 2026-09-18: this is what wakes
# the next fetchReviews XHR; the «Ещё» control often sits below the
# pane's viewport so locator.click alone does not fire it.
_SCROLL_PANE_JS = """
() => {
    const containers = [...document.querySelectorAll(
        'div, section'
    )].filter((el) => {
        const st = getComputedStyle(el);
        return (st.overflowY === 'auto'
                || st.overflowY === 'scroll')
            && el.scrollHeight > el.clientHeight + 200;
    });
    if (!containers.length) return false;
    const target = containers.sort(
        (a, b) => b.scrollHeight - a.scrollHeight
    )[0];
    target.scrollTop = target.scrollHeight;
    return true;
}
"""

# Aspect carousel chips (``Персонал · 66%положительный3184 отзыва``).
# The chips overflow the carousel's viewport, so locator clicks fail
# on off-screen chips — JS el.click() works for all of them.
_ASPECT_CHIP_LABELS_JS = """
() => [...document.querySelectorAll(
    '.business-review-aspects button'
)].map((b) => (b.textContent || '').trim()).filter(Boolean)
"""

_CLICK_ASPECT_CHIP_JS = """
(name) => {
    const buttons = [...document.querySelectorAll(
        '.business-review-aspects button'
    )];
    const target = buttons.find((b) => {
        const text = (b.textContent || '').trim();
        return text === name || text.startsWith(name + ' ·');
    });
    if (!target) return false;
    target.click();
    return true;
}
"""


def find_review_results(
    node: Any,
) -> dict[str, Any] | None:
    """Locate the ``{reviews, params}`` dict anywhere in the state.

    The blob nests ``reviewResults`` deep inside the serialized
    org object; walking the tree beats pinning the exact path (the
    frontend reshuffles its state tree between builds).
    """
    if isinstance(node, dict):
        reviews = node.get("reviews")
        if (
            isinstance(reviews, list)
            and reviews
            and isinstance(reviews[0], dict)
            and "reviewId" in reviews[0]
        ):
            return node
        for value in node.values():
            found = find_review_results(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = find_review_results(value)
            if found is not None:
                return found
    return None


def find_rating_data(node: Any) -> dict[str, Any] | None:
    """Locate the org aggregate ``ratingData`` dict in the state."""
    if isinstance(node, dict):
        rating_data = node.get("ratingData")
        if isinstance(rating_data, dict):
            return rating_data
        for value in node.values():
            found = find_rating_data(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = find_rating_data(value)
            if found is not None:
                return found
    return None


_ASPECT_SIZE_RE = re.compile(r"(\d+)\s*отзыв")


def aspect_chip_size(label: str) -> int:
    """Review count from a chip label
    (``Персонал · 66%положительный3184 отзыва`` → 3184).

    Bigger aspects expose bigger windows, so the walk visits them
    first; 0 when the label carries no count.
    """
    match = _ASPECT_SIZE_RE.search(label)
    return int(match.group(1)) if match else 0


# --- direct-API signing ----------------------------------------------------
#
# Reversed 2026-09-19 from the maps-front-maps base chunk (webpack
# module 79409): the request wrapper signs every /maps/api query
# with ``s = djb2_xor32(stringify(params_without_s))`` where
# stringify is the query-string npm module with a
# case-insensitive-alphabetical key sort (verified 96/96 against
# live captured URLs). This unlocks arbitrary (ranking, aspectId,
# page) combinations the UI never offers.

def djb2_xor32(text: str) -> int:
    """djb2-xor-32: ``h = 33*h ^ charCode`` over the string,
    truncated to unsigned 32 bits (JS ``>>> 0``)."""
    h = 5381
    for ch in text:
        h = ((33 * h) ^ ord(ch)) & 0xFFFFFFFF
    return h


def sign_maps_query(
    params: dict[str, Any],
) -> str:
    """Serialize + sign a Maps API query (adds the ``s`` param).

    Returns the full query string (without the leading ``?``).
    ``csrfToken``/``reqId``/``sessionId`` values must be the ones
    the page session is using — the signature covers every param.
    """
    def encode(items: list[tuple[str, Any]]) -> str:
        return "&".join(
            f"{quote(str(k), safe='')}={quote(str(v), safe='')}"
            for k, v in items
        )

    items = sorted(
        params.items(), key=lambda kv: kv[0].lower(),
    )
    signed = dict(params)
    signed["s"] = djb2_xor32(encode(items))
    return encode(sorted(signed.items(), key=lambda kv: kv[0].lower()))


def find_aspects(node: Any) -> list[dict[str, Any]]:
    """Aspects with ids from the state blob
    (``[{"id": "3502044050", "text": "Персонал", "count": 3184,
    …}]``) — the aspect carousel's data source."""
    if isinstance(node, dict):
        aspects = node.get("aspects")
        if (
            isinstance(aspects, list)
            and aspects
            and isinstance(aspects[0], dict)
            and "id" in aspects[0]
            and "text" in aspects[0]
        ):
            return [
                aspect
                for aspect in aspects
                if isinstance(aspect, dict) and aspect.get("id")
            ]
        for value in node.values():
            found = find_aspects(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = find_aspects(value)
            if found:
                return found
    return []


class YandexMapsBrowserTransport:
    """Streams batches of raw review dicts for one Maps org."""

    def __init__(
        self,
        *,
        timeout_ms: int = 90_000,
        settle_ms: int = 2_000,
        debug_dir: str | Path = "debug_yandex_maps",
        proxy: dict[str, str] | None = None,
        proxy_pool: Any = None,
        cookies: list[dict[str, Any]] | None = None,
        humanize: bool = True,
        cookies_path: str | Path | None = None,
        max_idle_rounds: int = 3,
        walk_extra_streams: bool = True,
        dup_streak_stop: int = 300,
        use_direct_api: bool = True,
        api_pacing_seconds: float = 0.8,
    ) -> None:
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self.debug_dir = Path(debug_dir)
        self.proxy = proxy
        self.proxy_pool = proxy_pool
        self.cookies = cookies
        self.humanize = humanize
        self.cookies_path = (
            Path(cookies_path) if cookies_path else None
        )
        self.max_idle_rounds = max_idle_rounds
        #: Walk the ranking options + aspect chips after the default
        #: stream (each is a separate ~600-review window).
        self.walk_extra_streams = walk_extra_streams
        #: Abort a stream after this many CONSECUTIVE already-seen
        #: reviews (the same guard as the Ozon extra streams).
        self.dup_streak_stop = dup_streak_stop
        #: Direct signed fetchReviews pagination (the reversed
        #: ``s`` recipe); falls back to the UI walk when the site
        #: changes the recipe.
        self.use_direct_api = use_direct_api
        #: Pause between direct API requests (politeness).
        self.api_pacing_seconds = api_pacing_seconds
        #: Filled while iterating (the CLI prints them in the
        #: summary).
        self.last_total_count: int | None = None
        self.last_average_rating: float | None = None
        #: Total assessments incl. rating-only (ratingCount) — Maps,
        #: like Ozon, never lists those individually.
        self.last_rating_count: int | None = None

    async def iter_review_batches(
        self,
        org_url: str,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Yield batches of NEW review dicts (dedup by ``reviewId``)
        until the site's own counter says everything is loaded."""
        org_path = extract_yandex_maps_org_path(org_url)

        browser_cls = import_invisible_playwright()
        self.debug_dir.mkdir(parents=True, exist_ok=True)

        proxy = self.proxy
        if proxy is None and self.proxy_pool is not None:
            proxy = await self._next_proxy()
            if proxy is not None:
                print(
                    "Я.Карты: один proxy на сессию — "
                    f"{proxy.get('server', '?')}"
                )

        async with browser_cls(
            proxy=proxy,
            seed=None,
            humanize=self.humanize,
        ) as browser:
            page = await browser.new_page()
            if self.cookies:
                try:
                    await page.context.add_cookies(self.cookies)
                except Exception as exc:
                    print(
                        f"Я.Карты: не удалось внедрить cookies: {exc}"
                    )

            captured: list[Any] = []
            page.on(
                "response",
                lambda response: (
                    captured.append(response)
                    if FETCH_REVIEWS_MARKER in response.url
                    else None
                ),
            )

            await self._goto_reviews(page, org_path)

            state_text = await page.evaluate(_STATE_JS)
            state: Any = None
            if state_text:
                try:
                    state = json.loads(state_text)
                except json.JSONDecodeError:
                    state = None

            results = find_review_results(state)
            if results is None:
                await self._dump_html(
                    "reviews_no_state.html", page,
                )
                raise YandexSoftBlockError(
                    "Я.Карты: state-view blob без reviewResults — "
                    "страница отрисовалась без отзывов"
                )

            self._update_totals(
                results.get("params") or {},
                find_rating_data(state) or {},
            )
            await self._save_cookies(page)

            seen: set[str] = {
                card["reviewId"]
                for card in results.get("reviews") or []
                if card.get("reviewId")
            }
            first_batch = results.get("reviews") or []
            if first_batch:
                yield first_batch

            consumed = 0
            dup_streak = 0

            def total_reached() -> bool:
                total = self.last_total_count
                return (
                    total is not None and len(seen) >= total
                )

            async def drain() -> list[dict[str, Any]]:
                """Consume new fetchReviews responses → new cards.

                Aspect-stream payloads (``aspectId=`` in the URL)
                carry the ASPECT's count — they must not overwrite
                the org-wide ``last_total_count``.
                """
                nonlocal consumed, dup_streak
                new_cards: list[dict[str, Any]] = []
                while consumed < len(captured):
                    response = captured[consumed]
                    consumed += 1
                    is_aspect = "aspectId=" in response.url
                    try:
                        payload = await response.json()
                    except Exception:
                        continue
                    data = (
                        payload.get("data")
                        if isinstance(payload, dict)
                        else None
                    ) or {}
                    self._update_totals(
                        data.get("params") or {},
                        {},
                        is_aspect=is_aspect,
                    )
                    for card in data.get("reviews") or []:
                        review_id = card.get("reviewId")
                        if review_id and review_id in seen:
                            dup_streak += 1
                            continue
                        dup_streak = 0
                        if review_id:
                            seen.add(review_id)
                        new_cards.append(card)
                return new_cards

            async def stream_batches(
            ) -> AsyncIterator[list[dict[str, Any]]]:
                """Exhaust the CURRENT stream (whatever filter the
                page has active) until it stops loading."""
                nonlocal dup_streak
                dup_streak = 0
                idle_rounds = 0
                while True:
                    if total_reached():
                        return
                    if (
                        self.dup_streak_stop > 0
                        and dup_streak >= self.dup_streak_stop
                    ):
                        return

                    clicked = await self._click_more(page)
                    scrolled = await page.evaluate(
                        _SCROLL_PANE_JS,
                    )
                    await asyncio.sleep(
                        self.settle_ms / 1000,
                    )

                    new_cards = await drain()
                    if new_cards:
                        idle_rounds = 0
                        yield new_cards
                        continue

                    idle_rounds += 1
                    if idle_rounds >= self.max_idle_rounds:
                        return
                    if not (clicked or scrolled):
                        # Nothing can load more (control gone, pane
                        # not scrollable) but a previous round's
                        # XHR may still be in flight — one more idle
                        # round absorbs it.
                        if idle_rounds >= 2:
                            return

            # Preferred path: direct signed pagination over every
            # (ranking, aspect) combination — no scrolling, no UI
            # clicks, and windows the UI cannot even reach.
            if self.use_direct_api:
                try:
                    async for batch in self._iter_direct_api(
                        page, captured, seen, state,
                    ):
                        yield batch
                    await self._save_cookies(page)
                    return
                except _DirectApiUnavailable as exc:
                    print(
                        f"Я.Карты: прямой API недоступен ({exc}) — "
                        "переключаюсь на обход через UI"
                    )

            # Stream 0: the default ranking.
            async for batch in stream_batches():
                yield batch

            # Streams 1..N: other rankings + aspect chips, each its
            # own ~600-review window on the same list.
            if self.walk_extra_streams and not total_reached():
                for label in _RANKING_OPTIONS:
                    if not await self._select_ranking(
                        page, label,
                    ):
                        continue
                    print(
                        f"Я.Карты: поток сортировки «{label}» "
                        f"(собрано уникальных: {len(seen)})"
                    )
                    async for batch in stream_batches():
                        yield batch
                    if total_reached():
                        break

            if self.walk_extra_streams and not total_reached():
                chips = await page.evaluate(
                    _ASPECT_CHIP_LABELS_JS,
                )
                # Biggest aspects first: bigger windows, and the
                # total_reached() early stop fires sooner.
                chips.sort(
                    key=aspect_chip_size, reverse=True,
                )
                for chip_label in chips:
                    name = chip_label.split(" · ")[0]
                    aspect_id = await self._activate_aspect(
                        page, name, captured,
                    )
                    if aspect_id is None:
                        continue
                    print(
                        f"Я.Карты: поток аспекта «{name}» "
                        f"(собрано уникальных: {len(seen)})"
                    )
                    async for batch in stream_batches():
                        yield batch
                    if total_reached():
                        break

            await self._save_cookies(page)

    # --- internals -------------------------------------------------------

    async def _ensure_api_template(
        self,
        page: Any,
        captured: list[Any],
    ) -> bool:
        """Make the site fire ONE fetchReviews XHR so its params
        (reqId/sessionId/csrfToken) can template our signed calls.

        No-op when a response is already captured; one pane scroll
        otherwise. Returns False when nothing flows (e.g. the org
        has a single page — nothing to paginate anyway)."""
        if captured:
            return True
        await page.evaluate(_SCROLL_PANE_JS)
        for _poll in range(4):
            await page.wait_for_timeout(800)
            if captured:
                return True
        return False

    async def _iter_direct_api(
        self,
        page: Any,
        captured: list[Any],
        seen: set[str],
        state: Any,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Direct signed fetchReviews pagination.

        Signs requests locally (see :func:`sign_maps_query`) and
        walks every (ranking, aspectId) window until the union
        reaches the site total. Raises :class:`_DirectApiUnavailable`
        when the recipe stops validating (site update) so the caller
        can fall back to the UI walk."""
        if not await self._ensure_api_template(page, captured):
            raise _DirectApiUnavailable(
                "сайт не выпустил XHR для шаблона параметров"
            )
        query = dict(
            parse_qsl(urlsplit(captured[0].url).query),
        )
        base = {
            key: value
            for key, value in query.items()
            if key not in ("s", "page", "ranking", "aspectId")
        }
        token = base.get("csrfToken") or ""
        end_of_window = object()

        def total_reached() -> bool:
            total = self.last_total_count
            return total is not None and len(seen) >= total

        async def call_api(
            ranking: str,
            aspect_id: str | None,
            page_no: int,
        ) -> Any:
            """One signed request → ``data`` dict,
            ``end_of_window`` sentinel, or raises."""
            nonlocal token

            async def fetchonce() -> Any:
                params = dict(
                    base,
                    page=page_no,
                    ranking=ranking,
                    csrfToken=token,
                )
                if aspect_id:
                    params["aspectId"] = aspect_id
                url = f"{API_URL}?{sign_maps_query(params)}"
                out = await page.evaluate(_API_FETCH_JS, url)
                return out

            out = await fetchonce()
            status = out.get("status") if isinstance(out, dict) else None
            payload = (
                out.get("payload") if isinstance(out, dict) else None
            )
            if not isinstance(payload, dict):
                raise _DirectApiUnavailable(
                    f"не-JSON ответ (status={status})"
                )
            # Token rotation: the wrapper retries once with the
            # fresh token (mirrors module 79409).
            if (
                "csrfToken" in payload
                and payload["csrfToken"]
                and payload["csrfToken"] != token
            ):
                token = payload["csrfToken"]
                out = await fetchonce()
                payload = (
                    out.get("payload")
                    if isinstance(out, dict)
                    else None
                )
                if not isinstance(payload, dict):
                    raise _DirectApiUnavailable(
                        "не-JSON ответ после ротации токена"
                    )
            if payload.get("type") == "captcha":
                raise YandexCaptchaError(
                    "Я.Карты: капча на прямой API-запрос"
                )
            error = payload.get("error")
            if isinstance(error, dict):
                message = str(error.get("message", ""))
                if "Validation" in message:
                    raise _DirectApiUnavailable(
                        f"сервер отверг параметры: {message[:80]}"
                    )
                # «Internal error» past the 600-review window.
                return end_of_window
            return payload.get("data") or {}

        # Aspects come straight from the state blob — no chip
        # clicking. Biggest first: bigger windows, earlier stop.
        aspects = find_aspects(state)
        aspects.sort(
            key=lambda a: int(a.get("count") or 0), reverse=True,
        )

        streams: list[tuple[str, str | None]] = [
            (ranking, None) for ranking in _GLOBAL_RANKINGS
        ]
        if self.walk_extra_streams:
            for aspect in aspects:
                aid = str(aspect["id"])
                count = int(aspect.get("count") or 0)
                # An aspect with ≤600 reviews fits ENTIRELY into a
                # single window — one ranking collects it fully and
                # the other five would only re-serve the same
                # reviews. Matters for the full-drain runs
                # (--dup-streak-stop 0 walks every page to the
                # window edge).
                if (
                    self.dup_streak_stop == 0
                    and 0 < count <= _WINDOW_SIZE
                ):
                    streams.append(
                        ("by_relevance_org", aid),
                    )
                    continue
                streams += [
                    (ranking, aid)
                    for ranking in _ASPECT_RANKINGS
                ]

        for ranking, aspect_id in streams:
            if total_reached():
                break
            label = ranking + (
                f" · аспект {aspect_id}" if aspect_id else ""
            )
            stream_new = 0
            # Review-based dup guard (the same semantics as the
            # Ozon streams): a stream's top pages may re-serve
            # known ground before reaching new reviews (measured:
            # global by_rating_desc starts with ~150 reviews the
            # by_relevance window already holds), so a page-count
            # guard would abandon it too early.
            dup_run = 0
            for page_no in range(1, _MAX_API_PAGES + 1):
                data = await call_api(ranking, aspect_id, page_no)
                if data is end_of_window:
                    break
                self._update_totals(
                    data.get("params") or {},
                    {},
                    is_aspect=aspect_id is not None,
                )
                page_cards = data.get("reviews") or []
                new_cards: list[dict[str, Any]] = []
                for card in page_cards:
                    review_id = card.get("reviewId")
                    if review_id and review_id in seen:
                        continue
                    if review_id:
                        seen.add(review_id)
                    new_cards.append(card)
                if new_cards:
                    dup_run = 0
                    stream_new += len(new_cards)
                    yield new_cards
                    if total_reached():
                        break
                else:
                    dup_run += len(page_cards)
                    if (
                        self.dup_streak_stop > 0
                        and dup_run >= self.dup_streak_stop
                    ):
                        break
                await asyncio.sleep(self.api_pacing_seconds)
            print(
                f"Я.Карты: прямой поток {label}: "
                f"+{stream_new} (уникальных: {len(seen)})"
            )

    async def _next_proxy(self) -> dict[str, str] | None:
        """Pull the next proxy from the pool (async-aware)."""
        if self.proxy_pool is None:
            return None
        try:
            pool_next = getattr(self.proxy_pool, "next_async", None)
            if pool_next is not None:
                return cast(
                    "dict[str, str] | None",
                    await pool_next(),
                )
            return cast(
                "dict[str, str] | None",
                self.proxy_pool.next(),
            )
        except Exception:
            return None

    async def _goto_reviews(
        self,
        page: Any,
        org_path: str,
    ) -> None:
        """Warmup on the org page, then open ``/reviews/``."""
        base = YANDEX_MAPS_BASE
        await page.goto(
            f"{base}{org_path}", timeout=self.timeout_ms,
        )
        await page.wait_for_timeout(self.settle_ms)

        await page.goto(
            f"{base}{org_path}/reviews/",
            timeout=self.timeout_ms,
            referer=f"{base}{org_path}",
        )
        await page.wait_for_timeout(self.settle_ms)

        url = page.url
        html = await page.content()
        if any(m in url for m in _CAPTCHA_URL_MARKERS):
            await self._dump_html(
                "captcha.html", page, html=html,
            )
            raise YandexCaptchaError(
                f"Я.Карты: капча на {url[:80]}"
            )
        markers = [
            m for m in _CAPTCHA_HTML_MARKERS if m in html
        ]
        if markers:
            await self._dump_html(
                "captcha.html", page, html=html,
            )
            raise YandexCaptchaError(
                f"Я.Карты: inline-капча ({markers[0]!r})"
            )

    async def _click_more(self, page: Any) -> bool:
        """Click «Ещё»; False when absent or disabled."""
        try:
            locator = page.locator(_MORE_SELECTOR).first
            if await locator.count() == 0:
                return False
            class_attr = (
                await locator.get_attribute("class") or ""
            )
            if "_disabled" in class_attr:
                return False
            await locator.click(timeout=3_000)
            return True
        except Exception:
            return False

    async def _select_ranking(
        self,
        page: Any,
        label: str,
    ) -> bool:
        """Open the ranking dropdown and pick ``label``; False when
        the popup does not open (e.g. it only exists on orgs with
        enough reviews)."""
        try:
            await page.locator(
                _RANKING_CONTROL_SELECTOR,
            ).first.click(timeout=5_000)
            await page.wait_for_timeout(800)
            line = page.locator(
                _RANKING_LINE_SELECTOR,
                has_text=label,
            ).first
            await line.click(timeout=4_000)
            await page.wait_for_timeout(1_200)
            return True
        except Exception:
            return False

    async def _activate_aspect(
        self,
        page: Any,
        name: str,
        captured: list[Any],
    ) -> str | None:
        """Click the aspect chip until its stream actually starts.

        Returns the ``aspectId`` that started flowing, or None when
        the chip never sticks. A JS click right after page load can
        be silently dropped — the carousel hydrates late (measured
        2026-09-18: a click 3 s after load produced a default-stream
        XHR with no aspectId at all), so each click is verified by
        waiting for a NEW response carrying an aspectId.
        """
        aspect_re = re.compile(r"aspectId=(\d+)")
        for _attempt in range(3):
            before = len(captured)
            try:
                clicked = await page.evaluate(
                    _CLICK_ASPECT_CHIP_JS, name,
                )
            except Exception:
                clicked = False
            if not clicked:
                return None
            for _poll in range(4):
                await page.wait_for_timeout(1_000)
                for response in captured[before:]:
                    match = aspect_re.search(response.url)
                    if match:
                        return match.group(1)
        return None

    def _update_totals(
        self,
        params: dict[str, Any],
        rating_data: dict[str, Any],
        *,
        is_aspect: bool = False,
    ) -> None:
        count = params.get("count")
        if isinstance(count, int) and not is_aspect:
            self.last_total_count = count
        rating_value = rating_data.get("ratingValue")
        if isinstance(rating_value, (int, float)):
            # The blob stores float32 (measured: 4.400000095367432).
            self.last_average_rating = round(
                float(rating_value), 2,
            )
        rating_count = rating_data.get("ratingCount")
        if isinstance(rating_count, int):
            self.last_rating_count = rating_count

    async def _save_cookies(self, page: Any) -> None:
        if self.cookies_path is None:
            return
        try:
            cookies = await page.context.cookies()
            self.cookies_path.write_text(
                json.dumps(cookies, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    async def _dump_html(
        self,
        name: str,
        page: Any,
        *,
        html: str | None = None,
    ) -> None:
        try:
            if html is None:
                html = await page.content()
            (self.debug_dir / name).write_text(
                html, encoding="utf-8",
            )
        except Exception:
            pass


__all__ = [
    "YandexMapsBrowserTransport",
    "aspect_chip_size",
    "djb2_xor32",
    "find_aspects",
    "find_rating_data",
    "find_review_results",
    "sign_maps_query",
]
