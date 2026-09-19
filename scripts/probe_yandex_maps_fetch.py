# scripts/probe_yandex_maps_fetch.py
"""Call fetchReviews from INSIDE the Maps page (page.evaluate).

Isolates the 400 puzzle: the same minimal param set that fails over
curl_cffi — does it work from the page's own JS context (real
browser headers + cookies)?

Run::

    uv run python scripts/probe_yandex_maps_fetch.py \
        --url https://yandex.ru/maps/org/<slug>/<id>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

FETCH_JS = """
async (token, paramsMode) => {
    const q = new URLSearchParams({
        ajax: '1',
        businessId: '1120018525',
        csrfToken: token,
        locale: 'ru_RU',
        page: '1',
        pageSize: '50',
        ranking: 'by_relevance_org',
    });
    const resp = await fetch(
        '/maps/api/business/fetchReviews?' + q.toString(),
        {headers: {'Accept': 'application/json, text/plain, */*'}},
    );
    const text = await resp.text();
    return {status: resp.status, head: text.slice(0, 300)};
}
"""

EXTRACT_TOKEN_JS = """
() => {
    const html = document.documentElement.innerHTML;
    const m = html.match(/"csrfToken":"([^"]+)"/);
    return m ? m[1] : null;
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
        await page.goto(reviews_url, timeout=90_000)
        await page.wait_for_timeout(3000)

        token = await page.evaluate(EXTRACT_TOKEN_JS)
        print(f"token: {token!r}")

        for label, tok in (
            ("full", token),
            ("hex-only", token.split(":")[0] if token else None),
        ):
            if not tok:
                continue
            result = await page.evaluate(FETCH_JS, tok)
            print(f"{label}: status={result['status']}")
            print(f"  head={result['head']!r}")
            if result["status"] == 200:
                try:
                    payload = json.loads(result["head"])
                    if "data" in payload:
                        d = payload["data"]
                        print(
                            f"  REVIEWS: "
                            f"{len(d.get('reviews') or [])}",
                        )
                except json.JSONDecodeError:
                    pass

        await page.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
