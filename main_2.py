# main_2.py
#
# DEPRECATED — kept for historical reference.
#
# This was an exploratory probe of Ozon's entrypoint API using plain
# httpx. The behavior it explored (calling
# /api/entrypoint-api.bx/page/json/v2?url=…/reviews) is now implemented
# inside the page context via BrowserJsonTransport in
# src/infrastructure/transports/browser_json.py.
from __future__ import annotations

import asyncio
import sys

import httpx


async def main() -> None:
    print(
        "[deprecation] main_2.py is a historical probe; "
        "see BrowserJsonTransport for the production path.",
        file=sys.stderr,
    )
    endpoint = (
        "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2"
    )

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=30,
    ) as client:
        response = await client.get(
            endpoint,
            params={
                "url": (
                    "/product/"
                    "ip-telefon-yealink-sip-t30-voip-ofisnyy-680123890"
                    "/reviews"
                ),
            },
        )
        print(response.request)
        print(response.status_code)
        print(response.text[:1000])


if __name__ == "__main__":
    asyncio.run(main())
