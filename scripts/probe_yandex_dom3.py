"""Probe 3: hydration vs soft-block, and chip-href semantics.

Per reviews page: poll the DOM until the card set is stable (or a
time cap), then dump every card (id, nick, date, text, chip href) and
the JSON-LD review list. Answers:

1. Are empty pages a hydration race (cards appear if we wait) or a
   soft block (never appear)?
2. Does the ``ugc-element-offer-info`` chip href point at the
   reviewed product (own vs foreign discrimination by product id)?
3. Which cards match JSON-LD reviews by (author, date)?

Run: uv run python scripts/probe_yandex_dom3.py --url <product-url>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

SNAPSHOT_JS = """
() => {
    const out = {cards: [], ld: [], aggCount: null, aggRating: null,
                 readyState: document.readyState,
                 bodyLen: document.body ? document.body.innerHTML.length : 0,
                 skeletons: document.querySelectorAll(
                     '[class*="skeleton"], [class*="loader"]').length};
    document.querySelectorAll('[data-auto="review-item"]').forEach(
        (root) => {
            const get = (sel) => {
                const el = root.querySelector(sel);
                return el ? (el.textContent || '').trim() : null;
            };
            let chip = root.querySelector(
                '[data-auto="ugc-element-offer-info"]');
            let chipHref = null;
            if (chip) {
                const a = chip.querySelector('a[href]') ||
                    chip.closest('a[href]');
                if (a) chipHref = a.getAttribute('href');
            }
            out.cards.push({
                id: root.getAttribute('id'),
                nick: get('[data-auto="nickname"]'),
                date: get('[data-auto="created-date"]'),
                text: (get('[data-auto="review-description"]')
                    || get('[data-auto="review-comment"]') || ''
                ).slice(0, 70),
                chip: !!chip,
                chipHref,
            });
        });
    for (const s of document.querySelectorAll(
            'script[type="application/ld+json"]')) {
        let data;
        try { data = JSON.parse(s.textContent); } catch (e) { continue; }
        const items = Array.isArray(data) ? data : [data];
        for (const item of items) {
            if (!item || item['@type'] !== 'Product') continue;
            const agg = item.aggregateRating || {};
            if (agg.reviewCount != null) out.aggCount = agg.reviewCount;
            if (agg.ratingValue != null) out.aggRating = agg.ratingValue;
            for (const r of (item.review || [])) {
                out.ld.push({
                    author: r.author && r.author.name
                        ? r.author.name : null,
                    date: r.datePublished || null,
                    rating: r.reviewRating
                        ? r.reviewRating.ratingValue : null,
                    body: (r.reviewBody || '').slice(0, 70),
                });
            }
        }
    }
    return out;
}
"""


def _ids(snapshot: dict) -> set[str]:
    return {
        c["id"] or str(i)
        for i, c in enumerate(snapshot["cards"])
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--pages", type=int, default=2)
    parser.add_argument("--max-wait-s", type=float, default=20.0)
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
        await page.wait_for_timeout(3000)

        for page_no in range(1, args.pages + 1):
            url = f"{base}{card_path}/reviews?page={page_no}"
            await page.goto(
                url, timeout=90_000, referer=f"{base}{card_path}",
            )

            # Stability poll: read every 700 ms until two consecutive
            # snapshots agree on the card-id set (non-empty), or cap.
            stable = None
            prev_ids: set[str] | None = None
            rounds = 0
            deadline = asyncio.get_event_loop().time() + args.max_wait_s
            while asyncio.get_event_loop().time() < deadline:
                await page.wait_for_timeout(700)
                rounds += 1
                snap = await page.evaluate(SNAPSHOT_JS)
                ids = _ids(snap)
                if ids and ids == prev_ids:
                    stable = snap
                    break
                prev_ids = ids

            snap = stable or snap
            print(f"===== page={page_no} rounds={rounds} "
                  f"stable={stable is not None} "
                  f"readyState={snap['readyState']} "
                  f"bodyLen={snap['bodyLen']} "
                  f"skeletons={snap['skeletons']}")
            print(f"aggCount={snap['aggCount']} "
                  f"aggRating={snap['aggRating']} "
                  f"ld={len(snap['ld'])} cards={len(snap['cards'])}")
            for c in snap["cards"]:
                print(f"  card {c['id']} nick={c['nick']!r} "
                      f"date={c['date']!r} chip={c['chip']} "
                      f"chipHref={c['chipHref']!r} "
                      f"text={c['text']!r}")
            for r in snap["ld"]:
                print(f"  ld   {r['date']} rating={r['rating']} "
                      f"author={r['author']!r} body={r['body']!r}")
        await page.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
