"""Bounded live comparison of Ozon strategies, all via Invisible Playwright.

Example (run from the project root):
    .venv/bin/python scripts/benchmark_ozon.py --url <product-url> \
        --cookies cookie.json --proxy-list proxies.txt --proxy-index 51

Writes review files plus timings. Does not print cookies or proxy addresses.
An intentional review/page limit is reported as partial, not a complete crawl.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from infrastructure.marketplaces.ozon import OzonAdapter
from infrastructure.transports.browser_json import BrowserJsonTransport
from infrastructure.transports.cookie_loader import load_cookies_file
from infrastructure.transports.proxy_pool import parse_proxy_file
from marketplace_maps_parser.cli_args import parse_args
from marketplace_maps_parser.runner import run_collection
from shared.async_iterators import closing_iterator


class TimedTransport(BrowserJsonTransport):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.api_seconds: list[float] = []

    async def _fetch_json_inside_page(self, **kwargs: Any) -> dict[str, Any]:
        started = time.perf_counter()
        result = await super()._fetch_json_inside_page(**kwargs)
        self.api_seconds.append(round(time.perf_counter() - started, 4))
        return result


async def benchmark(options: argparse.Namespace) -> list[dict[str, Any]]:
    proxy = None
    if options.proxy_list:
        proxies = parse_proxy_file(options.proxy_list)
        if not 1 <= options.proxy_index <= len(proxies):
            raise ValueError("--proxy-index is outside the proxy list")
        proxy = proxies[options.proxy_index - 1]
    cookies = load_cookies_file(options.cookies) if options.cookies else None
    output_dir = Path(options.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for mode in options.strategies:
        args = parse_args([
            "--url", options.url,
            "--output", str(output_dir / f"{mode}.json"),
            "--max-reviews", str(options.max_reviews),
            "--max-pages", str(options.max_pages),
        ])
        transport = TimedTransport(
            proxy=proxy, cookies=cookies, seed=options.seed,
            fetch_strategy="auto" if mode == "scroll" else mode,
            timeout_ms=options.timeout_ms,
            page_delay_seconds=options.page_delay_seconds,
        )
        adapter = OzonAdapter(browser_transport=transport)
        started = time.perf_counter()
        first_review_seconds = None

        async def reviews(
            adapter=adapter, mode=mode, started=started,
        ):
            nonlocal first_review_seconds
            async with closing_iterator(adapter.iter_all_reviews(
                options.url,
                strategy="scroll" if mode == "scroll" else "pagination",
                extra_streams=False, parallel_streams=False,
                pagination_max_pages=options.max_pages, retry_attempts=1,
            )) as stream:
                async for review in stream:
                    if first_review_seconds is None:
                        first_review_seconds = round(
                            time.perf_counter() - started, 4,
                        )
                    yield review

        print(f"Benchmark Ozon: {mode}, limit={options.max_reviews}")
        timed_out = False
        try:
            count = await asyncio.wait_for(
                run_collection(
                    args, adapter=adapter, make_iterator=reviews,
                    extra_diagnostics=lambda adapter=adapter: {
                        "total_count": adapter.last_review_count,
                    },
                ),
                timeout=options.run_timeout_seconds,
            )
        except TimeoutError:
            # run_collection saves its checkpoint on cancellation.
            # Keep the timed-out variant in the report and test the next one.
            timed_out = True
            document = json.loads(Path(args.output).read_text())
            count = document["diagnostics"]["total_records"]
        elapsed = time.perf_counter() - started
        results.append({
            "strategy": mode, "reviews": count,
            "target_reached": count >= options.max_reviews,
            "timed_out": timed_out,
            "status": args._run_status,
            "elapsed_seconds": round(elapsed, 4),
            "first_review_seconds": first_review_seconds,
            "reviews_per_second": round(count / elapsed, 3),
            "api_seconds": transport.api_seconds,
            "output": str(Path(args.output).resolve()),
        })
        report = {
            "timestamp": datetime.now(UTC).isoformat(),
            "url": options.url, "runtime": "Invisible Playwright",
            "proxy_index": options.proxy_index if proxy else None,
            "max_reviews": options.max_reviews, "max_pages": options.max_pages,
            "page_delay_seconds": options.page_delay_seconds,
            "results": results,
        }
        (output_dir / "benchmark.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        print(json.dumps(results[-1], ensure_ascii=False))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--cookies")
    parser.add_argument("--proxy-list")
    parser.add_argument("--proxy-index", type=int, default=1)
    parser.add_argument(
        "--strategies", nargs="+",
        choices=("auto", "navigation", "fetch", "scroll"),
        default=["navigation", "auto"],
    )
    parser.add_argument("--max-reviews", type=int, default=150)
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument("--timeout-ms", type=int, default=45_000)
    parser.add_argument("--run-timeout-seconds", type=float, default=180)
    parser.add_argument("--page-delay-seconds", type=float, default=0.8)
    parser.add_argument("--seed", type=int, default=20261010)
    parser.add_argument(
        "--output-dir",
        default="data/ozon_benchmark_" + datetime.now(UTC).strftime(
            "%Y%m%d_%H%M%S",
        ),
    )
    options = parser.parse_args()
    for key in (
        "max_reviews", "max_pages", "timeout_ms", "run_timeout_seconds",
    ):
        if getattr(options, key) <= 0:
            parser.error(f"--{key.replace('_', '-')} must be positive")
    if options.page_delay_seconds < 0:
        parser.error("--page-delay-seconds must be non-negative")
    asyncio.run(benchmark(options))


if __name__ == "__main__":
    main()
