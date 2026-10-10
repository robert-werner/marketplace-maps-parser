# src/marketplace_maps_parser/collectors.py
"""Collectors: one per marketplace (extracted from ``__main__.py``).

Each ``_collect_*`` coroutine wires the marketplace adapter to its
transport, streams reviews into the output (with resume / dedup
support) and returns the number of reviews written. The heavy
transports are imported lazily so ``--help`` stays fast.

Two output formats (``--format``):

- ``json`` (default) — the unified document: one ``reviews`` array
  with the shared field set + ``diagnostics`` (errors land there,
  never as review records — see ``shared/unified_format.py``);
- ``jsonl`` — the legacy one-record-per-line stream.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from marketplace_maps_parser.concurrency import (
    PartialCollectionError as PartialCollectionError,
)
from marketplace_maps_parser.concurrency import (
    _iter_parallel_reviews as _iter_parallel_reviews,
)
from marketplace_maps_parser.concurrency import (
    _ParallelYandexSessions,
)
from marketplace_maps_parser.rating_summary import (
    _finalize_rating_summary as _finalize_rating_summary,
)
from marketplace_maps_parser.rating_summary import (
    _scan_output_ratings as _scan_output_ratings,
)
from marketplace_maps_parser.run_state import write_status
from marketplace_maps_parser.runner import run_collection as _run_unified_json
from marketplace_maps_parser.transport_factory import (
    _build_ozon_transport as _build_ozon_transport,
)
from marketplace_maps_parser.transport_factory import (
    _build_proxy_pool as _build_proxy_pool,
)
from marketplace_maps_parser.transport_factory import (
    _build_single_proxy as _build_single_proxy,
)


async def _draw_session_proxies(
    pool: Any,
    count: int,
) -> list[dict[str, str]]:
    """Pin one proxy per parallel session from the pool.

    Stops early when the pool runs dry — the leftover sessions
    then run direct (the caller warns about the shared IP)."""
    proxies: list[dict[str, str]] = []
    if pool is None:
        return proxies
    for _ in range(count):
        try:
            next_async = getattr(pool, "next_async", None)
            proxy = (
                await next_async()
                if next_async is not None
                else pool.next()
            )
        except Exception:
            proxy = None
        if proxy is None:
            break
        proxies.append(proxy)
    return proxies


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


async def _collect_ozon_once(args: argparse.Namespace) -> int:
    # Lazy import: heavy transport modules are imported only when
    # the user selects them.
    from infrastructure.marketplaces.ozon import OzonAdapter

    if not args.debug_dir:
        args.debug_dir = "debug_ozon"

    transport = await _build_ozon_transport(args)
    adapter = OzonAdapter(browser_transport=transport)


    try:
        count = await _run_unified_json(
            args,
            adapter=adapter,
            make_iterator=lambda: adapter.iter_all_reviews(
                product_url=args.url,
                strategy=args.strategy,
                max_reviews=None,
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
            ),
            extra_diagnostics=lambda: {
                "total_count": adapter.last_review_count,
                "average_score": (
                    (adapter.last_rating_summary or {}).get(
                        "average_score",
                    )
                ),
            },
        )
    finally:
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
    if (args.format == "jsonl" and isinstance(summary, dict)
            and summary.get("histogram")):
        added = _finalize_rating_summary(
            output=Path(args.output),
            summary=summary,
            include_rating_only=getattr(
                args, "include_rating_only", False,
            ),
            marketplace=adapter.name,
        )
        count += added
        write_status(
            args.output,
            status=getattr(args, "_run_status", "complete"),
            collected=count,
            total_records=count,
            rating_only_rows=added,
        )

    return count


async def _collect_ozon(args: argparse.Namespace) -> int:
    """Retry failed runs on distinct proxies with fresh browser sessions.

    Keep already-written records via --resume; do not reuse a fingerprint
    or cookies within one poisoned browser session. A partial run without
    an exception (e.g. --max-reviews) is not automatically retried.
    """
    if (
        not args.proxy_list
        or args.proxy
        or getattr(args, "proxy_attempts", 1) == 1
    ):
        return await _collect_ozon_once(args)

    from infrastructure.transports.proxy_pool import ProxyPool

    pool = ProxyPool.from_file(args.proxy_list)
    attempts = min(args.proxy_attempts, pool.size)
    original_resume = args.resume
    count = 0
    try:
        for index in range(attempts):
            pinned_proxy = pool.next()
            if pinned_proxy is None:
                break
            args._pinned_proxy = pinned_proxy
            args.resume = original_resume or index > 0
            # Every attempt starts a new browser and, when resuming,
            # deduplicates records saved by previous attempts.
            count += await _collect_ozon_once(args)
            status = getattr(args, "_run_status", "failed")
            if status == "complete":
                return count
            from marketplace_maps_parser.run_state import status_path

            try:
                import json

                metadata = json.loads(
                    status_path(args.output).read_text(encoding="utf-8"),
                )
            except (OSError, ValueError):
                metadata = {}
            if not metadata.get("error"):
                return count
            pool.mark_blocked(pinned_proxy)
            if index + 1 < attempts:
                print(
                    f"Ozon: ошибка с proxy #{index + 1}; "
                    "пробую новую браузерную сессию с другим proxy"
                )
        return count
    finally:
        args.resume = original_resume
        if hasattr(args, "_pinned_proxy"):
            delattr(args, "_pinned_proxy")


async def _probe_yandex_page_count(
    args: argparse.Namespace,
    *,
    parallel: int,
    proxy: dict[str, str] | None,
    cookies: list[dict[str, Any]] | None,
    debug_dir: str,
) -> int | None:
    """Measure the product's review counter with ONE probe session
    and translate it into a page-range size for ``--parallel-
    sessions`` (so ``--max-pages`` is optional for Yandex.Market).

    Returns ``None`` when the counter cannot be read (a captcha
    that outlives the ladder, a page without JSON-LD counters) —
    the caller then downgrades to a single session instead of
    guessing a range."""
    from infrastructure.transports.yandex_browser import (
        YandexBrowserTransport,
    )
    from marketplace_maps_parser.parallel_sessions import (
        estimate_review_pages,
    )

    print("Я.Маркет: замеряю счётчик отзывов перед сессиями…")
    probe = YandexBrowserTransport(
        timeout_ms=args.timeout_ms,
        settle_ms=args.settle_ms,
        debug_dir=debug_dir,
        proxy=proxy,
        # NOT the pool on purpose: the probe must not consume a
        # proxy pinned for one of the sessions.
        cookies=cookies,
        humanize=not args.no_humanize,
        cookies_path=(args.save_cookies or "yandex_cookies.json"),
        use_api=not getattr(args, "no_browser_api", False),
    )
    try:
        total = await probe.fetch_total_count(args.url)
    except Exception as exc:
        print(f"[warning] Я.Маркет: замер не удался: {exc}")
        return None
    if not total:
        print(
            "[warning] Я.Маркет: счётчик отзывов на странице "
            "не найден"
        )
        return None
    pages = estimate_review_pages(total, parallel)
    print(
        f"Я.Маркет: счётчик отзывов: {total} ≈ {pages} страниц "
        f"(по ~10 отзывов на страницу + запас на сессию)"
    )
    return pages


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

    parallel = max(
        1, getattr(args, "parallel_sessions", 1) or 1,
    )
    pages_total: int | None = args.max_pages
    if parallel > 1 and not pages_total:
        # Probe the review counter FIRST (one quick session), then
        # size the parallel ranges from it — --max-pages becomes
        # optional. A failed probe downgrades to a single session
        # (its walk handles the captcha ladder + proxy rotation
        # itself) instead of guessing a range.
        pages_total = await _probe_yandex_page_count(
            args,
            parallel=parallel,
            proxy=proxy,
            cookies=cookies,
            debug_dir=debug_dir,
        )
        if not pages_total:
            print(
                "[warning] Я.Маркет: размер диапазона страниц "
                "неизвестен — запускаю одну сессию без "
                "--parallel-sessions"
            )
            parallel = 1
    if parallel > 1:
        # In-process page-range sessions (the wall-time multiplier
        # measured for Ozon child processes applies to independent
        # browser launches too): one stealth browser per disjoint
        # [start, start+max_pages) chunk, one pinned proxy each.
        from marketplace_maps_parser.parallel_sessions import (
            split_page_range,
        )

        chunks = split_page_range(
            args.start_page or 1, pages_total, parallel,
        )
        session_proxies = await _draw_session_proxies(
            proxy_pool, len(chunks),
        )
        if not session_proxies and not proxy:
            print(
                "WARNING: --parallel-sessions без отдельных proxy — "
                "все сессии пойдут с одного IP, риск капчи растёт"
            )
        save_path = args.save_cookies or "yandex_cookies.json"
        session_adapters = []
        for i, (start, size) in enumerate(chunks):
            # Distinct pool proxies when available; the single
            # --proxy is shared by everyone (the Ozon precedent:
            # works, but the sessions share one IP).
            session_proxy = (
                session_proxies[i % len(session_proxies)]
                if session_proxies
                else proxy
            )
            session_transport = YandexBrowserTransport(
                use_api=not getattr(args, "no_browser_api", False),
                timeout_ms=args.timeout_ms,
                settle_ms=args.settle_ms,
                debug_dir=debug_dir,
                proxy=session_proxy,
                cookies=cookies,
                humanize=not args.no_humanize,
                # Only session 0 checkpoints the cookie jar — N
                # sessions writing one file would clobber it.
                cookies_path=(
                    save_path if i == 0 else None
                ),
                start_page=start,
                max_pages=size,
                dup_pages_stop=getattr(
                    args, "dup_pages_stop", 3,
                ),
            )
            session_adapters.append(
                YandexMarketAdapter(
                    browser_transport=session_transport,
                ),
            )
            print(
                f"Я.Маркет: сессия {i + 1}/{len(chunks)} — "
                f"страницы {start}..{start + size - 1}, proxy: "
                + (
                    session_proxy.get("server", "?")
                    if session_proxy
                    else "напрямую"
                )
            )
        adapter = _ParallelYandexSessions(session_adapters)
    else:
        transport = YandexBrowserTransport(
            use_api=not getattr(args, "no_browser_api", False),
            timeout_ms=args.timeout_ms,
            settle_ms=args.settle_ms,
            debug_dir=debug_dir,
            proxy=proxy,
            proxy_pool=proxy_pool,
            cookies=cookies,
            humanize=not args.no_humanize,
            dup_pages_stop=getattr(args, "dup_pages_stop", 3),
            start_page=args.start_page,
            max_pages=args.max_pages,
            # NOTE: block_assets stays at the transport default
            # (False) — a real browser loads images/fonts and
            # SmartCaptcha weighs that; --no-block-assets is an
            # Ozon-side flag.
            cookies_path=(
                args.save_cookies or "yandex_cookies.json"
            ),
        )
        adapter = YandexMarketAdapter(browser_transport=transport)

    return await _run_unified_json(
        args,
        adapter=adapter,
        make_iterator=lambda: adapter.iter_reviews(
            args.url,
        ),
        extra_diagnostics=lambda: {
            "total_count": adapter.last_total_count,
            "average_rating": adapter.last_average_rating,
        },
    )


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
        use_direct_api=not getattr(args, "no_browser_api", False),
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
        # Direct-API speed knobs: concurrent streams + per-stream
        # pacing (defaults match the CLI flags).
        api_pacing_seconds=getattr(
            args, "maps_api_pacing", 0.8,
        ),
        api_concurrency=getattr(
            args, "maps_api_concurrency", 3,
        ),
    )
    adapter = YandexMapsAdapter(browser_transport=transport)

    return await _run_unified_json(
        args,
        adapter=adapter,
        make_iterator=lambda: adapter.iter_reviews(
            args.url,
        ),
        extra_diagnostics=lambda: {
            "total_count": adapter.last_total_count,
            "average_rating": adapter.last_average_rating,
            "rating_count": adapter.last_rating_count,
        },
    )


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
        use_api=not getattr(args, "no_browser_api", False),
        max_pages=args.max_pages,
        page_delay_seconds=args.page_delay_seconds,
        timeout_ms=args.timeout_ms,
        settle_ms=args.settle_ms,
        debug_dir=debug_dir,
        proxy=proxy,
        proxy_pool=proxy_pool,
        cookies=cookies,
        humanize=not args.no_humanize,
    )
    adapter = TwoGisAdapter(browser_transport=transport)

    return await _run_unified_json(
        args,
        adapter=adapter,
        make_iterator=lambda: adapter.iter_reviews(
            args.url,
        ),
        extra_diagnostics=lambda: {
            "total_count": adapter.last_total_count,
            "average_rating": adapter.last_average_rating,
        },
    )


async def _collect_wildberries(args: argparse.Namespace) -> int:
    # Lazy import: keeps the --help path dependency-light.
    from infrastructure.marketplaces.wildberries import (
        WildberriesAdapter,
    )
    from infrastructure.transports.cookie_loader import load_cookies_file
    from infrastructure.transports.wb_browser import (
        WildberriesBrowserTransport,
    )

    proxy = _build_single_proxy(args) if args.proxy else None
    if proxy is None:
        selected = await _draw_session_proxies(
            await _build_proxy_pool(args), 1,
        )
        proxy = selected[0] if selected else None
    transport = WildberriesBrowserTransport(
        product_url=args.url, timeout_ms=args.timeout_ms,
        settle_ms=args.settle_ms, proxy=proxy,
        humanize=not args.no_humanize,
        cookies=load_cookies_file(args.cookies) if args.cookies else None,
        use_api=not getattr(args, "no_browser_api", False),
        max_pages=args.max_pages,
    )
    adapter = WildberriesAdapter(transport)
    return await _run_unified_json(
        args, adapter=adapter,
        make_iterator=lambda: adapter.iter_reviews(args.url),
        extra_diagnostics=lambda: {
            "total_count": adapter.last_total_count,
            "average_rating": adapter.last_average_rating,
            "collection_path": transport.collection_path,
        },
    )


async def _collect_avito(args: argparse.Namespace) -> int:
    from infrastructure.marketplaces.avito import AvitoAdapter
    from infrastructure.transports.avito_browser import AvitoBrowserTransport
    from infrastructure.transports.cookie_loader import load_cookies_file

    proxy = _build_single_proxy(args) if args.proxy else None
    if proxy is None:
        pool = await _build_proxy_pool(args)
        selected = await _draw_session_proxies(pool, 1)
        proxy = selected[0] if selected else None
    transport = AvitoBrowserTransport(
        proxy=proxy,
        cookies=load_cookies_file(args.cookies) if args.cookies else None,
        timeout_ms=args.timeout_ms, settle_ms=args.settle_ms,
        humanize=not args.no_humanize,
        use_api=not args.no_browser_api, max_pages=args.max_pages,
        page_delay_seconds=args.page_delay_seconds,
    )
    adapter = AvitoAdapter(transport)
    return await _run_unified_json(
        args, adapter=adapter,
        make_iterator=lambda: adapter.iter_reviews(args.url),
        extra_diagnostics=lambda: {
            "total_count": adapter.last_total_count,
            "average_rating": adapter.last_average_rating,
            "collection_path": transport.collection_path,
        },
    )
