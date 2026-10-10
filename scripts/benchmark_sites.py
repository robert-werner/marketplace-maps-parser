"""Bounded live smoke benchmarks using Invisible Playwright only."""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from infrastructure.transports.proxy_pool import parse_proxy_file, proxy_to_url
from marketplace_maps_parser.__main__ import _run
from marketplace_maps_parser.cli_args import parse_args

URLS = {
    "wildberries": (
        "https://www.wildberries.ru/catalog/150479920/detail.aspx?targetUrl=MI"
    ),
    "yandex": (
        "https://market.yandex.ru/card/"
        "prisposobleniye-karetka-dlya-vyravnivaniya-ploskosti-frezerom/"
        "103791325581"
    ),
    "avito": "https://www.avito.ru/brands/i219481394/all",
    "2gis": "https://2gis.ru/moscow/firm/70000001063167147",
    "yandex_maps": (
        "https://yandex.ru/maps/org/"
        "otdeleniye_pochtovoy_svyazi_430028/1120018525/"
    ),
}


async def benchmark(options: argparse.Namespace) -> None:
    output_dir = Path(options.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    proxy = None
    if options.proxy_list:
        proxies = parse_proxy_file(options.proxy_list)
        if not 1 <= options.proxy_index <= len(proxies):
            raise ValueError("--proxy-index is outside the proxy list")
        proxy = proxy_to_url(proxies[options.proxy_index - 1])
    results: list[dict[str, Any]] = []
    for site in options.sites:
        for mode in options.modes:
            output = output_dir / f"{site}_{mode}.json"
            args = parse_args([
                "--url", URLS[site], "--output", str(output),
                "--max-reviews", str(options.max_reviews),
                "--max-pages", str(options.max_pages),
                "--timeout-ms", str(options.timeout_ms),
            ])
            args.no_browser_api = mode == "dom"
            args.proxy = proxy
            cookie_name = {
                "yandex": "yandex_cookies.json",
                "yandex_maps": "yandex_maps_cookies.json",
            }.get(site)
            if cookie_name:
                cookie_path = Path(options.cookies_dir) / cookie_name
                if cookie_path.exists():
                    args.cookies = str(cookie_path)
            print(f"Benchmark {site}: {mode}, limit={options.max_reviews}")
            timed_out = False
            try:
                await asyncio.wait_for(
                    _run(args), timeout=options.run_timeout_seconds,
                )
            except TimeoutError:
                timed_out = True  # runner has saved cancellation checkpoint.
            document = json.loads(output.read_text())
            diagnostics = document["diagnostics"]
            results.append({
                "site": site, "mode": mode, "timed_out": timed_out,
                "reviews": len(document["reviews"]),
                "elapsed_seconds": diagnostics.get("elapsed_seconds"),
                "status": diagnostics["status"],
                "completeness_verified": diagnostics.get(
                    "completeness_verified",
                ),
                "collection_path": diagnostics.get("collection_path"),
                "error": diagnostics.get("error"),
                "output": str(output.resolve()),
            })
            report = {
                "created_at": datetime.now(UTC).isoformat(),
                "runtime": "Invisible Playwright", "results": results,
                "proxy_index": options.proxy_index if proxy else None,
                "max_reviews": options.max_reviews,
                "max_pages": options.max_pages,
                "note": "Sequential smoke runs, not a statistical benchmark.",
            }
            (output_dir / "benchmark.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(json.dumps(results[-1], ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sites", nargs="+", choices=URLS, default=list(URLS))
    parser.add_argument(
        "--modes", nargs="+", choices=("fast", "dom"), default=["fast"],
    )
    parser.add_argument("--max-reviews", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--timeout-ms", type=int, default=30_000)
    parser.add_argument("--run-timeout-seconds", type=int, default=180)
    parser.add_argument("--cookies-dir", default=".")
    parser.add_argument("--proxy-list")
    parser.add_argument("--proxy-index", type=int, default=1)
    parser.add_argument(
        "--output-dir", default="data/sites_benchmark_" +
        datetime.now(UTC).strftime("%Y%m%d_%H%M%S"),
    )
    options = parser.parse_args()
    for name in (
        "max_reviews", "max_pages", "timeout_ms", "run_timeout_seconds",
    ):
        if getattr(options, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    asyncio.run(benchmark(options))


if __name__ == "__main__":
    main()
