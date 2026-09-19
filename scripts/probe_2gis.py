# scripts/probe_2gis.py
"""Live probe of a 2GIS org page: reviews API discovery.

Answers (for the upcoming TwoGisTransport):
1. Is ``/firm/<id>/tab/reviews`` a direct URL that renders reviews?
2. Which endpoints serve the review list (URL, params, response)?
3. How the list paginates (limit/offset? page? key required?).
4. Does the API answer plain curl_cffi too (no browser)?

Run::

    uv run python scripts/probe_2gis.py \
        --url https://2gis.ru/moscow/firm/70000001063192616
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

INTERESTING = ("review", "catalog.api", "/api/", "2gis")
DUMP_DIR = Path("debug_2gis")

PROBE_JS = """
() => {
    const out = {title: document.title, url: location.href,
                 reviewNodes: 0, sample: null, buttons: []};
    const nodes = document.querySelectorAll(
        '[class*="review"], [data-testid*="review"]');
    out.reviewNodes = nodes.length;
    if (nodes.length) {
        out.sample = nodes[0].outerHTML.slice(0, 1200);
    }
    const btns = [...document.querySelectorAll(
        'button, a, [role="button"], [role="tab"]'
    )].map((b) => (b.textContent || '').trim());
    out.buttons = btns.filter(
        (t) => t && /отзыв|ревью|review/i.test(t)
    ).slice(0, 8);
    return out;
}
"""

SCROLL_JS = """
() => {
    const containers = [...document.querySelectorAll('div, section')].filter((el) => {
        const st = getComputedStyle(el);
        return (st.overflowY === 'auto' || st.overflowY === 'scroll')
            && el.scrollHeight > el.clientHeight + 200;
    });
    if (!containers.length) { window.scrollTo(0, document.body.scrollHeight); return 'window'; }
    const t = containers.sort((a, b) => b.scrollHeight - a.scrollHeight)[0];
    t.scrollTop = t.scrollHeight;
    return 'pane';
}
"""


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--scrolls", type=int, default=6)
    args = parser.parse_args()

    from infrastructure.transports.browser_common import (
        import_invisible_playwright,
    )

    DUMP_DIR.mkdir(parents=True, exist_ok=True)

    firm_url = args.url.split("?")[0].rstrip("/")
    reviews_url = firm_url + "/tab/reviews"

    requests_log: list[dict] = []
    responses: list = []

    browser_cls = import_invisible_playwright()
    async with browser_cls(
        proxy=None, seed=None, humanize=True,
    ) as browser:
        page = await browser.new_page()

        def on_request(request) -> None:
            url = request.url
            if any(k in url for k in INTERESTING):
                requests_log.append(
                    {
                        "url": url,
                        "method": request.method,
                        "post_data": request.post_data,
                    },
                )

        def on_response(response) -> None:
            url = response.url
            if (
                any(k in url for k in INTERESTING)
                and "json" in response.headers.get(
                    "content-type", "",
                )
            ):
                responses.append(response)

        page.on("request", on_request)
        page.on("response", on_response)

        print(f"== goto {firm_url}")
        await page.goto(firm_url, timeout=90_000)
        await page.wait_for_timeout(4000)

        result = await page.evaluate(PROBE_JS)
        print(json.dumps(result, ensure_ascii=False, indent=1)[:1500])

        print("== goto reviews tab")
        await page.goto(
            reviews_url, timeout=90_000, referer=firm_url,
        )
        await page.wait_for_timeout(4000)

        for burst in range(args.scrolls):
            mode = await page.evaluate(SCROLL_JS)
            await page.wait_for_timeout(1800)
            print(
                f"  scroll {burst + 1} ({mode}): "
                f"requests={len(requests_log)}",
            )

        (DUMP_DIR / "captured_requests.json").write_text(
            json.dumps(requests_log, indent=1, ensure_ascii=False),
            encoding="utf-8",
        )

        print(
            f"== captured {len(requests_log)} requests / "
            f"{len(responses)} json responses",
        )
        seen = set()
        for response in responses:
            url = response.url
            short = url.split("?")[0]
            if short in seen:
                continue
            seen.add(short)
            try:
                body = await response.json()
            except Exception:
                continue
            preview = json.dumps(body, ensure_ascii=False)[:300]
            q = dict(parse_qsl(urlsplit(url).query))
            print(f"[{response.status}] {short}")
            print(f"   params: {list(q.items())[:8]}")
            print(f"   body: {preview}")

        (DUMP_DIR / "page.html").write_text(
            await page.content(), encoding="utf-8",
        )
        await page.close()

    print(f"done; dumps in {DUMP_DIR}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
