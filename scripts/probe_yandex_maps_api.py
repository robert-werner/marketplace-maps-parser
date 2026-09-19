# scripts/probe_yandex_maps_api.py
"""Can fetchReviews be called over plain HTTP (no browser)?

Flow under test:
1. GET the org /reviews/ page with curl_cffi (chrome TLS
   impersonation) — is it a healthy SSR page or a captcha shell?
2. Find the csrfToken (cookie jar? HTML?).
3. GET /maps/api/business/fetchReviews with the token + cookies.
4. Paginate page=1..N and check the JSON payloads.

Run::

    uv run python scripts/probe_yandex_maps_api.py \
        --url https://yandex.ru/maps/org/<slug>/<id>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

API = "https://yandex.ru/maps/api/business/fetchReviews"

CAPTCHA_MARKERS = (
    "Вы не робот?",
    "Подтвердите, что запросы отправляли вы",
    "smartcaptcha",
    "showcaptcha",
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
        "--proxy", default=None, help="Optional proxy URL.",
    )
    args = parser.parse_args()

    from curl_cffi import requests as curl_requests

    reviews_url = _reviews_url(args.url)
    business_id = reviews_url.rstrip("/").split("/")[-2]

    jar_dump: dict[str, str] = {}

    async with curl_requests.AsyncSession(
        impersonate="chrome120",
        timeout=30.0,
        proxy=args.proxy,
    ) as session:
        resp = await session.get(reviews_url)
        body = resp.text
        print(
            f"page: status={resp.status_code} len={len(body)} "
            f"url={resp.url[:90]}"
        )
        markers = [m for m in CAPTCHA_MARKERS if m in body]
        print(f"captcha markers: {markers or 'none'}")
        print(
            "has SSR reviews: "
            f"{'business-reviews-card-view' in body}"
        )

        for cookie in session.cookies.jar:
            jar_dump[cookie.name] = cookie.value[:60]
        print(f"cookies: {list(jar_dump)}")

        # csrfToken candidates: cookie, or embedded in the HTML
        # state blob.
        token = jar_dump.get("csrftoken")
        token_src = "cookie:csrftoken"
        if not token:
            match = re.search(
                r'"csrfToken":"([^"]+)"', body,
            )
            if match:
                token = match.group(1)
                token_src = "html:state-blob"
        print(f"csrfToken via {token_src}: {bool(token)}")
        if not token:
            print("no token — cannot test the API; abort")
            return 1

        for page in (1, 2, 3):
            params = {
                "ajax": "1",
                "businessId": business_id,
                "csrfToken": token,
                "locale": "ru_RU",
                "page": str(page),
                "pageSize": "50",
                "ranking": "by_relevance_org",
            }
            api_resp = await session.get(
                f"{API}?{urlencode(params)}",
                headers={"Referer": reviews_url},
            )
            print(
                f"page={page}: status={api_resp.status_code} "
                f"len={len(api_resp.text)}"
            )
            try:
                payload = api_resp.json()
            except ValueError as exc:
                print(f"  not JSON: {exc}; head={api_resp.text[:200]!r}")
                return 1
            data = payload.get("data") or {}
            reviews = data.get("reviews") or []
            meta = data.get("params") or {}
            print(
                f"  reviews={len(reviews)} meta={json.dumps(meta)}"
            )
            if page == 1 and reviews:
                sample = reviews[0]
                print(
                    "  sample: "
                    + json.dumps(sample, ensure_ascii=False)[:400]
                )
            if not reviews:
                break

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
