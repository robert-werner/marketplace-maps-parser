"""Probe 2: how to tell own-product cards from the cross-product feed,
and how to merge JSON-LD ratings into DOM cards.

For every review card dumps: root id, nickname, date, offer-info chip
link href, description prefix. Plus the LD review list.
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
    const out = {cards: [], ld: [], offerHrefs: {}};
    document.querySelectorAll('[data-auto="review-item"]').forEach(
        (root) => {
            const get = (sel) => {
                const el = root.querySelector(sel);
                return el ? (el.textContent || '').trim() : null;
            };
            const offer = root.querySelector(
                '[data-auto="ugc-element-offer-info"] a, '
                + '[data-auto="ugc-element-offer-info"] [href]');
            let offerHref = null;
            if (offer) offerHref = offer.getAttribute('href');
            out.cards.push({
                id: root.getAttribute('id'),
                nickname: get('[data-auto="nickname"]'),
                date: get('[data-auto="created-date"]'),
                description: (get('[data-auto="review-description"]')
                    || get('[data-auto="review-comment"]') || '')
                    .slice(0, 60),
                offerHref,
            });
            if (offerHref) {
                out.offerHrefs[offerHref] =
                    (out.offerHrefs[offerHref] || 0) + 1;
            }
        });
    for (const s of document.querySelectorAll(
            'script[type="application/ld+json"]')) {
        let data;
        try { data = JSON.parse(s.textContent); } catch (e) { continue; }
        const items = Array.isArray(data) ? data : [data];
        for (const item of items) {
            if (!item || item['@type'] !== 'Product') continue;
            for (const r of (item.review || [])) {
                out.ld.push({
                    author: r.author && r.author.name
                        ? r.author.name : null,
                    date: r.datePublished || null,
                    rating: r.reviewRating
                        ? r.reviewRating.ratingValue : null,
                    body: (r.reviewBody || '').slice(0, 60),
                });
            }
        }
    }
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
        await page.wait_for_timeout(2000)

        for page_no in range(1, args.pages + 1):
            url = f"{base}{card_path}/reviews?page={page_no}"
            await page.goto(url, timeout=90_000, referer=f"{base}{card_path}")
            await page.wait_for_timeout(2500)
            result = await page.evaluate(PROBE_JS)

            print(f"===== page={page_no}")
            print("offer hrefs histogram: "
                  + json.dumps(result["offerHrefs"], ensure_ascii=False))
            for card in result["cards"]:
                print(f"  card id={card['id']!r} nick={card['nickname']!r}"
                      f" date={card['date']!r} offer={card['offerHref']!r}"
                      f" desc={card['description']!r}")
            for item in result["ld"]:
                print(f"  ld   author={item['author']!r} date={item['date']!r}"
                      f" rating={item['rating']!r} body={item['body']!r}")
        await page.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
