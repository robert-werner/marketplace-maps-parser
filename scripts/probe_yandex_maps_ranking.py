# scripts/probe_yandex_maps_ranking.py
"""The default ranking caps the lazy window at ~600 reviews.

Measure what other streams exist and how much NEW ground each one
covers (union of reviewIds):

1. exhaust the default ranking by pane-scroll;
2. open the ranking dropdown (``.rating-ranking-view``) and dump
   its options (the popup's items, not the aspect chips);
3. per option: click, exhaust-scroll, count stream pages + reviews
   NEW to the global union.

Run::

    uv run python scripts/probe_yandex_maps_ranking.py \
        --url https://yandex.ru/maps/org/<slug>/<id>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

FETCH_MARKER = "/maps/api/business/fetchReviews"

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

POPUP_ITEMS_JS = """
() => [...document.querySelectorAll(
    '[role="menuitem"], [role="option"]'
)].map((el) => ({
    text: (el.textContent || '').trim(),
    cls: String(el.className).slice(0, 100),
}))
"""


class XhrLog:
    """Records fetchReviews requests AND response review ids."""

    def __init__(self) -> None:
        self.entries: list[dict] = []
        self.union: set[str] = set()

    def attach(self, page) -> None:
        def on_request(request) -> None:
            if FETCH_MARKER not in request.url:
                return
            qs = parse_qs(urlparse(request.url).query)
            self.entries.append(
                {
                    "ranking": qs.get("ranking", ["?"])[0],
                    "aspectId": qs.get("aspectId", [None])[0],
                    "page": qs.get("page", ["?"])[0],
                },
            )

        async def on_response(response) -> None:
            if FETCH_MARKER not in response.url:
                return
            try:
                payload = await response.json()
            except Exception:
                return
            data = (
                payload.get("data")
                if isinstance(payload, dict)
                else None
            ) or {}
            for card in data.get("reviews") or []:
                rid = card.get("reviewId")
                if rid:
                    self.union.add(rid)

        page.on("request", on_request)
        page.on("response", on_response)


async def exhaust(page, log: XhrLog) -> int:
    """Scroll until no new fetchReviews arrive; returns pages."""
    seen = len(log.entries)
    idle = 0
    while idle < 2:
        await page.evaluate(SCROLL_PANE_JS)
        await page.wait_for_timeout(1500)
        if len(log.entries) > seen:
            seen = len(log.entries)
            idle = 0
        else:
            idle += 1
    return len(log.entries)


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
        log = XhrLog()
        log.attach(page)

        await page.goto(reviews_url, timeout=90_000)
        await page.wait_for_timeout(3000)

        print("== exhaust default ranking")
        before_union = len(log.union)
        pages = await exhaust(page, log)
        print(
            f"default stream: {pages} XHR pages, "
            f"union={len(log.union)} (+{len(log.union) - before_union})"
        )
        default_ranking = (
            log.entries[0]["ranking"] if log.entries else "?"
        )
        print(f"default ranking param: {default_ranking!r}")

        print("== open ranking dropdown")
        try:
            await page.locator(
                ".rating-ranking-view",
            ).first.click(timeout=5000)
        except Exception as exc:
            print(f"dropdown click failed: {str(exc)[:200]}")
            await page.close()
            return 1
        await page.wait_for_timeout(1500)
        items = await page.evaluate(POPUP_ITEMS_JS)
        print(
            "popup items:",
            json.dumps(items, ensure_ascii=False, indent=1),
        )
        print(f"url after dropdown: {page.url[:120]}")

        for item in items:
            label = item["text"]
            print(f"== option {label!r}")
            union_before = len(log.union)
            entries_before = len(log.entries)
            try:
                await page.get_by_role(
                    "menuitem", name=label,
                ).first.click(timeout=4000)
            except Exception:
                try:
                    await page.get_by_text(
                        label, exact=True,
                    ).first.click(timeout=4000)
                except Exception as exc:
                    print(f"  click failed: {str(exc)[:120]}")
                    continue
            await page.wait_for_timeout(2000)
            await exhaust(page, log)
            stream = log.entries[entries_before:]
            rankings = sorted({
                e["ranking"] for e in stream
            })
            print(
                f"  stream: {len(stream)} pages, rankings="
                f"{rankings}, NEW reviews="
                f"{len(log.union) - union_before}, "
                f"union={len(log.union)}"
            )

        print("== timeline tail:")
        for e in log.entries[-15:]:
            print("  ", e)
        await page.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
