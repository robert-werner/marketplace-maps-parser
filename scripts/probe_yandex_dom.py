"""One-off live probe of the Yandex.Market reviews page DOM.

Answers three questions the integration depends on:
1. Do the transport's ``data-auto`` selectors match the live DOM?
2. Does ``?page=N`` paginate, or is it a «Показать ещё» lazy-append?
3. What does the JSON-LD block carry (per-review ratings? count?)

Run: uv run python scripts/probe_yandex_dom.py --url <product-url>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

PROBE_JS = """
() => {
    const out = {dataAutoAttrs: {}, reviewItems: 0, sampleRoot: null,
                 ldBlocks: 0, ldReviews: 0, ldSample: null,
                 aggCount: null, aggRating: null,
                 nextPageLinks: [], showMore: null, bodyLen: 0,
                 title: document.title};

    // 1. What data-auto attributes exist at all?
    document.querySelectorAll('[data-auto]').forEach((el) => {
        const v = el.getAttribute('data-auto');
        out.dataAutoAttrs[v] = (out.dataAutoAttrs[v] || 0) + 1;
    });

    // 2. Review cards by the transport's selector + any id attr.
    const roots = document.querySelectorAll('[data-auto="review-item"]');
    out.reviewItems = roots.length;
    if (roots.length) {
        const r = roots[0];
        out.sampleRoot = {
            id: r.getAttribute('id'),
            outerStart: r.outerHTML.slice(0, 1500),
        };
    }

    // 3. JSON-LD blocks.
    for (const s of document.querySelectorAll(
            'script[type="application/ld+json"]')) {
        out.ldBlocks += 1;
        let data;
        try { data = JSON.parse(s.textContent); } catch (e) { continue; }
        const items = Array.isArray(data) ? data : [data];
        for (const item of items) {
            if (item && item['@type'] === 'Product') {
                const agg = item.aggregateRating || {};
                out.aggCount = agg.reviewCount ?? out.aggCount;
                out.aggRating = agg.ratingValue ?? out.aggRating;
                const revs = item.review || [];
                out.ldReviews += revs.length;
                if (revs.length && !out.ldSample) {
                    out.ldSample = revs[0];
                }
            }
        }
    }

    // 4. Pagination surface.
    document.querySelectorAll(
        'a[href*="page="]'
    ).forEach((a) => {
        const h = a.getAttribute('href') || '';
        if (out.nextPageLinks.length < 6 && /page=/.test(h)) {
            out.nextPageLinks.push(h);
        }
    });
    for (const sel of ['[data-auto="showMore"]', '[data-auto="show-more"]']) {
        const el = document.querySelector(sel);
        if (el) { out.showMore = sel; break; }
    }
    const btns = [...document.querySelectorAll(
        'button, [role="button"]'
    )].map((b) => (b.textContent || '').trim());
    out.pokazatButtons = btns.filter(
        (t) => t && /показать|ещё|еще/i.test(t)
    ).slice(0, 5);

    out.bodyLen = document.body ? document.body.innerHTML.length : 0;
    return out;
}
"""


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--pages", type=int, default=3)
    args = parser.parse_args()

    from shared.url_parsers import extract_yandex_market_card_path

    from infrastructure.transports.browser_common import (
        import_invisible_playwright,
    )

    card_path = extract_yandex_market_card_path(args.url)
    browser_cls = import_invisible_playwright()

    async with browser_cls(proxy=None, seed=None, humanize=True) as browser:
        page = await browser.new_page()
        base = "https://market.yandex.ru"

        await page.goto(f"{base}{card_path}", timeout=90_000)
        await page.wait_for_timeout(2500)

        for page_no in range(1, args.pages + 1):
            url = f"{base}{card_path}/reviews?page={page_no}"
            await page.goto(url, timeout=90_000, referer=f"{base}{card_path}")
            await page.wait_for_timeout(2500)
            result = await page.evaluate(PROBE_JS)
            print(f"===== page={page_no} url={url}")
            print(f"title: {result['title']!r}")
            print(f"body length: {result['bodyLen']}")
            print(f"review-item cards: {result['reviewItems']}")
            print(
                "data-auto attrs (top): "
                + json.dumps(
                    dict(
                        sorted(
                            result["dataAutoAttrs"].items(),
                            key=lambda kv: -kv[1],
                        )[:15],
                    ),
                    ensure_ascii=False,
                )
            )
            print(f"ld blocks: {result['ldBlocks']}, ld reviews: "
                  f"{result['ldReviews']}")
            print(f"agg count: {result['aggCount']}, agg rating: "
                  f"{result['aggRating']}")
            if result["ldSample"]:
                print("ld sample: "
                      + json.dumps(
                          result["ldSample"], ensure_ascii=False,
                      )[:400])
            if result["sampleRoot"]:
                print(f"root id: {result['sampleRoot']['id']!r}")
                print("root html start: "
                      + result["sampleRoot"]["outerStart"][:600])
            print(f"page links: {result['nextPageLinks'][:4]}")
            print(f"showMore selector: {result['showMore']}")
            print(f"показать-кнопки: {result['pokazatButtons']}")
        await page.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
