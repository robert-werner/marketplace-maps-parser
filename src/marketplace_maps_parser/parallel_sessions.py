# src/marketplace_maps_parser/parallel_sessions.py
"""Multi-process collection for one product: split the page range
into disjoint chunks, run one CLI child per chunk (each with its own
proxy), then merge the parts with review_id dedup.

Why processes and not tabs: measured 2026-09-15, tabs of a single
browser session serialize (one Firefox + one proxy tunnel), while
independent processes scale linearly. The chunks use the classic
URL pagination (``--strategy pagination``), which honors
``--start-page`` / ``--max-pages`` — deep naked ``?page=N`` URLs
require a logged-in session (``--cookies``), anonymous access caps
at ~5 pages.

Public helpers ``split_page_range`` and ``merge_jsonl_dedup`` are
pure and unit-tested; ``run_parallel_sessions`` orchestrates the
child processes.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any


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
    from urllib.parse import urlparse

    if args.proxy_list:
        from infrastructure.transports.proxy_pool import (
            parse_proxy_file,
        )

        proxies = parse_proxy_file(args.proxy_list)
        urls: list[str | None] = []
        for i in range(sessions):
            if proxies:
                p = proxies[i % len(proxies)]
                url = p["server"]
                if p.get("username"):
                    parsed = urlparse(url)
                    url = (
                        f"{parsed.scheme}://{p['username']}:"
                        f"{p.get('password', '')}@{parsed.hostname}"
                        f":{parsed.port}"
                    )
                urls.append(url)
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
            for (s, n), _ in zip(chunks, part_paths)
        )
    )

    tasks = []
    for (start, size), part_path, proxy_url in zip(
        chunks, part_paths, proxies
    ):
        cmd = _child_command(
            args, part_path, start, size, proxy_url
        )
        debug_dir = Path(args.debug_dir or "debug_ozon")
        part_path.parent.mkdir(parents=True, exist_ok=True)
        print(
            f"  часть {part_path.name}: страницы {start}.."
            f"{start + size - 1}, proxy: "
            f"{proxy_url or 'напрямую'}"
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

    for (start, size), proc in zip(chunks, procs):
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
