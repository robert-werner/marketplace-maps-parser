from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

from domain.entities import ProductRef, Review, ReviewPage
from infrastructure.marketplaces.base import MarketplaceAdapter
from infrastructure.marketplaces.ozon_payload import (  # noqa: F401
    UUID_RE as UUID_RE,
)
from infrastructure.marketplaces.ozon_payload import (
    build_review_key as build_review_key,
)
from infrastructure.marketplaces.ozon_payload import (
    extract_ozon_product_title as extract_ozon_product_title,
)
from infrastructure.marketplaces.ozon_payload import (
    extract_ozon_rating_summary as extract_ozon_rating_summary,
)
from infrastructure.marketplaces.ozon_payload import (
    extract_review_id as extract_review_id,
)
from infrastructure.marketplaces.ozon_payload import (
    extract_reviews_from_ozon_payload as extract_reviews_from_ozon_payload,
)
from infrastructure.marketplaces.ozon_payload import (
    extract_seller_answer as extract_seller_answer,
)
from infrastructure.marketplaces.ozon_payload import (
    first_value as first_value,
)
from infrastructure.marketplaces.ozon_payload import (
    is_review_node as is_review_node,
)
from infrastructure.marketplaces.ozon_payload import (
    map_ozon_review_node as map_ozon_review_node,
)
from infrastructure.marketplaces.ozon_payload import (
    normalize_rating as normalize_rating,
)
from infrastructure.marketplaces.ozon_payload import (
    normalize_text as normalize_text,
)
from infrastructure.marketplaces.ozon_payload import (
    parse_ozon_date as parse_ozon_date,
)
from infrastructure.marketplaces.ozon_payload import (
    walk_json as walk_json,
)
from shared.url_parsers import (
    extract_ozon_product_id,
    extract_ozon_product_path,
)


class OzonBrowserTransport(Protocol):
    """Subset of BrowserJsonTransport / BrowserDomTransport
    used by OzonAdapter."""

    def iter_ozon_reviews_json(
        self,
        product_path: str,
        *,
        start_page: int = ...,
        max_pages: int | None = ...,
        retry_attempts: int = ...,
        extra_query: str = ...,
    ) -> AsyncIterator[tuple[int, dict[str, Any]]]: ...

    async def get_ozon_reviews_json(
        self,
        product_path: str,
        *,
        page_number: int = ...,
    ) -> dict[str, Any]: ...

    def iter_ozon_reviews_by_scroll(
        self,
        product_path: str,
        *,
        max_reviews: int | None = ...,
    ) -> AsyncIterator[list[dict[str, Any]]]: ...

    def iter_all_ozon_reviews(
        self,
        product_path: str,
        *,
        max_reviews: int | None = ...,
        pagination_max_pages: int | None = ...,
        pagination_start_page: int = ...,
        scroll_max_rounds: int = ...,
        page_delay_seconds: float = ...,
        scroll_pause_seconds: float = ...,
        retry_attempts: int = ...,
    ) -> AsyncIterator[
        tuple[str, dict[str, Any]]
    ]: ...


class OzonAdapter(MarketplaceAdapter):
    name = "ozon"

    def __init__(
        self,
        browser_transport: OzonBrowserTransport,
    ) -> None:
        self.browser_transport = browser_transport
        # Rating histogram from the webReviewProductScore widget of the
        # most recent payload (filled by ``iter_reviews``). Ozon does
        # NOT expose rating-only "оценки" as individual review cards —
        # only these aggregate counts — so the summary is the only way
        # to account for them.
        self.last_rating_summary: dict[str, Any] | None = None
        # Product title for the unified output's ``product_title``
        # — from the pdp_reviews seo block ("N отзыв на <name>").
        self.last_product_title: str | None = None
        # Total number of reviews advertised by Ozon. API transports
        # expose it through webReviewProductScore; PublicPageTransport
        # reads it from the product/reviews page during warmup.
        self.last_review_count: int | None = None

    @staticmethod
    def _normalize_reported_review_count(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value >= 0 else None
        if isinstance(value, str):
            normalized = value.replace(" ", "").replace("\u00a0", "")
            if normalized.isdecimal():
                return int(normalized)
        return None

    def _reported_review_count(self) -> int | None:
        """Return Ozon's product-wide count when a transport exposed it.

        The count is used only to stop *after* that many unique cards
        are collected. If unavailable, normal pagination and stream
        discovery remain unchanged.
        """
        candidates = [
            self._normalize_reported_review_count(
                self.last_review_count,
            ),
            self._normalize_reported_review_count(
                (self.last_rating_summary or {}).get("reviews_count"),
            ),
            self._normalize_reported_review_count(
                getattr(self.browser_transport, "last_review_count", None),
            ),
        ]
        valid = [count for count in candidates if count is not None]
        if not valid:
            return None

        # Prefer the larger number if Ozon's page variants disagree:
        # this is conservative and avoids truncating distinct cards.
        self.last_review_count = max(valid)
        return self.last_review_count

    def _has_collected_reported_total(self, yielded: int) -> bool:
        total = self._reported_review_count()
        return total is not None and yielded >= total

    def _print_total_reached(self, yielded: int) -> None:
        total = self._reported_review_count()
        if total is not None:
            print(
                f"Ozon: собрано {yielded} из {total} заявленных "
                "отзывов — дополнительные стримы не нужны"
            )

    async def _preflight_reported_review_count(
        self,
        product_path: str,
    ) -> None:
        """Ask transports that support a cheap product-page preflight."""
        get_review_count = getattr(
            self.browser_transport,
            "get_ozon_review_count",
            None,
        )
        if get_review_count is None:
            return

        try:
            reported = await get_review_count(product_path)
        except Exception:
            return

        count = self._normalize_reported_review_count(reported)
        if count is not None:
            self.last_review_count = count

        product_title = getattr(
            self.browser_transport,
            "last_product_title",
            None,
        )
        if isinstance(product_title, str) and product_title.strip():
            self.last_product_title = product_title.strip()

    async def collect(self, product_url: str) -> ReviewPage:
        product_id = extract_ozon_product_id(product_url)
        product_path = extract_ozon_product_path(product_url)

        raw = await self.browser_transport.get_ozon_reviews_json(
            product_path=product_path,
            page_number=1,
        )

        product = ProductRef(
            marketplace=self.name,
            source_url=product_url,
            product_id=str(product_id),
        )

        reviews = extract_reviews_from_ozon_payload(
            payload=raw,
            product=product,
        )

        return ReviewPage(
            product=product,
            reviews=reviews,
            total_count=len(reviews),
            raw=raw,
        )

    async def iter_reviews(
            self,
            product_url: str,
            *,
            start_page: int = 1,
            max_pages: int | None = None,
            extra_query: str = "",
    ) -> AsyncIterator[Review]:
        """Walk ONE reviews stream (``extra_query`` selects the
        stream variant, e.g. ``&sort=score_asc``).

        ``extra_query`` is passed to the transport only when
        non-empty, so transports (and test fakes) that predate the
        parameter keep working for the default stream.
        """
        product_id = extract_ozon_product_id(product_url)
        product_path = extract_ozon_product_path(product_url)

        product = ProductRef(
            marketplace=self.name,
            source_url=product_url,
            product_id=str(product_id),
        )

        seen_keys: set[str] = set()

        fetch_kwargs: dict[str, Any] = {}
        if extra_query:
            fetch_kwargs["extra_query"] = extra_query

        async for page_number, payload in (
                self.browser_transport.iter_ozon_reviews_json(
                    product_path=product_path,
                    start_page=start_page,
                    max_pages=max_pages,
                    **fetch_kwargs,
                )
        ):

            # The first page of every stream carries the rating
            # histogram (webReviewProductScore widget); keep the
            # freshest copy for the run summary / --include-rating-only.
            summary = extract_ozon_rating_summary(
                payload,
                product_id=str(product_id),
                product_url=product_url,
            )
            if summary is not None:
                self.last_rating_summary = summary
                self._reported_review_count()

            if self.last_product_title is None:
                self.last_product_title = (
                    extract_ozon_product_title(payload)
                )

            reviews = extract_reviews_from_ozon_payload(
                payload=payload,
                product=product,
            )

            new_count = 0

            for position, review in enumerate(reviews):
                key = review.review_id

                if not key:
                    key = build_review_key(
                        review,
                        page_number=page_number,
                        position=position,
                    )

                if key in seen_keys:
                    continue

                seen_keys.add(key)
                new_count += 1
                yield review

            print(
                f"Ozon: страница {page_number}; "
                f"получено={len(reviews)}; "
                f"новых={new_count}"
            )

    def parse_ozon_dom_card(self,
            card: dict[str, Any],
            product: ProductRef,
    ) -> Review:
        # ``text`` from the DOM is multi-line: avatar initials, author
        # name, date, review text, "Вам помог этот отзыв?", "Да N Нет M".
        # We split into lines to extract author and review text.
        raw_text = card.get("text") or ""
        # ``review_text`` is a pre-extracted cleaner version when the
        # DOM transport already did the line-by-line filtering.
        text = card.get("review_text") or raw_text

        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip()
        ]

        # Author extraction: DOM transport already extracts this when
        # available; fall back to line-based heuristic for raw cards.
        author = card.get("author")
        if author is None:
            author = lines[1] if len(lines) > 1 else (
                lines[0] if lines else None
            )

        # Rating: DOM transport extracts it from SVG star colors
        # (BrowserDomTransport._read_review_rating). When the scroll
        # mode in browser_json.py is used, rating is not extracted
        # (see _read_review_cards in browser_json.py); we fall back to
        # the explicit "rating" key in the card dict if present.
        rating = card.get("rating")
        raw_images = card.get("images")
        photos = [
            image
            for image in raw_images
            if isinstance(image, str) and image
        ] if isinstance(raw_images, list) else []

        return Review(
            review_id=card.get("uuid"),
            product=product,
            rating=rating,
            text=text or None,
            author=author,
            created_at=parse_ozon_date(
                card.get("published_at")
            ),
            photos=photos,
            raw=card,
        )

    async def iter_reviews_by_scroll(
            self,
            product_url: str,
            *,
            max_reviews: int | None = None,
    ) -> AsyncIterator[Review]:
        product_id = extract_ozon_product_id(product_url)
        product_path = extract_ozon_product_path(product_url)

        product = ProductRef(
            marketplace=self.name,
            source_url=product_url,
            product_id=str(product_id),
        )

        async for cards in (
                self.browser_transport.iter_ozon_reviews_by_scroll(
                    product_path=product_path,
                    max_reviews=max_reviews,
                )
        ):
            for _position, card in enumerate(cards):
                review = self.parse_ozon_dom_card(
                    card=card,
                    product=product,
                )

                if review is not None:
                    yield review

    # ------------------------------------------------------------------
    # Unified "collect ALL reviews" stream
    # ------------------------------------------------------------------

    # Additional review-stream sorts. Ozon exposes through the
    # DEFAULT stream (usefulness_desc) only a window of ~34 pages
    # (~1000 reviews) even for a logged-in session, while the API
    # payload knows the full total (measured 2026-09-16: total=4470,
    # default stream EOF at 34 pages / 1020 unique, of which only 20
    # of 96 one-star reviews; a previous 5814-review product also
    # capped at ~990). The OTHER sorts open different windows:
    # score_asc starts from the low-star reviews that are almost
    # absent from the default window. Streams are walked with the
    # same nextPage chain and merged by review_id.
    _EXTRA_STREAMS: tuple[str, ...] = ("score_asc", "score_desc")

    # Additional FILTER windows (opt-in via --filter-streams). The
    # webListReviews widget exposes ``filters: {withPhotos,
    # withMedia}``; each active filter is its own list ordering and
    # therefore its own ~7k-review window (measured 2026-09-17:
    # each sort window tops out at ~7080-7266 unique). Photo/video
    # reviews ranked below the plain windows' cutoffs become
    # reachable through the filter window. If the param guess is
    # wrong Ozon serves the default stream — the cross-duplicate
    # streak stop (``--dup-streak-stop``) bounds that waste to a
    # few pages.
    _FILTER_STREAMS: tuple[tuple[str, str], ...] = (
        ("&withPhotos=true", "withPhotos"),
        ("&withMedia=true", "withMedia"),
    )

    async def iter_all_reviews(
            self,
            product_url: str,
            *,
            strategy: str = "auto",
            max_reviews: int | None = None,
            pagination_max_pages: int | None = None,
            pagination_start_page: int = 1,
            scroll_max_rounds: int = 500,
            page_delay_seconds: float = 1.5,
            scroll_pause_seconds: float = 1.0,
            retry_attempts: int = 3,
            extra_streams: bool = True,
            parallel_streams: bool = False,
            filter_streams: bool = False,
            dup_streak_stop: int = 300,
    ) -> AsyncIterator[Review]:
        """Stream every review for an Ozon product.

        Strategy modes:

        - ``"auto"`` (default): run pagination first, then scroll as a
          fallback / supplement. Reviews are deduplicated by their
          stable id (``reviewId`` / ``uuid``) across both strategies.
        - ``"pagination"``: only the internal Ozon API pagination.
        - ``"scroll"``: only the DOM scroll.

        When ``extra_streams`` is True (default), after the main
        stream ends the adapter walks the additional sorts from
        :attr:`_EXTRA_STREAMS` and yields only reviews not seen in
        earlier streams — this is what lifts the collection beyond
        the default ~1000-review window.

        ``parallel_streams=True`` (pagination strategy only) runs all
        streams CONCURRENTLY — each opens its own browser session and
        walks its nextPage chain independently, so wall time ≈ the
        slowest stream instead of the sum (~3x for default +
        score_asc + score_desc).

        Always streams — never materializes the full set in memory.
        """
        from shared.logging import get_logger

        log = get_logger("marketplaces.ozon")

        product_id = extract_ozon_product_id(product_url)
        product_path = extract_ozon_product_path(product_url)

        product = ProductRef(
            marketplace=self.name,
            source_url=product_url,
            product_id=str(product_id),
        )

        self.last_rating_summary = None
        self.last_product_title = None
        self.last_review_count = None
        await self._preflight_reported_review_count(product_path)
        seen_keys: set[str] = set()
        yielded = 0

        if strategy == "pagination":
            streams: list[tuple[str, str, int]] = [
                ("", "default", pagination_start_page),
            ]
            if extra_streams:
                streams += [
                    (f"&sort={s}", s, 1)
                    for s in self._EXTRA_STREAMS
                ]
            if filter_streams:
                streams += [
                    (query, label, 1)
                    for query, label in self._FILTER_STREAMS
                ]
            if parallel_streams and len(streams) > 1:
                async for item in self._iter_streams_concurrently(
                    product_url=product_url,
                    streams=streams,
                    max_reviews=max_reviews,
                    pagination_max_pages=pagination_max_pages,
                    dup_streak_stop=dup_streak_stop,
                ):
                    yield item
                return
            for extra_query, label, start_page in streams:
                if (
                    max_reviews is not None
                    and yielded >= max_reviews
                ):
                    return
                if (
                    label == "default"
                    and self._has_collected_reported_total(yielded)
                ):
                    self._print_total_reached(yielded)
                    return
                if (
                    label != "default"
                    and self._has_collected_reported_total(yielded)
                ):
                    self._print_total_reached(yielded)
                    return
                if label != "default":
                    print(
                        f"Ozon: дополнительный стрим {label} — "
                        "ищу отзывы вне дефолтного окна"
                    )
                stream_new = 0
                # Cross-stream duplicate streak: consecutive reviews
                # already claimed by earlier streams. A long streak
                # means the stream is re-serving known ground (e.g.
                # an ignored filter param) — stop it instead of
                # walking hundreds of duplicate pages.
                dup_streak = 0
                async for stream_review in self.iter_reviews(
                    product_url=product_url,
                    start_page=start_page,
                    max_pages=pagination_max_pages,
                    extra_query=extra_query,
                ):
                    key = (
                        stream_review.review_id
                        or build_review_key(
                            stream_review,
                            page_number=0,
                            position=yielded,
                        )
                    )
                    if key in seen_keys:
                        if dup_streak_stop > 0:
                            dup_streak += 1
                            if dup_streak >= dup_streak_stop:
                                print(
                                    f"Ozon: стрим {label}: "
                                    f"{dup_streak} подряд дубликатов "
                                    f"— останавливаю стрим"
                                )
                                break
                        continue
                    dup_streak = 0
                    seen_keys.add(key)
                    stream_new += 1
                    yielded += 1
                    yield stream_review
                    if (
                        max_reviews is not None
                        and yielded >= max_reviews
                    ):
                        return
                print(
                    f"Ozon: стрим {label}: +{stream_new} новых "
                    f"(всего уникальных: {yielded})"
                )
            return

        if strategy == "scroll":
            async for scroll_review in self.iter_reviews_by_scroll(
                product_url=product_url,
                max_reviews=max_reviews,
            ):
                yield scroll_review
            return

        if strategy != "auto":
            raise ValueError(
                f"Unknown strategy: {strategy!r}. "
                "Use 'auto', 'pagination', or 'scroll'."
            )

        # ------------------ auto: pagination + scroll ------------------
        if not hasattr(
            self.browser_transport, "iter_all_ozon_reviews"
        ):
            log.warning(
                "Ozon: transport does not implement "
                "iter_all_ozon_reviews; using adapter-level fallback."
            )
            async for fallback_review in (
                self._iter_all_reviews_adapter_fallback(
                    product=product,
                    product_path=product_path,
                    max_reviews=max_reviews,
                    pagination_max_pages=pagination_max_pages,
                    pagination_start_page=pagination_start_page,
                    retry_attempts=retry_attempts,
                )
            ):
                yield fallback_review
            return

        async for strategy_name, node in (
            self.browser_transport.iter_all_ozon_reviews(
                product_path=product_path,
                max_reviews=max_reviews,
                pagination_max_pages=pagination_max_pages,
                pagination_start_page=pagination_start_page,
                scroll_max_rounds=scroll_max_rounds,
                page_delay_seconds=page_delay_seconds,
                scroll_pause_seconds=scroll_pause_seconds,
                retry_attempts=retry_attempts,
            )
        ):
            review: Review | None
            if strategy_name == "pagination":
                review = map_ozon_review_node(
                    node=node,
                    product=product,
                )
            else:
                review = self.parse_ozon_dom_card(
                    card=node,
                    product=product,
                )

            if review is None:
                continue

            key = review.review_id or build_review_key(
                review,
                page_number=0,
                position=yielded,
            )

            if key in seen_keys:
                continue
            seen_keys.add(key)

            yield review
            yielded += 1

            if (
                max_reviews is not None
                and yielded >= max_reviews
            ):
                log.info(
                    "Ozon: reached max_reviews={}, stopping",
                    max_reviews,
                )
                return

        # PublicPageTransport discovers this during the product-page
        # warmup inside its unified iterator. Read it after that
        # iterator is exhausted: its first review is yielded before
        # the transport assigns the captured count.
        self._reported_review_count()

        # --- extra sort streams (beyond the default window) ---
        if extra_streams:
            if self._has_collected_reported_total(yielded):
                self._print_total_reached(yielded)
                return
            for sort_value in self._EXTRA_STREAMS:
                if (
                    max_reviews is not None
                    and yielded >= max_reviews
                ):
                    return
                print(
                    f"Ozon: дополнительный стрим sort={sort_value} — "
                    "ищу отзывы вне дефолтного окна"
                )
                stream_new = 0
                try:
                    async for review in self.iter_reviews(
                        product_url=product_url,
                        start_page=1,
                        max_pages=pagination_max_pages,
                        extra_query=f"&sort={sort_value}",
                    ):
                        key = (
                            review.review_id
                            or build_review_key(
                                review,
                                page_number=0,
                                position=yielded,
                            )
                        )
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        stream_new += 1
                        yielded += 1
                        yield review
                        if (
                            max_reviews is not None
                            and yielded >= max_reviews
                        ):
                            return
                except Exception as exc:
                    log.warning(
                        "Ozon: стрим sort={} не удался: {} — "
                        "пропускаю",
                        sort_value, exc,
                    )
                print(
                    f"Ozon: стрим {sort_value}: +{stream_new} новых "
                    f"(всего уникальных: {yielded})"
                )

        log.info(
            "Ozon: auto strategy done, {} unique reviews",
            yielded,
        )

    async def _iter_streams_concurrently(
            self,
            *,
            product_url: str,
            streams: list[tuple[str, str, int]],
            max_reviews: int | None,
            pagination_max_pages: int | None,
            dup_streak_stop: int = 300,
    ) -> AsyncIterator[Review]:
        """Run ALL review streams concurrently and merge by review_id.

        Each worker walks one stream (``iter_reviews`` → its own
        browser session inside the transport) and claims reviews
        into a shared ``seen_keys`` set before pushing them into the
        queue, so exactly one worker pushes each review. A worker
        that sees ``dup_streak_stop`` CONSECUTIVE reviews already
        claimed by other streams stops itself — that bounds the cost
        of streams that re-serve known ground (measured 2026-09-17:
        an ignored ``withPhotos`` param would just re-walk the
        default window). Wall time ≈ the slowest stream instead of
        the sum of the streams.

        A failed stream is logged and skipped — the others keep
        going. When ``max_reviews`` is reached (or the consumer stops
        iterating), the remaining workers are cancelled in ``finally``
        so no browser session is leaked.
        """
        import asyncio

        from shared.logging import get_logger

        log = get_logger("marketplaces.ozon")

        queue: asyncio.Queue[Review | None] = asyncio.Queue()
        total_streams = len(streams)
        # Shared claim set; the event loop is single-threaded, and
        # ``queue.put`` on an unbounded queue never suspends, so the
        # check-claim-put sequence in a worker is atomic.
        seen_keys: set[str] = set()
        yielded = 0

        async def worker(
            extra_query: str,
            label: str,
            start_page: int,
        ) -> None:
            stream_new = 0
            dup_streak = 0
            try:
                async for review in self.iter_reviews(
                    product_url=product_url,
                    start_page=start_page,
                    max_pages=pagination_max_pages,
                    extra_query=extra_query,
                ):
                    key = review.review_id or build_review_key(
                        review,
                        page_number=0,
                        position=yielded,
                    )
                    if key in seen_keys:
                        if dup_streak_stop > 0:
                            dup_streak += 1
                            if dup_streak >= dup_streak_stop:
                                print(
                                    f"Ozon: стрим {label}: "
                                    f"{dup_streak} подряд "
                                    f"дубликатов — останавливаю "
                                    f"стрим"
                                )
                                break
                        continue
                    dup_streak = 0
                    seen_keys.add(key)
                    stream_new += 1
                    await queue.put(review)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Ozon: параллельный стрим {} не удался: {} — "
                    "пропускаю",
                    label,
                    exc,
                )
            finally:
                print(
                    f"Ozon: стрим {label} завершён "
                    f"(+{stream_new} новых)"
                )
                await queue.put(None)

        tasks = [
            asyncio.create_task(
                worker(extra_query, label, start_page)
            )
            for extra_query, label, start_page in streams
        ]
        print(
            f"Ozon: запускаю {total_streams} стрима параллельно"
        )

        finished = 0
        try:
            while finished < total_streams:
                review = await queue.get()
                if review is None:
                    finished += 1
                    continue

                # Workers claim reviews into shared seen_keys BEFORE
                # pushing, so every queued review is unique — count
                # and yield directly.
                yielded += 1
                yield review

                if (
                    max_reviews is not None
                    and yielded >= max_reviews
                ):
                    return
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            print(
                f"Ozon: параллельный сбор завершён, "
                f"всего уникальных: {yielded}"
            )


    async def _iter_all_reviews_adapter_fallback(
            self,
            product: ProductRef,
            product_path: str,
            *,
            max_reviews: int | None,
            pagination_max_pages: int | None,
            pagination_start_page: int,
            retry_attempts: int = 3,
    ) -> AsyncIterator[Review]:
        """Used when the transport doesn't implement ``iter_all_ozon_reviews``.

        Runs pagination first, then scroll, with adapter-level
        cross-strategy dedup by ``review_id``.
        """
        from shared.logging import get_logger

        log = get_logger("marketplaces.ozon")
        seen_keys: set[str] = set()
        yielded = 0

        # --- pagination ---
        try:
            async for page_num, payload in (
                self.browser_transport.iter_ozon_reviews_json(
                    product_path=product_path,
                    start_page=pagination_start_page,
                    max_pages=pagination_max_pages,
                    retry_attempts=retry_attempts,
                )
            ):
                reviews = extract_reviews_from_ozon_payload(
                    payload=payload,
                    product=product,
                )
                for review in reviews:
                    key = review.review_id or build_review_key(
                        review, page_number=page_num, position=yielded,
                    )
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    yield review
                    yielded += 1
                    if (
                        max_reviews is not None
                        and yielded >= max_reviews
                    ):
                        return
        except Exception as exc:
            log.warning(
                "Ozon: pagination failed in fallback: {} — "
                "trying scroll only",
                exc,
            )

        # --- scroll ---
        try:
            async for cards in (
                self.browser_transport.iter_ozon_reviews_by_scroll(
                    product_path=product_path,
                    max_reviews=None,
                )
            ):
                for card in cards:
                    review = self.parse_ozon_dom_card(
                        card=card, product=product,
                    )
                    if review is None:
                        continue
                    key = review.review_id or build_review_key(
                        review, page_number=0, position=yielded,
                    )
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    yield review
                    yielded += 1
                    if (
                        max_reviews is not None
                        and yielded >= max_reviews
                    ):
                        return
        except Exception as exc:
            log.error(
                "Ozon: scroll failed in fallback: {}", exc,
            )
            if yielded == 0:
                raise

    async def collect_all(
        self,
        product_url: str,
        *,
        start_page: int = 1,
        max_pages: int | None = None,
    ) -> ReviewPage:
        product_id = extract_ozon_product_id(product_url)

        product = ProductRef(
            marketplace=self.name,
            source_url=product_url,
            product_id=str(product_id),
        )

        reviews: list[Review] = []

        async for review in self.iter_reviews(
            product_url=product_url,
            start_page=start_page,
            max_pages=max_pages,
        ):
            reviews.append(review)

        return ReviewPage(
            product=product,
            reviews=reviews,
            total_count=len(reviews),
        )
