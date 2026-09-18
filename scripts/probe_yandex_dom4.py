"""Probe 4: capture the soft-block shell HTML and the anatomy of a
rating-only card (stars markup, chip innerHTML).

- Navigates ?page=1..N with a short stability poll.
- A page with 0 cards + 0 LD + no aggregate is a suspected SOFT
  BLOCK: its full HTML is saved to debug_shell_<n>.html for marker
  mining.
- For up to 3 cards per healthy page dumps: chip innerHTML, the
  star-rating markup (any svg/aria within the card header), and
  whether a hidden input/data attribute carries the grade.

Run: uv run python scripts/probe_yandex_dom4.py --url <product-url>
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

SNAPSHOT_JS = """
() => {
    const out = {cards: 0, ld: 0, aggCount: null, bodyLen: 0,
                 readyState: document.readyState, html: null,
                 samples: []};
    out.bodyLen = document.body
        ? document.body.innerHTML.length : 0;
    const roots = document.querySelectorAll(
        '[data-auto="review-item"]');
    out.cards = roots.length;

    let ldCount = 0;
    for (const s of document.querySelectorAll(
            'script[type="application/ld+json"]')) {
        let data;
        try { data = JSON.parse(s.textContent); } catch (e) { continue; }
        const items = Array.isArray(data) ? data : [data];
        for (const item of items) {
            if (!item || item['@type'] !== 'Product') continue;
            const agg = item.aggregateRating || {};
            if (agg.reviewCount != null) out.aggCount = agg.reviewCount;
            ldCount += (item.review || []).length;
        }
    }
    out.ld = ldCount;

    if (out.cards === 0 && ldCount === 0 && out.aggCount == null) {
        out.html = document.documentElement.outerHTML;
        return out;
    }

    roots.forEach((root, i) => {
        if (i >= 3) return;
        const chip = root.querySelector(
            '[data-auto="ugc-element-offer-info"]');
        // Star rating: any element with aria-label containing
        // 'Оценка', or a svg group inside the card's first rows.
        let aria = null;
        for (const el of root.querySelectorAll('[aria-label]')) {
            const label = el.getAttribute('aria-label') || '';
            if (/оценк|звезд|star/i.test(label)) {
                aria = label; break;
            }
        }
        // data attributes that look grade-ish.
        const dataAttrs = {};
        root.querySelectorAll('*').forEach((el) => {
            for (const attr of el.attributes) {
                if (/grade|score|rating|star/i.test(attr.name)
                        && attr.value) {
                    const key = el.tagName + ':' + attr.name;
                    if (!(key in dataAttrs)) {
                        dataAttrs[key] = attr.value.slice(0, 40);
                    }
                }
            }
        });
        // Inner svg count in the rating area (5 stars = 5 svgs).
        const svgCount = root.querySelectorAll(
            '[data-widget*="rating"] svg, '
            + '[class*="rating"] svg').length;
        out.samples.push({
            id: root.getAttribute('id'),
            chipHTML: chip ? chip.innerHTML.slice(0, 400) : null,
            aria,
            dataAttrs,
            svgCount,
            headHTML: root.innerHTML.slice(0, 900),
        });
    });
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
        await page.wait_for_timeout(3000)

        for page_no in range(1, args.pages + 1):
            url = f"{base}{card_path}/reviews?page={page_no}"

            snap = None
            prev_ids = None
            # A captcha shell often alternates with healthy pages —
            # re-navigate (up to 6 tries) until a non-shell loads.
            for _nav in range(6):
                await page.goto(
                    url, timeout=90_000,
                    referer=f"{base}{card_path}",
                )
                for _round in range(14):
                    await page.wait_for_timeout(700)
                    snap = await page.evaluate(SNAPSHOT_JS)
                    ids = (snap["cards"], snap["ld"])
                    if ids == prev_ids and snap["cards"]:
                        break
                    prev_ids = ids
                if snap["html"] is None:
                    break
                shell_saved = False

            print(f"===== page={page_no} ready={snap['readyState']} "
                  f"bodyLen={snap['bodyLen']} cards={snap['cards']} "
                  f"ld={snap['ld']} agg={snap['aggCount']}")

            if snap["html"] is not None:
                if not shell_saved:
                    shell = Path(f"debug_shell_{page_no}.html")
                    shell.write_text(snap["html"], encoding="utf-8")
                    shell_saved = True
                    print(f"  SOFT BLOCK SHELL saved: {shell} "
                          f"({len(snap['html'])} chars)")
                continue

            for sample in snap["samples"]:
                print(f"  -- card {sample['id']}")
                print(f"     aria={sample['aria']!r} "
                      f"svgCount={sample['svgCount']} "
                      f"dataAttrs={sample['dataAttrs']}")
                chip = sample["chipHTML"]
                print(f"     chip={chip!r}")
        await page.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
