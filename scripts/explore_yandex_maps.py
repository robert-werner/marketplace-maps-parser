# scripts/explore_yandex_maps.py
"""Live probe of a Yandex.Maps organization reviews page.

Answers the four questions the upcoming YandexMapsTransport depends
on (mirrors what probe_yandex_dom.py did for Yandex.Market):

1. Is ``/org/<slug>/<id>/reviews/`` a direct URL that renders
   reviews, or only a tab inside the org page?
2. Which endpoints serve the review list (URL, method, POST body,
   response JSON shape)?
3. How the list paginates: scroll append / «Показать ещё» button /
   an API ``page`` param.
4. What stable DOM markers the review cards carry (classes, data-*).

Run::

    uv run python scripts/explore_yandex_maps.py \
        --url https://yandex.ru/maps/org/<slug>/<id>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# URL substrings worth capturing: the Maps API prefix plus the
# review-ish keywords. Kept broad — the endpoint names are exactly
# what we do not know yet.
INTERESTING = ("maps/api", "review", "ugc", "rating", "comment")

DUMP_DIR = Path("debug_yandex_maps")

# Candidate card selectors, most specific first. NOTE: bare
# [class*="review"] also matches <body> (experiment class names
# contain "review") — body/html are excluded in JS.
CARD_SELECTORS = [
    "[class*='business-reviews-card']",
    "[class*='review-view']",
    "[class*='review-card']",
    "[class*='review']",
]

PROBE_JS = """
() => {
    const out = {title: document.title, url: location.href,
                 bodyLen: 0, cards: {}, sample: null,
                 showMoreButtons: [], scrollables: [],
                 ratingFilterTabs: []};

    out.bodyLen = document.body
        ? document.body.innerHTML.length : 0;

    const cardSelectors = %SELECTORS%;
    for (const sel of cardSelectors) {
        const nodes = [...document.querySelectorAll(sel)]
            .filter((el) => !['BODY', 'HTML'].includes(el.tagName)
                && el.closest('body') !== null
                && el.tagName !== 'BODY');
        out.cards[sel] = nodes.length;
        if (!out.sample && nodes.length
                && nodes[0].outerHTML.length > 500) {
            out.sample = {
                selector: sel,
                html: nodes[0].outerHTML.slice(0, 4000),
            };
        }
    }

    const btns = [...document.querySelectorAll(
        'button, [role="button"]'
    )].map((b) => (b.textContent || '').trim());
    out.showMoreButtons = btns.filter(
        (t) => t && /показать|ещё|еще/i.test(t)
    ).slice(0, 6);

    document.querySelectorAll(
        '[role="tab"], [class*="rating-filter"]'
    ).forEach((el) => {
        const t = (el.textContent || '').trim();
        if (t && /звезд|star|все/i.test(t)
                && out.ratingFilterTabs.length < 6) {
            out.ratingFilterTabs.push(t.slice(0, 40));
        }
    });

    document.querySelectorAll('div, section').forEach((el) => {
        const st = getComputedStyle(el);
        if (st.overflowY === 'auto' || st.overflowY === 'scroll') {
            if (el.scrollHeight > el.clientHeight + 200
                    && out.scrollables.length < 6) {
                out.scrollables.push({
                    cls: String(el.className).slice(0, 120),
                    scrollHeight: el.scrollHeight,
                    clientHeight: el.clientHeight,
                });
            }
        }
    });

    return out;
}
""".replace("%SELECTORS%", json.dumps(CARD_SELECTORS))

SCROLL_JS = """
() => {
    const containers = [...document.querySelectorAll(
        'div, section'
    )].filter((el) => {
        const st = getComputedStyle(el);
        return (st.overflowY === 'auto'
                || st.overflowY === 'scroll')
            && el.scrollHeight > el.clientHeight + 200;
    });
    if (!containers.length) return null;
    const target = containers.sort(
        (a, b) => b.scrollHeight - a.scrollHeight
    )[0];
    const before = target.scrollTop;
    target.scrollTop = target.scrollHeight;
    return {
        cls: String(target.className).slice(0, 120),
        before,
        after: target.scrollTop,
        scrollHeight: target.scrollHeight,
    };
}
"""


async def _dump_responses(
    responses: list,
    dump_dir: Path,
) -> None:
    """Read bodies of captured responses while the page is alive."""
    for idx, response in enumerate(responses):
        url = response.url
        try:
            body = await response.body()
        except Exception as exc:
            print(f"  [body error] {url[:110]} — {exc}")
            continue

        content_type = response.headers.get("content-type", "")
        name = f"resp_{idx:02d}_{response.status}"
        name += ".json" if "json" in content_type else ".txt"
        path = dump_dir / name
        try:
            path.write_bytes(body)
        except OSError as exc:
            print(f"  [write error] {name}: {exc}")
            continue

        preview = body[:300].decode("utf-8", "replace")
        keys = ""
        if "json" in content_type:
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    keys = f" keys={list(parsed.keys())[:10]}"
            except json.JSONDecodeError:
                pass
        print(
            f"  [{response.status}] {url[:110]}{keys}\n"
            f"    -> {name} ({len(body)} B) {preview[:200]!r}"
        )


def _reviews_url(org_url: str) -> str:
    base = org_url.split("?")[0].rstrip("/")
    if not base.endswith("/reviews"):
        base += "/reviews/"
    return base


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument(
        "--cookies",
        default=None,
        help="Optional cookies JSON for page.context.add_cookies.",
    )
    parser.add_argument(
        "--scrolls",
        type=int,
        default=8,
        help="scrollTop jumps on the reviews pane (default 8).",
    )
    args = parser.parse_args()

    from infrastructure.transports.browser_common import (
        import_invisible_playwright,
    )

    DUMP_DIR.mkdir(parents=True, exist_ok=True)

    browser_cls = import_invisible_playwright()
    reviews_url = _reviews_url(args.url)

    async with browser_cls(
        proxy=None, seed=None, humanize=True,
    ) as browser:
        page = await browser.new_page()

        if args.cookies:
            from infrastructure.transports.cookie_loader import (
                load_cookies_file,
            )
            await page.context.add_cookies(
                load_cookies_file(args.cookies),
            )
            print(f"cookies injected from {args.cookies}")

        captured_responses: list = []
        captured_requests: list[dict] = []

        def on_response(response) -> None:
            if any(k in response.url for k in INTERESTING):
                captured_responses.append(response)

        def on_request(request) -> None:
            url = request.url
            if any(k in url for k in INTERESTING):
                captured_requests.append(
                    {
                        "url": url,
                        "method": request.method,
                        "post_data": request.post_data,
                    },
                )

        page.on("response", on_response)
        page.on("request", on_request)

        # Warmup: land on the org card first, then open /reviews/
        # with the card as referer (same anti-bot pattern as the
        # Yandex.Market transport).
        print(f"== warmup goto {args.url}")
        await page.goto(args.url, timeout=90_000)
        await page.wait_for_timeout(3000)

        print(f"== goto {reviews_url}")
        await page.goto(
            reviews_url, timeout=90_000, referer=args.url,
        )
        await page.wait_for_timeout(4000)

        result = await page.evaluate(PROBE_JS)
        print(json.dumps(result, ensure_ascii=False, indent=1)[:4000])
        if result.get("sample"):
            sample_path = DUMP_DIR / "sample_card.html"
            sample_path.write_text(
                result["sample"]["html"], encoding="utf-8",
            )
            print(
                f"sample card ({result['sample']['selector']})"
                f" -> {sample_path}"
            )

        html = await page.content()
        (DUMP_DIR / "reviews_page.html").write_text(
            html, encoding="utf-8",
        )

        base_len = len(captured_responses)
        print(
            f"== {args.scrolls} scrollTop jumps on the pane",
        )
        for burst in range(args.scrolls):
            scroll_info = await page.evaluate(SCROLL_JS)
            await page.wait_for_timeout(1800)
            result = await page.evaluate(PROBE_JS)
            cards_now = {
                sel: n
                for sel, n in result["cards"].items()
                if n
            }
            new_responses = len(captured_responses) - base_len
            base_len = len(captured_responses)
            print(
                f"  jump {burst + 1}: {scroll_info} "
                f"cards={cards_now} new_responses={new_responses} "
                f"btns={result['showMoreButtons'][:3]}"
            )

        (DUMP_DIR / "captured_requests.json").write_text(
            json.dumps(captured_requests, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (DUMP_DIR / "page_after_scroll.html").write_text(
            await page.content(), encoding="utf-8",
        )

        print(
            f"== captured {len(captured_requests)} requests / "
            f"{len(captured_responses)} responses",
        )
        await _dump_responses(captured_responses, DUMP_DIR)

        await page.close()

    print(f"done; dumps in {DUMP_DIR}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
