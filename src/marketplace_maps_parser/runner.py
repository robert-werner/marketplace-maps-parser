"""One lifecycle for all collectors, formats, cancellation and checkpoints."""
from __future__ import annotations

import argparse
import asyncio
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from domain.entities import Review
from marketplace_maps_parser.output import ReviewWriter
from marketplace_maps_parser.run_state import infer_status


async def run_collection(
    args: argparse.Namespace, *, adapter: Any,
    make_iterator: Callable[[], AsyncIterator[Review]],
    extra_diagnostics: Callable[[], dict[str, Any]] | None = None,
) -> int:
    writer = ReviewWriter(
        Path(args.output), output_format=args.format,
        resume=args.resume, source_url=args.url,
    )
    count = 0
    error: str | None = None
    interrupted = False
    stopped_by_limit = False
    checkpoint_count = max(1, getattr(args, "checkpoint_interval", 100))
    checkpoint_seconds = max(0.01, getattr(args, "checkpoint_seconds", 5.0))
    started = time.monotonic()
    iterator = make_iterator()
    last_saved = writer.total

    def details() -> dict[str, Any]:
        result = extra_diagnostics() if extra_diagnostics else {}
        transport = getattr(adapter, "transport", None)
        if transport is None:
            transport = getattr(adapter, "browser_transport", None)
        if result.get("total_count") is None:
            value = getattr(transport, "last_total_count", None)
            if value is not None:
                result["total_count"] = value
        reason = getattr(transport, "incomplete_reason", None)
        if reason:
            result["incomplete_reason"] = reason
        path = getattr(transport, "collection_path", None)
        if path:
            result["collection_path"] = path
        api_reason = getattr(transport, "api_fallback_reason", None)
        if api_reason:
            result["api_fallback_reason"] = api_reason
        return result

    def snapshot() -> dict[str, Any]:
        return {
            **details(), "status": "partial", "error": error,
            "collected": count, "interrupted": interrupted,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }

    async def periodic_checkpoint() -> None:
        nonlocal last_saved
        while True:
            await asyncio.sleep(checkpoint_seconds)
            if writer.total != last_saved:
                writer.checkpoint(snapshot())
                last_saved = writer.total

    # A run that is killed before its first record is still marked unfinished.
    try:
        writer.checkpoint(snapshot())
    except BaseException:
        writer.close()
        raise
    ticker = asyncio.create_task(periodic_checkpoint())
    try:
        async for review in iterator:
            if ticker.done():
                ticker.result()  # checkpoint I/O errors must not be hidden
            if not writer.add(review):
                continue
            count += 1
            if writer.total - last_saved >= checkpoint_count:
                writer.checkpoint(snapshot())
                last_saved = writer.total
            if count % 100 == 0:
                print(f"Собрано отзывов: {count}")
            if args.max_reviews is not None and count >= args.max_reviews:
                stopped_by_limit = True
                break
    except asyncio.CancelledError:
        interrupted = True
        error = "CancelledError: collection interrupted"
        raise
    except KeyboardInterrupt:
        interrupted = True
        error = "KeyboardInterrupt: collection interrupted"
        raise
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        print(f"Сбор прерван ошибкой: {error}")
    finally:
        ticker.cancel()
        ticker_result = await asyncio.gather(ticker, return_exceptions=True)
        for result in ticker_result:
            if isinstance(result, Exception) and error is None:
                error = f"Checkpoint error: {result}"
        close = getattr(iterator, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception as exc:
                error = error or f"Cleanup error: {exc}"
        diagnostics = details()
        expected = diagnostics.get("total_count")
        # WB currently exposes ratings, not a count of readable reviews.
        if getattr(args, "marketplace", None) == "wildberries":
            diagnostics["rating_count"] = expected
            expected = None
            diagnostics.pop("total_count", None)
        if not isinstance(expected, int) or isinstance(expected, bool):
            expected = None
        scope_limited = (
            getattr(args, "start_page", 1) > 1
            or getattr(args, "max_pages", None) is not None
        )
        proven = expected is not None and writer.total >= expected
        incomplete = diagnostics.get("incomplete_reason")
        status = infer_status(
            error=error, interrupted=interrupted, record_count=writer.total,
            expected_count=expected,
            stopped_by_limit=(
                bool(incomplete) or ((stopped_by_limit or scope_limited)
                                     and not proven)
            ),
        )
        diagnostics.update(
            status=status, error=error, collected=count,
            interrupted=interrupted, expected_count=expected,
            completeness_verified=proven,
            stop_reason=(
                "cancelled" if interrupted else "error" if error
                else "max_reviews" if stopped_by_limit else "exhausted"
            ),
            elapsed_seconds=round(time.monotonic() - started, 3),
        )
        args._run_status = status
        try:
            writer.finish(
                diagnostics,
                product_title=getattr(adapter, "last_product_title", None),
            )
        finally:
            writer.close()
    return count
