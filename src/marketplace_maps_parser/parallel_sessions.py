# src/marketplace_maps_parser/parallel_sessions.py
"""Multi-process collection for one product: split the page range
into disjoint chunks, run one CLI child per chunk (each with its own
proxy), then merge the parts with review_id dedup.

Multi-PRODUCT collection (``--products-file``): one CLI child per
product, bounded concurrency (``--products-sessions``), one proxy
per product from ``--proxy-list`` — the reliable wall-time
multiplier (measured 2026-09-16: tabs of one session serialize;
independent processes scale linearly).

Why processes and not tabs: measured 2026-09-15, tabs of a single
browser session serialize (one Firefox + one proxy tunnel), while
independent processes scale linearly. The chunks use the classic
URL pagination (``--strategy pagination``), which honors
``--start-page`` / ``--max-pages`` — deep naked ``?page=N`` URLs
require a logged-in session (``--cookies``), anonymous access caps
at ~5 pages.

Public helpers ``split_page_range`` and ``merge_jsonl_dedup`` are
pure and unit-tested; ``run_parallel_sessions`` /
``run_products_parallel`` orchestrate the child processes.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any


def estimate_review_pages(
    total_reviews: int,
    sessions: int,
    *,
    per_page: int = 10,
) -> int:
    """Estimate the ``?page=N`` range covering ``total_reviews``.

    ``ceil(total/per_page)`` plus one margin page per session. The
    margin leans on the walk's duplicate-streak stop: a session
    that runs past the real end costs at most a few duplicate
    pages and stops, while an UNDERestimate would silently drop
    tail reviews (a session's ``max_pages`` bound ends it with
    cards still uncollected).

    Yandex.Market measured 2026-09-18: ~10 DOM cards per reviews
    page; the Show-More expansion can only pull pages FORWARD
    (more per page), never fewer.
    """
    if total_reviews <= 0:
        return 0
    pages = -(-total_reviews // per_page)
    return pages + max(1, sessions)


def split_page_range(
    start_page: int,
    max_pages: int,
    sessions: int,
) -> list[tuple[int, int]]:
    """Split [start_page, start_page + max_pages) into ``sessions``
    contiguous chunks. Returns ``[(start, pages), ...]``; the last
    chunk takes the remainder. Sessions larger than the page count
    get empty chunks dropped."""
    if sessions <= 0:
        raise ValueError("sessions must be >= 1")
    if max_pages <= 0:
        raise ValueError("max_pages must be >= 1")
    per = max_pages // sessions
    rem = max_pages % sessions
    chunks: list[tuple[int, int]] = []
    cur = start_page
    for i in range(sessions):
        size = per + (1 if i < rem else 0)
        if size == 0:
            continue
        chunks.append((cur, size))
        cur += size
    return chunks


def merge_jsonl_dedup(
    part_paths: list[Path],
    out_path: Path,
    *,
    max_reviews: int | None = None,
) -> int:
    """Merge JSONL parts into ``out_path`` deduplicating by
    ``review_id``. Returns the number of unique reviews written."""
    seen: set[str] = set()
    count = 0
    with open(out_path, "w", encoding="utf-8") as out:
        for path in part_paths:
            if not path.exists():
                continue
            with open(path, encoding="utf-8") as part:
                for line in part:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        review = json.loads(line)
                    except ValueError:
                        continue
                    rid = review.get("review_id")
                    if rid is None or rid in seen:
                        continue
                    seen.add(rid)
                    out.write(
                        json.dumps(
                            review, ensure_ascii=False,
                        ) + "\n"
                    )
                    count += 1
                    if (
                        max_reviews is not None
                        and count >= max_reviews
                    ):
                        return count
    return count


def _proxy_urls_for_sessions(
    args: Any, sessions: int
) -> list[str | None]:
    """One proxy URL per child session.

    Prefers distinct entries from --proxy-list (the pool exists for
    exactly this); falls back to the single --proxy for everyone;
    returns Nones (direct) when neither is set."""
    if args.proxy_list:
        from infrastructure.transports.proxy_pool import (
            parse_proxy_file,
            proxy_to_url,
        )

        proxies = parse_proxy_file(args.proxy_list)
        urls: list[str | None] = []
        for i in range(sessions):
            if proxies:
                p = proxies[i % len(proxies)]
                urls.append(proxy_to_url(p))
            else:
                urls.append(None)
        return urls

    if args.proxy:
        # Same exit for everyone — works, but the sessions share
        # bandwidth; distinct --proxy-list entries are better.
        return [args.proxy] * sessions

    return [None] * sessions


def _child_command(
    args: Any,
    part_path: Path,
    start_page: int,
    max_pages: int,
    proxy_url: str | None,
) -> list[str]:
    cmd = [
        sys.executable, "-m", "marketplace_maps_parser",
        "--marketplace", "ozon",
        "--url", args.url,
        "--output", str(part_path),
        "--transport", "public_page",
        "--strategy", "pagination",
        "--start-page", str(start_page),
        "--max-pages", str(max_pages),
        "--workers", "1",
        "--retry-attempts", str(min(args.retry_attempts, 20)),
    ]
    if proxy_url:
        cmd += ["--proxy", proxy_url]
    if args.cookies:
        cmd += ["--cookies", args.cookies]
    if args.timeout_ms:
        cmd += ["--timeout-ms", str(args.timeout_ms)]
    if args.settle_ms:
        cmd += ["--settle-ms", str(args.settle_ms)]
    if args.debug_dir:
        cmd += ["--debug-dir", str(args.debug_dir)]
    if args.no_stealth:
        cmd += ["--no-stealth"]
    if args.no_humanize:
        cmd += ["--no-humanize"]
    if args.no_widget_scroll:
        cmd += ["--no-widget-scroll"]
    return cmd


async def run_parallel_sessions(args: Any) -> int:
    """Spawn one CLI child per page chunk, wait for all, merge the
    parts into ``args.output`` (dedup by review_id). Returns the
    unique review count."""
    sessions = args.parallel_sessions
    if args.marketplace != "ozon":
        raise SystemExit(
            "--parallel-sessions поддерживает только --marketplace ozon"
        )
    if args.transport != "public_page":
        raise SystemExit(
            "--parallel-sessions работает с --transport public_page"
        )
    if args.max_pages is None:
        raise SystemExit(
            "--parallel-sessions требует --max-pages (общее число "
            "страниц: ~отзывы/30); диапазоны делятся между сессиями"
        )
    if not args.cookies:
        print(
            "WARNING: --parallel-sessions без --cookies: анонимный "
            "доступ ограничен ~5 страницами на сессию"
        )

    chunks = split_page_range(
        args.start_page or 1, args.max_pages, sessions,
    )
    proxies = _proxy_urls_for_sessions(args, len(chunks))
    out_path = Path(args.output)
    part_paths = [
        out_path.with_name(
            f"{out_path.name}.part{i}.jsonl"
        )
        for i in range(len(chunks))
    ]

    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")

    print(
        f"parallel-sessions: {len(chunks)} процессов по страницам "
        + ", ".join(
            f"[{s}..{s + n - 1}]"
            for (s, n), _ in zip(
                chunks, part_paths, strict=False
            )
        )
    )

    tasks = []
    for (start, size), part_path, proxy_url in zip(
        chunks, part_paths, proxies, strict=False
    ):
        cmd = _child_command(
            args, part_path, start, size, proxy_url
        )
        part_path.parent.mkdir(parents=True, exist_ok=True)
        from infrastructure.transports.proxy_pool import (
            mask_proxy_url,
        )

        print(
            f"  часть {part_path.name}: страницы {start}.."
            f"{start + size - 1}, proxy: "
            f"{mask_proxy_url(proxy_url) if proxy_url else 'напрямую'}"
        )
        tasks.append(
            asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=None,  # наследуем stderr родителя
                env=env,
            )
        )
    procs = await asyncio.gather(*tasks)

    for (start, size), proc in zip(
        chunks, procs, strict=False
    ):
        if proc.returncode not in (0, None):
            print(
                f"WARNING: часть страниц {start}..{start + size - 1} "
                f"завершилась с кодом {proc.returncode}"
            )
    await asyncio.gather(
        *(p.wait() for p in procs), return_exceptions=True
    )

    count = merge_jsonl_dedup(
        part_paths, out_path, max_reviews=args.max_reviews,
    )
    for part_path in part_paths:
        try:
            part_path.unlink()
        except OSError:
            pass
    return count


# ---------------------------------------------------------------------------
# Multi-PRODUCT collection (--products-file)
# ---------------------------------------------------------------------------


def read_products_file(path: str | Path) -> list[str]:
    """Read product URLs from a file (one per line, ``#`` comments
    and blank lines allowed).

    Raises ``FileNotFoundError`` for a missing file and
    ``ValueError`` for a file with no URLs.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Products file not found: {p}")

    urls: list[str] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            urls.append(line)

    if not urls:
        raise ValueError(f"Products file {p} contains no URLs")
    return urls


def _product_child_command(
    args: Any,
    url: str,
    part_path: Path,
    proxy_url: str | None,
) -> list[str]:
    """One CLI child per product, honoring the caller's flags."""
    cmd = [
        sys.executable, "-m", "marketplace_maps_parser",
        "--marketplace", "ozon",
        "--url", url,
        "--output", str(part_path),
        "--transport", args.transport,
        "--strategy", args.strategy,
        "--retry-attempts", str(min(args.retry_attempts, 20)),
    ]
    if proxy_url:
        cmd += ["--proxy", proxy_url]
    if args.max_reviews is not None:
        cmd += ["--max-reviews", str(args.max_reviews)]
    if args.cookies:
        cmd += ["--cookies", args.cookies]
    if args.timeout_ms:
        cmd += ["--timeout-ms", str(args.timeout_ms)]
    if args.settle_ms:
        cmd += ["--settle-ms", str(args.settle_ms)]
    if args.debug_dir:
        cmd += ["--debug-dir", str(args.debug_dir)]
    if getattr(args, "workers", 1) != 1:
        cmd += ["--workers", str(args.workers)]
    if args.no_stealth:
        cmd += ["--no-stealth"]
    if args.no_humanize:
        cmd += ["--no-humanize"]
    if args.no_widget_scroll:
        cmd += ["--no-widget-scroll"]
    if getattr(args, "randomize_fingerprint", False):
        cmd += ["--randomize-fingerprint"]
    return cmd


def _count_jsonl_lines(path: Path) -> int:
    """Non-empty line count of a JSONL part (0 if missing)."""
    try:
        with open(path, encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


async def run_products_parallel(args: Any) -> int:
    """Collect reviews for MANY products in parallel.

    One CLI child process per product URL from ``--products-file``,
    at most ``--products-sessions`` children running at a time, each
    with its own proxy from ``--proxy-list`` (round-robin; falls
    back to the single ``--proxy`` / direct). Review ids are
    globally unique, so all parts merge safely into ``--output``
    with review_id dedup. Failed children still contribute whatever
    they collected before failing. Returns the total unique review
    count.
    """
    if args.marketplace != "ozon":
        raise SystemExit(
            "--products-file поддерживает только --marketplace ozon"
        )
    urls = read_products_file(args.products_file)
    sessions = max(1, getattr(args, "products_sessions", 3) or 1)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    part_paths = [
        out_path.with_name(f"{out_path.name}.p{i:03d}.jsonl")
        for i in range(len(urls))
    ]
    proxies = _proxy_urls_for_sessions(args, len(urls))

    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")

    from infrastructure.transports.proxy_pool import (
        mask_proxy_url,
    )

    print(
        f"products: {len(urls)} товаров, "
        f"параллельно до {sessions} процессов"
    )

    sem = asyncio.Semaphore(sessions)
    exit_codes: list[int | None] = [None] * len(urls)

    async def _run_one(i: int) -> None:
        async with sem:
            cmd = _product_child_command(
                args, urls[i], part_paths[i], proxies[i],
            )
            print(
                f"  [{i + 1}/{len(urls)}] {urls[i]} — proxy: "
                + (
                    mask_proxy_url(proxies[i])
                    if proxies[i] else "напрямую"
                )
            )
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=None,  # наследуем stderr родителя
                    env=env,
                )
            except OSError as exc:
                print(
                    f"  [{i + 1}/{len(urls)}] запуск не удался: "
                    f"{exc}"
                )
                exit_codes[i] = -1
                return
            exit_codes[i] = await proc.wait()

    await asyncio.gather(
        *(_run_one(i) for i in range(len(urls)))
    )

    for i, (url, part, code) in enumerate(
        zip(urls, part_paths, exit_codes, strict=True)
    ):
        status = "OK" if code == 0 else f"код {code}"
        print(
            f"  [{i + 1}/{len(urls)}] {url}: "
            f"{_count_jsonl_lines(part)} отзывов ({status})"
        )

    total = merge_jsonl_dedup(part_paths, out_path)
    for part_path in part_paths:
        try:
            part_path.unlink()
        except OSError:
            pass
    print(
        f"products: слито в {out_path} — {total} уникальных отзывов"
    )
    return total
