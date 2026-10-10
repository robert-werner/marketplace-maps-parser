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
import signal
import sys
from pathlib import Path
from typing import Any

from marketplace_maps_parser.merging import merge_parts
from marketplace_maps_parser.run_state import status_path


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
    args: Any, part_path: Path, start_page: int,
    max_pages: int, proxy_url: str | None,
) -> list[str]:
    cmd = _product_child_command(args, args.url, part_path, proxy_url)
    # Override parent scope: each child owns precisely one page range.
    cmd += [
        "--strategy", "pagination", "--start-page", str(start_page),
        "--max-pages", str(max_pages), "--no-extra-streams",
    ]
    return cmd


async def _stop_child(proc: Any) -> None:
    """Give the child time to checkpoint on SIGINT, then reap it."""
    if proc.returncode is not None:
        return
    try:
        proc.send_signal(signal.SIGINT)
    except (ProcessLookupError, OSError):
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


async def _run_child(cmd: list[str], env: dict[str, str]) -> int:
    spawning = asyncio.create_task(asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=None, env=env,
    ))
    proc: Any = None
    try:
        # Cancellation between process creation and handle delivery must
        # not orphan a browser process.
        proc = await asyncio.shield(spawning)
        return int(await proc.wait())
    except asyncio.CancelledError:
        if proc is None:
            proc = await spawning
        await _stop_child(proc)
        raise


def _cleanup_parts(parts: list[Path], status: str) -> None:
    if status != "complete":
        return  # keep partial parts for diagnosis/recovery
    for path in parts:
        path.unlink(missing_ok=True)
        status_path(path).unlink(missing_ok=True)


def _prepare_part(path: Path, resume: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not resume:
        # Never let an old part masquerade as a newly failed child.
        path.unlink(missing_ok=True)
        status_path(path).unlink(missing_ok=True)
        Path(f"{path}.checkpoint.jsonl").unlink(missing_ok=True)


async def run_parallel_sessions(args: Any) -> int:
    """Run bounded Ozon page ranges and merge in the requested format."""
    if args.marketplace != "ozon":
        raise SystemExit("--parallel-sessions supports only Ozon")
    if args.max_pages is None:
        raise SystemExit("--parallel-sessions requires --max-pages")
    chunks = split_page_range(
        args.start_page, args.max_pages, args.parallel_sessions,
    )
    proxies = _proxy_urls_for_sessions(args, len(chunks))
    out = Path(args.output)
    parts = [out.with_name(f"{out.name}.part{i}.jsonl")
             for i in range(len(chunks))]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    exits: list[int | None] = [None] * len(parts)

    async def run_one(i: int) -> None:
        _prepare_part(parts[i], getattr(args, "resume", False))
        start, size = chunks[i]
        try:
            exits[i] = await _run_child(
                _child_command(args, parts[i], start, size, proxies[i]), env,
            )
        except OSError as exc:
            print(f"Child {i} could not start: {exc}")
            exits[i] = -1

    tasks = [asyncio.create_task(run_one(i)) for i in range(len(parts))]
    interrupted = False
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        interrupted = True
        raise
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        count, status = merge_parts(
            parts, out, output_format=getattr(args, "format", "jsonl"),
            exit_codes=exits, max_reviews=args.max_reviews,
            resume=getattr(args, "resume", False),
            interrupted=interrupted, page_ranges=True,
        )
        args._run_status = status
        _cleanup_parts(parts, status)
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
    args: Any, url: str, part_path: Path, proxy_url: str | None,
) -> list[str]:
    """Forward browser/output settings, but never recurse into supervisors."""
    cmd = [
        sys.executable, "-m", "marketplace_maps_parser",
        "--marketplace", "ozon", "--url", url,
        "--output", str(part_path),
        "--format", getattr(args, "format", "jsonl"),
        "--transport", args.transport, "--strategy", args.strategy,
    ]
    if proxy_url:
        cmd += ["--proxy", proxy_url]
    for name in (
        "cookies", "timeout_ms", "settle_ms", "retry_attempts",
        "max_reviews", "max_pages", "start_page", "fetch_strategy",
        "page_delay_seconds", "scroll_pause_seconds", "dup_streak_stop",
        "checkpoint_interval", "checkpoint_seconds",
        "proxy_attempts",
    ):
        value = getattr(args, name, None)
        if value is not None:
            cmd += ["--" + name.replace("_", "-"), str(value)]
    debug_root = getattr(args, "debug_dir", None) or "debug_ozon"
    cmd += ["--debug-dir", str(Path(debug_root) / part_path.name)]
    for name in (
        "no_stealth", "no_humanize", "no_block_assets", "screenshots",
        "no_extra_streams", "parallel_streams", "filter_streams",
        "include_rating_only", "resume", "debug_dumps",
    ):
        if getattr(args, name, False):
            cmd.append("--" + name.replace("_", "-"))
    if not getattr(args, "parallel_streams", True):
        cmd.append("--serial-streams")
    return cmd


def _count_jsonl_lines(path: Path) -> int:
    """Non-empty line count of a JSONL part (0 if missing)."""
    try:
        with open(path, encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


def _count_child_records(path: Path) -> int:
    """Count records in either a unified JSON or JSONL child part."""
    try:
        text = path.read_text(encoding="utf-8")
        document = json.loads(text)
    except (OSError, json.JSONDecodeError):
        return _count_jsonl_lines(path)
    if isinstance(document, dict) and isinstance(
        document.get("reviews"), list,
    ):
        return len(document["reviews"])
    return _count_jsonl_lines(path)


async def run_products_parallel(args: Any) -> int:
    """One process per product, bounded by --products-sessions."""
    if args.marketplace != "ozon":
        raise SystemExit("--products-file supports only --marketplace ozon")
    urls = read_products_file(args.products_file)
    sessions = max(1, getattr(args, "products_sessions", 3))
    out = Path(args.output)
    parts = [out.with_name(f"{out.name}.p{i:03d}.jsonl")
             for i in range(len(urls))]
    proxies = _proxy_urls_for_sessions(args, len(urls))
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    exits: list[int | None] = [None] * len(urls)
    sem = asyncio.Semaphore(sessions)
    # Invalidate stale files even for children still waiting on the semaphore.
    for part in parts:
        _prepare_part(part, getattr(args, "resume", False))

    async def run_one(i: int) -> None:
        async with sem:
            print(f"[{i + 1}/{len(urls)}] {urls[i]}")
            try:
                exits[i] = await _run_child(
                    _product_child_command(
                        args, urls[i], parts[i], proxies[i],
                    ),
                    env,
                )
            except OSError as exc:
                print(f"Child {i} could not start: {exc}")
                exits[i] = -1

    tasks = [asyncio.create_task(run_one(i)) for i in range(len(urls))]
    interrupted = False
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        interrupted = True
        raise
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        count, status = merge_parts(
            parts, out, output_format=getattr(args, "format", "jsonl"),
            exit_codes=exits, resume=getattr(args, "resume", False),
            interrupted=interrupted,
        )
        args._run_status = status
        _cleanup_parts(parts, status)
    print(f"products: {count} reviews → {out} ({status})")
    return count
