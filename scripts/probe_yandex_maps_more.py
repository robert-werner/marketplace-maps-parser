# scripts/probe_yandex_maps_more.py
"""Why does the lazy loader stall at ~600 reviews?

Reproduces the stall (pane scrollTop jumps until no new
fetchReviews XHR), then inspects the «Ещё» control and tries
several click strategies, watching for new fetchReviews responses.

Run::

    uv run python scripts/probe_yandex_maps_more.py \
        --url https://yandex.ru/maps/org/<slug>/<id>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

FETCH_MARKER = "/maps/api/business/fetchReviews"

COUNT_CARDS_JS = """
() => document.querySelectorAll(
    '.business-review-view'
).length
"""

INSPECT_MORE_JS = """
() => {
    const el = document.querySelector(
        '.business-reviews-card-view__more');
    if (!el) return null;
    const rect = el.getBoundingClientRect();
    return {
        tag: el.tagName,
        cls: el.className,
        text: (el.textContent || '').trim().slice(0, 80),
        rect: {x: rect.x, y: rect.y, w: rect.width, h: rect.height},
        disabled: el.classList.contains('_disabled'),
        href: el.getAttribute('href'),
        visible: rect.width > 0 && rect.height > 0,
        inViewport: rect.top >= 0 && rect.bottom <= innerHeight,
        outerStart: el.outerHTML.slice(0, 400),
    };
}
"""

CLICK_JS = """
() => {
    const el = document.querySelector(
        '.business-reviews-card-view__more');
    if (!el) return 'absent';
    el.click();
    return 'clicked';
}
"""

SCROLL_PANE_JS = """
() => {
    const containers = [...document.querySelectorAll(
        'div, section'
    )].filter((el) => {
        const st = getComputedStyle(el);
        return (st.overflowY === 'auto'
                || st.overflowY === 'scroll')
            && el.scrollHeight > el.clientHeight + 200;
    });
    if (!containers.length) return false;
    const target = containers.sort(
        (a, b) => b.scrollHeight - a.scrollHeight
    )[0];
    target.scrollTop = target.scrollHeight;
    return true;
}
"""


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    args = parser.parse_args()

    from infrastructure.transports.browser_common import (
        import_invisible_playwright,
    )

    reviews_url = args.url.split("?")[0].rstrip("/")
    if not reviews_url.endswith("/reviews"):
        reviews_url += "/reviews/"

    browser_cls = import_invisible_playwright()
    async with browser_cls(
        proxy=None, seed=None, humanize=True,
    ) as browser:
        page = await browser.new_page()

        responses: list = []

        def on_response(response) -> None:
            if FETCH_MARKER in response.url:
                responses.append(response)

        page.on("response", on_response)

        await page.goto(reviews_url, timeout=90_000)
        await page.wait_for_timeout(3000)

        # Phase 1: scroll until the loader stalls (2 idle rounds).
        print("== phase 1: scroll until stall")
        idle = 0
        rounds = 0
        while idle < 2 and rounds < 80:
            await page.evaluate(SCROLL_PANE_JS)
            await page.wait_for_timeout(1800)
            rounds += 1
            if len(responses) > 0:
                idle = 0
            else:
                idle += 1
            if rounds % 5 == 0:
                cards = await page.evaluate(COUNT_CARDS_JS)
                print(
                    f"  round {rounds}: xhr={len(responses)} "
                    f"cards={cards} idle={idle}"
                )
        cards = await page.evaluate(COUNT_CARDS_JS)
        print(
            f"stalled after {rounds} rounds: "
            f"xhr={len(responses)} cards={cards}"
        )

        # Phase 2: inspect the «Ещё» control.
        print("== phase 2: inspect more-control")
        info = await page.evaluate(INSPECT_MORE_JS)
        print(json.dumps(info, ensure_ascii=False, indent=1))

        # Phase 3: try the three click strategies.
        print("== phase 3: click strategies")
        strategies: list[tuple[str, object]] = [
            ("js-click", lambda: page.evaluate(CLICK_JS)),
            (
                "locator-click",
                lambda: page.locator(
                    ".business-reviews-card-view__more",
                ).first.click(timeout=5000),
            ),
            (
                "scroll-then-locator-click",
                lambda: page.locator(
                    ".business-reviews-card-view__more",
                ).first.click(timeout=5000),
            ),
        ]
        for name, action in strategies:
            before = len(responses)
            if name == "scroll-then-locator-click":
                await page.evaluate(SCROLL_PANE_JS)
                await page.wait_for_timeout(500)
            try:
                result = await action()
                print(f"  {name}: {result!r}")
            except Exception as exc:
                print(f"  {name}: FAILED {str(exc)[:200]}")
            for _ in range(4):
                await page.wait_for_timeout(2000)
                if len(responses) > before:
                    break
            cards = await page.evaluate(COUNT_CARDS_JS)
            print(
                f"    -> xhr_delta={len(responses) - before} "
                f"cards={cards}"
            )

        info = await page.evaluate(INSPECT_MORE_JS)
        print("final more-control:", json.dumps(
            info, ensure_ascii=False,
        )[:400])
        await page.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
