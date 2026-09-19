# src/marketplace_maps_parser/collectors.py
"""Collectors: one per marketplace (extracted from ``__main__.py``).

Each ``_collect_*`` coroutine wires the marketplace adapter to its
transport, streams reviews into the output JSONL (with resume /
dedup support) and returns the number of reviews written. The
heavy transports are imported lazily so ``--help`` stays fast.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _review_to_record(review: Any) -> dict[str, Any]:
    return {
        "review_id": review.review_id,
        "product_id": review.product.product_id,
        "marketplace": review.product.marketplace,
        "rating": review.rating,
        "text": review.text,
        "author": review.author,
        "created_at": review.created_at,
        "pros": review.pros,
        "cons": review.cons,
        "seller_answer": review.seller_answer,
        "raw": review.raw,
    }


def _load_existing_reviews(
    output: Path,
) -> set[str]:
    """Read ``review_id`` values from an existing JSONL file.

    Used by ``--resume`` to skip reviews already collected in a
    previous run. Returns an empty set if the file does not exist or
    cannot be parsed (so a corrupted file does not block a fresh run).
    """
    if not output.exists():
        return set()

    seen: set[str] = set()
    try:
        with output.open("r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rid = record.get("review_id")
                if rid:
                    seen.add(str(rid))
    except OSError:
        return set()

    return seen


def _scan_output_ratings(
    output: Path,
    product_id: str,
) -> tuple[dict[str, int], dict[str, int]]:
    """Scan the output JSONL: total rows per star and existing
    synthetic rating-only rows per star (ids prefixed
    ``<product_id>-ro-<star>-``).
    """
    per_star: dict[str, int] = {}
    synth: dict[str, int] = {}
    prefix = f"{product_id}-ro-"
    try:
        with output.open("r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rating = record.get("rating")
                if rating is None or isinstance(rating, bool):
                    continue
                try:
                    star = str(int(rating))
                except (TypeError, ValueError):
                    continue
                per_star[star] = per_star.get(star, 0) + 1
                rid = str(record.get("review_id") or "")
                if rid.startswith(f"{prefix}{star}-"):
                    synth[star] = synth.get(star, 0) + 1
    except OSError:
        pass
    return per_star, synth


def _finalize_rating_summary(
    *,
    output: Path,
    summary: dict[str, Any],
    include_rating_only: bool,
    marketplace: str,
) -> int:
    """Write ``<output>.summary.json``; with ``include_rating_only``
    also append synthetic rows for the rating-only remainder.

    Returns the number of synthetic rows appended this run.
    """
    histogram = summary.get("histogram") or {}
    product_id = str(summary.get("product_id") or "")
    if not histogram or not product_id:
        return 0

    per_star, synth = _scan_output_ratings(output, product_id)

    remainder = {
        star: max(0, int(count) - per_star.get(star, 0))
        for star, count in histogram.items()
    }

    added = 0
    if include_rating_only and any(remainder.values()):
        with output.open("a", encoding="utf-8") as file:
            for star in sorted(remainder, reverse=True):
                need = remainder[star]
                if need <= 0:
                    continue
                start = synth.get(star, 0)
                for n in range(start + 1, start + need + 1):
                    record = {
                        "review_id": (
                            f"{product_id}-ro-{star}-{n:05d}"
                        ),
                        "product_id": product_id,
                        "marketplace": marketplace,
                        "rating": int(star),
                        "text": None,
                        "author": None,
                        "created_at": None,
                        "pros": None,
                        "cons": None,
                        "seller_answer": None,
                        "raw": {
                            "synthetic": True,
                            "source": (
                                "webReviewProductScore histogram"
                            ),
                            "note": (
                                "Оценка без отзыва: Ozon не отдаёт "
                                "такие записи по отдельности"
                            ),
                        },
                    }
                    file.write(
                        json.dumps(
                            record,
                            ensure_ascii=False,
                            default=str,
                        )
                        + "\n"
                    )
                    added += 1

    summary_record = {
        "product_id": product_id,
        "product_url": summary.get("product_url"),
        "average_score": summary.get("average_score"),
        "site_ratings_total": summary.get("reviews_count"),
        "site_histogram": histogram,
        "rows_per_star_in_file": per_star,
        "rating_only_per_star": remainder,
        "synthetic_rows_appended_this_run": added,
        "synthetic_rows_total_in_file": sum(synth.values()) + added,
    }
    summary_path = Path(str(output) + ".summary.json")
    summary_path.write_text(
        json.dumps(
            summary_record,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    histogram_preview = " ".join(
        f"{star}*={count}"
        for star, count in sorted(
            histogram.items(),
            reverse=True,
        )
    )
    print(
        f"Ozon: гистограмма оценок: {histogram_preview}; "
        f"оценок без текста (нельзя собрать индивидуально): "
        f"{sum(remainder.values())}"
        + (
            f"; добавлено синтетических строк: {added}"
            if added
            else ""
        )
        + f"; сводка: {summary_path.name}"
    )
    return added


async def _collect_ozon(args: argparse.Namespace) -> int:
    # Lazy import: heavy transport modules are imported only when
    # the user selects them.
    from infrastructure.marketplaces.ozon import OzonAdapter

    if not args.debug_dir:
        args.debug_dir = "debug_ozon"

    transport = await _build_ozon_transport(args)
    adapter = OzonAdapter(browser_transport=transport)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    # ``--resume``: load review_ids already in the output file so we
    # don't re-emit them. Open in append mode so the new run extends
    # the file rather than overwriting it.
    if args.resume:
        seen_ids = _load_existing_reviews(output)
        if seen_ids:
            print(
                f"Resume: {len(seen_ids)} reviews already in "
                f"{output.name}, will skip them."
            )
        file_mode = "a"
    else:
        seen_ids = set()
        file_mode = "w"

    count = 0

    # When using curl_cffi, scroll strategy is not supported —
    # silently coerce it to pagination to avoid a NotImplementedError
    # at fetch time.
    effective_strategy = args.strategy
    if args.transport == "curl_cffi" and effective_strategy == "scroll":
        print(
            "[info] --transport curl_cffi не поддерживает "
            "--strategy scroll; переключаю на pagination"
        )
        effective_strategy = "pagination"
    elif (
        args.transport == "curl_cffi"
        and effective_strategy == "auto"
    ):
        print(
            "[info] --transport curl_cffi: auto strategy "
            "эквивалентна pagination (scroll не поддерживается)"
        )
        effective_strategy = "pagination"

    try:
        with output.open(file_mode, encoding="utf-8") as file:
            async for review in adapter.iter_all_reviews(
                product_url=args.url,
                strategy=effective_strategy,
                max_reviews=args.max_reviews,
                pagination_max_pages=args.max_pages,
                pagination_start_page=args.start_page,
                page_delay_seconds=args.page_delay_seconds,
                scroll_pause_seconds=args.scroll_pause_seconds,
                retry_attempts=args.retry_attempts,
                extra_streams=not args.no_extra_streams,
                parallel_streams=getattr(
                    args, "parallel_streams", False,
                ),
                filter_streams=getattr(
                    args, "filter_streams", False,
                ),
                dup_streak_stop=getattr(
                    args, "dup_streak_stop", 300,
                ),
            ):
                review_id = review.review_id
                if review_id and review_id in seen_ids:
                    continue
                if review_id:
                    seen_ids.add(review_id)

                file.write(
                    json.dumps(
                        _review_to_record(review),
                        ensure_ascii=False,
                        default=str,
                    )
                    + "\n"
                )
                file.flush()
                count += 1

                if count % 100 == 0:
                    print(f"Собрано отзывов: {count}")
    finally:
        # Ensure the transport's HTTP session is closed (curl_cffi
        # holds a connection pool that should be released).
        close = getattr(transport, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:
                pass

    # Ozon: rating histogram summary. Rating-only «оценки» (stars
    # without text) are not exposed individually by Ozon — only as
    # histogram counts. Write a summary file and, with
    # --include-rating-only, append synthetic rows for the remainder.
    summary = getattr(adapter, "last_rating_summary", None)
    if isinstance(summary, dict) and summary.get("histogram"):
        added = _finalize_rating_summary(
            output=output,
            summary=summary,
            include_rating_only=getattr(
                args, "include_rating_only", False,
            ),
            marketplace=adapter.name,
        )
        count += added

    return count


async def _build_ozon_transport(
    args: argparse.Namespace,
) -> Any:
    """Construct the Ozon transport based on --transport.

    Returns an object that implements the OzonBrowserTransport
    Protocol (iter_ozon_reviews_json, iter_ozon_reviews_by_scroll,
    iter_all_ozon_reviews, get_ozon_reviews_json).
    """
    # Build proxy pool / single proxy from CLI args.
    from infrastructure.transports.proxy_pool import proxy_to_url

    proxy_pool = await _build_proxy_pool(args)
    single_proxy = _build_single_proxy(args) if proxy_pool is None else None

    # playwright/hybrid drive ONE browser session and accept a
    # single proxy. Without this, --proxy-list would be silently
    # ignored for them and ALL traffic would go direct from this
    # machine (privacy + rotation loss). Take the next proxy from
    # the pool for the whole run.
    if (
        proxy_pool is not None
        and args.transport in ("playwright", "hybrid")
    ):
        pool_next = getattr(proxy_pool, "next_async", None)
        single_proxy = (
            await pool_next()
            if pool_next is not None
            else proxy_pool.next()
        )
        if single_proxy is None:
            print(
                "[warning] все proxy пула заблокированы — "
                "запуск напрямую с этого IP"
            )
        else:
            print(
                f"[info] {args.transport}-транспорт: один proxy на "
                f"весь запуск — {single_proxy.get('server', '?')} "
                "(построчная ротация только у public_page)"
            )

    cookies = None
    if args.cookies:
        from infrastructure.transports.cookie_loader import (
            load_cookies_file,
        )
        cookies = load_cookies_file(args.cookies)
        print(
            f"Ozon: загружено cookies из {args.cookies}: "
            f"{len(cookies)} шт."
        )

    if args.transport == "public_page":
        from infrastructure.transports.public_page import (
            PublicPageTransport,
        )
        return PublicPageTransport(
            timeout_ms=args.timeout_ms,
            settle_ms=args.settle_ms,
            debug_dir=args.debug_dir,
            proxy=single_proxy,
            proxy_pool=proxy_pool,
            humanize=not args.no_humanize,
            stealth=not args.no_stealth,
            randomize_fingerprint=args.randomize_fingerprint,
            cookies=cookies,
            workers=args.workers,
            widget_scroll=not args.no_widget_scroll,
            block_assets=not args.no_block_assets,
            screenshots=args.screenshots,
        )

    if args.transport == "curl_cffi":
        from infrastructure.transports.curl_cffi import (
            CurlCffiTransport,
        )
        # curl_cffi takes a proxy URL string, not a dict.
        proxy_url = (
            proxy_to_url(single_proxy)
            if single_proxy is not None
            else None
        )
        return CurlCffiTransport(
            timeout=args.timeout_ms / 1000.0,
            debug_dir=args.debug_dir,
            impersonate=args.impersonate,
            proxy=proxy_url,
        )

    if args.transport == "hybrid":
        from infrastructure.transports.hybrid import HybridTransport
        # Hybrid takes a playwright proxy dict + curl_cffi proxy URL.
        curl_proxy_url = (
            proxy_to_url(single_proxy)
            if single_proxy is not None
            else None
        )
        return HybridTransport(
            curl_cffi_kwargs={
                "timeout": args.timeout_ms / 1000.0,
                "impersonate": args.impersonate,
                "proxy": curl_proxy_url,
            },
            playwright_kwargs={
                "timeout_ms": args.timeout_ms,
                "settle_ms": args.settle_ms,
                "proxy": single_proxy,
                "humanize": not args.no_humanize,
                "fetch_strategy": args.fetch_strategy,
                "stealth": not args.no_stealth,
                "cookies": cookies,
                "block_assets": not args.no_block_assets,
            },
            debug_dir=args.debug_dir,
        )

    # default: playwright
    from infrastructure.transports.browser_json import (
        BrowserJsonTransport,
    )
    return BrowserJsonTransport(
        timeout_ms=args.timeout_ms,
        settle_ms=args.settle_ms,
        debug_dir=args.debug_dir,
        proxy=single_proxy,
        humanize=not args.no_humanize,
        fetch_strategy=args.fetch_strategy,
        stealth=not args.no_stealth,
        cookies=cookies,
        screenshots=args.screenshots,
        block_assets=not args.no_block_assets,
    )


async def _build_proxy_pool(
    args: argparse.Namespace,
) -> Any:
    """Build a proxy pool from --proxy-list or --free-proxy.

    Returns None if neither was provided.

    Priority: --proxy-list > --free-proxy (proxy-list takes
    precedence because residential proxies from a file are more
    reliable than free public proxies).
    """
    if args.proxy_list:
        from infrastructure.transports.proxy_pool import ProxyPool
        return ProxyPool.from_file(args.proxy_list)

    if args.free_proxy:
        from infrastructure.transports.free_proxy_pool import (
            FreeProxyPool,
        )
        country_id = None
        if args.free_proxy_country:
            country_id = [
                c.strip() for c in args.free_proxy_country.split(",")
                if c.strip()
            ]
        print(
            "[info] --free-proxy: загружаю бесплатные публичные "
            "proxy через free-proxy package..."
            + (f" (country={country_id})" if country_id else "")
            + (" (elite)" if args.free_proxy_elite else "")
        )
        # Async factory: the free-proxy batch fetch runs in a
        # worker thread so the event loop is never blocked.
        return await FreeProxyPool.create_async(
            country_id=country_id,
            elite=args.free_proxy_elite,
        )

    return None


def _build_single_proxy(
    args: argparse.Namespace,
) -> dict[str, str] | None:
    """Build a single proxy dict from --proxy. Returns None if no
    single proxy was provided."""
    if not args.proxy:
        return None
    from infrastructure.transports.proxy_pool import parse_proxy_line
    proxy = parse_proxy_line(args.proxy)
    if proxy is None:
        raise SystemExit(
            f"Invalid --proxy format: {args.proxy!r}. "
            "Expected: 'http://host:port' or "
            "'http://user:pass@host:port' or 'socks5://host:port'"
        )
    return proxy


async def _collect_yandex(args: argparse.Namespace) -> int:
    """Collect Yandex.Market reviews into the output JSONL."""
    from infrastructure.marketplaces.yandex import (
        YandexMarketAdapter,
    )
    from infrastructure.transports.yandex_browser import (
        YandexBrowserTransport,
    )

    # Proxy handling: a single --proxy pins the egress IP; a
    # --proxy-list builds a pool so a captcha that survives the
    # escalation ladder rotates the browser onto a fresh IP
    # (state — seen cards / page number — survives the restart).
    proxy = None
    proxy_pool = None
    if args.proxy:
        proxy = _build_single_proxy(args)
    elif args.proxy_list or args.free_proxy:
        proxy_pool = await _build_proxy_pool(args)
        if proxy_pool is None:
            print(
                "[warning] yandex: proxy-пул пуст — запуск "
                "напрямую с этого IP"
            )

    cookies = None
    if args.cookies:
        from infrastructure.transports.cookie_loader import (
            load_cookies_file,
        )
        cookies = load_cookies_file(args.cookies)
        print(
            f"Я.Маркет: загружено cookies из {args.cookies}: "
            f"{len(cookies)} шт."
        )

    debug_dir = args.debug_dir or "debug_yandex"

    transport = YandexBrowserTransport(
        timeout_ms=args.timeout_ms,
        settle_ms=args.settle_ms,
        debug_dir=debug_dir,
        proxy=proxy,
        proxy_pool=proxy_pool,
        cookies=cookies,
        humanize=not args.no_humanize,
        # NOTE: block_assets stays at the transport default (False)
        # — a real browser loads images/fonts and SmartCaptcha weighs
        # that; --no-block-assets is an Ozon-side flag.
        cookies_path=(
            args.save_cookies or "yandex_cookies.json"
        ),
    )
    adapter = YandexMarketAdapter(browser_transport=transport)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    if args.resume:
        seen_ids = _load_existing_reviews(output)
        if seen_ids:
            print(
                f"Resume: {len(seen_ids)} отзывов уже в "
                f"{output.name}, будут пропущены."
            )
        file_mode = "a"
    else:
        seen_ids = set()
        file_mode = "w"

    count = 0

    with output.open(file_mode, encoding="utf-8") as file:
        async for review in adapter.iter_reviews(args.url):
            review_id = review.review_id
            if review_id and review_id in seen_ids:
                continue
            if review_id:
                seen_ids.add(review_id)

            file.write(
                json.dumps(
                    _review_to_record(review),
                    ensure_ascii=False,
                    default=str,
                )
                + "\n"
            )
            file.flush()
            count += 1

            if count % 100 == 0:
                print(f"Собрано отзывов: {count}")

            if (
                args.max_reviews is not None
                and count >= args.max_reviews
            ):
                print(
                    f"Достигнут лимит --max-reviews: "
                    f"{args.max_reviews}"
                )
                break

    total = adapter.last_total_count
    if total is not None:
        print(
            f"Я.Маркет: по данным сайта всего отзывов: {total}; "
            f"собрано: {count}"
        )

    return count


async def _collect_yandex_maps(args: argparse.Namespace) -> int:
    """Collect Yandex.Maps org reviews into the output JSONL."""
    from infrastructure.marketplaces.yandex_maps import (
        YandexMapsAdapter,
    )
    from infrastructure.transports.yandex_maps_browser import (
        YandexMapsBrowserTransport,
    )

    # Same proxy handling as the Yandex.Market flow: a single
    # --proxy pins the egress IP; with a --proxy-list the transport
    # takes one proxy for the whole session.
    proxy = None
    proxy_pool = None
    if args.proxy:
        proxy = _build_single_proxy(args)
    elif args.proxy_list or args.free_proxy:
        proxy_pool = await _build_proxy_pool(args)
        if proxy_pool is None:
            print(
                "[warning] yandex_maps: proxy-пул пуст — запуск "
                "напрямую с этого IP"
            )

    cookies = None
    if args.cookies:
        from infrastructure.transports.cookie_loader import (
            load_cookies_file,
        )
        cookies = load_cookies_file(args.cookies)
        print(
            f"Я.Карты: загружено cookies из {args.cookies}: "
            f"{len(cookies)} шт."
        )

    debug_dir = args.debug_dir or "debug_yandex_maps"

    transport = YandexMapsBrowserTransport(
        timeout_ms=args.timeout_ms,
        settle_ms=args.settle_ms,
        debug_dir=debug_dir,
        proxy=proxy,
        proxy_pool=proxy_pool,
        cookies=cookies,
        humanize=not args.no_humanize,
        cookies_path=(
            args.save_cookies or "yandex_maps_cookies.json"
        ),
        # Ozon-style extra streams: after the default ranking's
        # ~600-review window walk the other rankings + aspect chips.
        walk_extra_streams=not args.no_extra_streams,
        # Review-count dup guard shared with the Ozon streams; 0 =
        # full drain of every window (slowest, most complete).
        dup_streak_stop=getattr(args, "dup_streak_stop", 300),
    )
    adapter = YandexMapsAdapter(browser_transport=transport)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    if args.resume:
        seen_ids = _load_existing_reviews(output)
        if seen_ids:
            print(
                f"Resume: {len(seen_ids)} отзывов уже в "
                f"{output.name}, будут пропущены."
            )
        file_mode = "a"
    else:
        seen_ids = set()
        file_mode = "w"

    count = 0

    with output.open(file_mode, encoding="utf-8") as file:
        async for review in adapter.iter_reviews(args.url):
            review_id = review.review_id
            if review_id and review_id in seen_ids:
                continue
            if review_id:
                seen_ids.add(review_id)

            file.write(
                json.dumps(
                    _review_to_record(review),
                    ensure_ascii=False,
                    default=str,
                )
                + "\n"
            )
            file.flush()
            count += 1

            if count % 100 == 0:
                print(f"Собрано отзывов: {count}")

            if (
                args.max_reviews is not None
                and count >= args.max_reviews
            ):
                print(
                    f"Достигнут лимит --max-reviews: "
                    f"{args.max_reviews}"
                )
                break

    total = adapter.last_total_count
    if total is not None:
        rating_note = ""
        if adapter.last_average_rating is not None:
            rating_note = (
                f", рейтинг организации: "
                f"{adapter.last_average_rating}"
            )
        rating_only_note = ""
        if (
            adapter.last_rating_count is not None
            and adapter.last_rating_count > total
        ):
            rating_only_note = (
                f"; оценок без отзыва (не собираются "
                f"индивидуально): "
                f"{adapter.last_rating_count - total}"
            )
        print(
            f"Я.Карты: по данным сайта всего отзывов: {total}; "
            f"собрано: {count}{rating_note}{rating_only_note}"
        )

    return count


async def _collect_2gis(args: argparse.Namespace) -> int:
    """Collect 2GIS firm reviews into the output JSONL."""
    from infrastructure.marketplaces.two_gis import TwoGisAdapter
    from infrastructure.transports.two_gis_browser import (
        TwoGisBrowserTransport,
    )

    proxy = None
    proxy_pool = None
    if args.proxy:
        proxy = _build_single_proxy(args)
    elif args.proxy_list or args.free_proxy:
        proxy_pool = await _build_proxy_pool(args)
        if proxy_pool is None:
            print(
                "[warning] 2gis: proxy-пул пуст — запуск "
                "напрямую с этого IP"
            )

    cookies = None
    if args.cookies:
        from infrastructure.transports.cookie_loader import (
            load_cookies_file,
        )
        cookies = load_cookies_file(args.cookies)
        print(
            f"2ГИС: загружено cookies из {args.cookies}: "
            f"{len(cookies)} шт."
        )

    debug_dir = args.debug_dir or "debug_2gis"

    transport = TwoGisBrowserTransport(
        timeout_ms=args.timeout_ms,
        settle_ms=args.settle_ms,
        debug_dir=debug_dir,
        proxy=proxy,
        proxy_pool=proxy_pool,
        cookies=cookies,
        humanize=not args.no_humanize,
    )
    adapter = TwoGisAdapter(browser_transport=transport)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    if args.resume:
        seen_ids = _load_existing_reviews(output)
        if seen_ids:
            print(
                f"Resume: {len(seen_ids)} отзывов уже в "
                f"{output.name}, будут пропущены."
            )
        file_mode = "a"
    else:
        seen_ids = set()
        file_mode = "w"

    count = 0

    with output.open(file_mode, encoding="utf-8") as file:
        async for review in adapter.iter_reviews(args.url):
            review_id = review.review_id
            if review_id and review_id in seen_ids:
                continue
            if review_id:
                seen_ids.add(review_id)

            file.write(
                json.dumps(
                    _review_to_record(review),
                    ensure_ascii=False,
                    default=str,
                )
                + "\n"
            )
            file.flush()
            count += 1

            if (
                args.max_reviews is not None
                and count >= args.max_reviews
            ):
                print(
                    f"Достигнут лимит --max-reviews: "
                    f"{args.max_reviews}"
                )
                break

    total = adapter.last_total_count
    if total is not None:
        rating_note = ""
        if adapter.last_average_rating is not None:
            rating_note = (
                f", рейтинг организации: "
                f"{adapter.last_average_rating}"
            )
        print(
            f"2ГИС: по данным сайта всего отзывов: {total}; "
            f"собрано: {count}{rating_note}"
        )

    return count


async def _collect_wildberries(args: argparse.Namespace) -> int:
    # Lazy import: keeps the --help path dependency-light.
    from infrastructure.marketplaces.wildberries import (
        WildberriesAdapter,
    )
    from infrastructure.transports.http import HttpJsonTransport

    async with HttpJsonTransport() as transport:
        adapter = WildberriesAdapter(transport)
        page = await adapter.collect(args.url)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    with output.open("w", encoding="utf-8") as file:
        for review in page.reviews:
            file.write(
                json.dumps(
                    _review_to_record(review),
                    ensure_ascii=False,
                    default=str,
                )
                + "\n"
            )

    return len(page.reviews)


