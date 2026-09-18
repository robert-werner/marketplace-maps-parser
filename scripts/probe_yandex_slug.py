"""Probe 5: why does the reviews URL 404 — the slug?

Compares the live page for three slugs of the same product id
(user's long slug, the canonical slug the site itself emitted in
probe 1, and a garbage slug), printing HTTP status markers, title
and card counts. Also walks the reviews LIST from the product card
page itself to see how the site navigates to reviews now.

Run: uv run python scripts/probe_yandex_slug.py --url <any-slug-url>
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

SNAPSHOT_JS = """
() => {
    const out = {
        title: document.title,
        cards: document.querySelectorAll('[data-auto="review-item"]').length,
        statusCode: null,
        pageName: null,
        links: [],
    };
    // The SSR state ships statusCode + page name in the first
    // <script id="state"> blob (or similar). Grab what we can.
    for (const s of document.querySelectorAll('script')) {
        const t = s.textContent || '';
        if (t.length < 100) continue;
        const m = t.match(/"statusCode":(\\d+)/);
        if (m && out.statusCode == null) out.statusCode = m[1];
        const p = t.match(/"page":"(market:[^"]+)"/);
        if (p && out.pageName == null) out.pageName = p[1];
    }
    // How does the site itself link to the reviews page?
    document.querySelectorAll('a[href*="reviews"], a[href*="/card/"]').forEach(
        (a) => {
            const href = a.getAttribute('href') || '';
            if (out.links.length < 10 && /reviews/.test(href)) {
                out.links.push(href);
            }
        });
    return out;
}
"""


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--cookies", default="src/yandex_cookies.json")
    args = parser.parse_args()

    from shared.url_parsers import extract_yandex_market_card_path
    from infrastructure.transports.browser_common import (
        import_invisible_playwright,
    )

    card_path = extract_yandex_market_card_path(args.url)
    product_id = card_path.rsplit("/", 1)[-1]
    base = "https://market.yandex.ru"

    browser_cls = import_invisible_playwright()
    async with browser_cls(proxy=None, seed=None, humanize=True) as browser:
        page = await browser.new_page()
        if args.cookies:
            import json
            cookies = json.loads(
                Path(args.cookies).read_text(encoding="utf-8"),
            )
            try:
                await page.context.add_cookies(cookies)
                print(f"cookies injected: {len(cookies)}")
            except Exception as exc:
                print("cookie inject failed:", exc)

        for slug in (
            "matochnoye-molochko-pchelinoye-kapsuly-kh2-nabor"
            "---altayskiy-zagotovitel",
            "baa-ultimatab-magnii-i-kaltsii-tabletki",
            "x",
        ):
            url = f"{base}/card/{slug}/{product_id}/reviews?page=1"
            try:
                await page.goto(url, timeout=60_000)
            except Exception as exc:
                print(f"{slug[:40]!r}: goto failed: {exc}")
                continue
            await page.wait_for_timeout(2_500)
            snap = await page.evaluate(SNAPSHOT_JS)
            print(f"slug={slug[:44]!r}")
            print(
                f"  status={snap['statusCode']} page={snap['pageName']}"
                f" cards={snap['cards']} title={snap['title'][:60]!r}"
            )
            if snap["links"]:
                print(f"  review links: {snap['links'][:4]}")

        # How does the CARD page link to reviews?
        card_url = f"{base}/card/baa-ultimatab-magnii-i-kaltsii-tabletki/{product_id}"
        try:
            await page.goto(card_url, timeout=60_000)
            await page.wait_for_timeout(2_500)
            snap = await page.evaluate(SNAPSHOT_JS)
            print(f"card page: status={snap['statusCode']} "
                  f"page={snap['pageName']} title={snap['title'][:50]!r}")
            print(f"  review links on card: {snap['links'][:6]}")
        except Exception as exc:
            print("card goto failed:", exc)
        await page.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
