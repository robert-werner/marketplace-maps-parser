# src/infrastructure/transports/browser_json.py
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from invisible_playwright.async_api import InvisiblePlaywright


class BrowserJsonTransport:
    """Получение JSON Ozon внутри одного browser context."""

    def __init__(
        self,
        *,
        timeout_ms: int = 90_000,
        settle_ms: int = 2_000,
        debug_dir: str = "debug_ozon",
        proxy: dict[str, str] | None = None,
        seed: int | None = None,
        pin: dict[str, Any] | None = None,
        humanize: bool = True,
    ) -> None:
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self.debug_dir = Path(debug_dir)
        self.proxy = proxy
        self.seed = seed
        self.pin = pin
        self.humanize = humanize

    async def iter_ozon_reviews_json(
        self,
        product_url: str,
        product_path: str,
        *,
        start_page: int = 1,
        max_pages: int | None = None,
    ) -> AsyncIterator[tuple[int, dict[str, Any]]]:
        """Последовательно получает страницы отзывов Ozon.

        Браузер и страница создаются один раз.
        """
        self.debug_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        async with InvisiblePlaywright(
            proxy=self.proxy,
            seed=self.seed,
            pin=self.pin,
            humanize=self.humanize,
        ) as browser:
            page = await browser.new_page()

            seen_review_ids: set[str] = set()
            page_number = start_page
            processed_pages = 0

            while True:
                if (
                    max_pages is not None
                    and processed_pages >= max_pages
                ):
                    return

                reviews_url = self._build_reviews_url(
                    product_path=product_path,
                    page_number=page_number,
                )

                await page.goto(
                    reviews_url,
                    wait_until="domcontentloaded",
                    timeout=self.timeout_ms,
                )

                if self.settle_ms > 0:
                    await page.wait_for_timeout(
                        self.settle_ms,
                    )

                internal_path = self._build_internal_path(
                    product_path=product_path,
                    page_number=page_number,
                )

                payload = await self._fetch_json_inside_page(
                    page=page,
                    internal_path=internal_path,
                    referer=reviews_url,
                )

                await self._save_debug(
                    page=page,
                    payload=payload,
                    page_number=page_number,
                )

                current_review_ids = (
                    self.extract_real_review_ids(payload)
                )

                new_review_ids = (
                    set(current_review_ids)
                    - seen_review_ids
                )

                seen_review_ids.update(current_review_ids)

                processed_pages += 1

                yield page_number, payload

                # Останавливаемся только если страница действительно
                # не содержит отзывов или целиком повторяет предыдущую.
                if not current_review_ids:
                    return

                if not new_review_ids:
                    return

                page_number += 1

    async def get_ozon_reviews_json(
        self,
        product_url: str,
        product_path: str,
        *,
        page_number: int = 1,
    ) -> dict[str, Any]:
        """Получает одну страницу отзывов."""
        async for current_page, payload in (
            self.iter_ozon_reviews_json(
                product_url=product_url,
                product_path=product_path,
                start_page=page_number,
                max_pages=1,
            )
        ):
            if current_page == page_number:
                return payload

        raise RuntimeError(
            f"Не удалось получить страницу отзывов {page_number}"
        )

    async def _fetch_json_inside_page(
        self,
        *,
        page,
        internal_path: str,
        referer: str,
    ) -> dict[str, Any]:
        endpoint = (
            "https://www.ozon.ru"
            "/api/entrypoint-api.bx/page/json/v2"
        )

        result = await page.evaluate(
            """
            async ({ endpoint, internalPath, referer }) => {
                const url = new URL(endpoint);
                url.searchParams.set("url", internalPath);

                const response = await fetch(
                    url.toString(),
                    {
                        method: "GET",
                        credentials: "include",
                        headers: {
                            "Accept": "application/json",
                            "Referer": referer,
                            "X-Requested-With":
                                "XMLHttpRequest"
                        }
                    }
                );

                return {
                    status: response.status,
                    url: response.url,
                    contentType:
                        response.headers.get("content-type") || "",
                    body: await response.text()
                };
            }
            """,
            {
                "endpoint": endpoint,
                "internalPath": internal_path,
                "referer": referer,
            },
        )

        status = result["status"]
        response_url = result["url"]
        content_type = result["contentType"].lower()
        body = result["body"]

        if status < 200 or status >= 300:
            raise RuntimeError(
                "Ozon browser fetch завершился ошибкой: "
                f"HTTP {status}; "
                f"url={response_url}; "
                f"body={body[:1000]}"
            )

        if "json" not in content_type:
            raise RuntimeError(
                "Ozon browser fetch вернул не JSON: "
                f"content-type={content_type}; "
                f"body={body[:500]}"
            )

        payload = json.loads(body)

        if not isinstance(payload, dict):
            raise RuntimeError(
                "Корень JSON Ozon не является dict"
            )

        return payload

    async def _save_debug(
        self,
        *,
        page,
        payload: dict[str, Any],
        page_number: int,
    ) -> None:
        page_dir = self.debug_dir / f"page_{page_number}"
        page_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        (page_dir / "response.json").write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        (page_dir / "page.html").write_text(
            await page.content(),
            encoding="utf-8",
        )

        await page.screenshot(
            path=str(page_dir / "page.png"),
            full_page=True,
        )

    @staticmethod
    def _build_reviews_url(
        *,
        product_path: str,
        page_number: int,
    ) -> str:
        query = urlencode(
            {"page": page_number}
        )

        return (
            f"https://www.ozon.ru"
            f"{product_path}/reviews/?{query}"
        )

    @staticmethod
    def _build_internal_path(
        *,
        product_path: str,
        page_number: int,
    ) -> str:
        query = urlencode(
            {"page": page_number}
        )

        return (
            f"{product_path}/reviews"
            f"?{query}"
        )

    @classmethod
    def extract_real_review_ids(
        cls,
        payload: Any,
    ) -> list[str]:
        """Извлекает UUID только из объектов, похожих на отзыв."""
        result: list[str] = []

        for node in cls.walk_json(payload):
            if not isinstance(node, dict):
                continue

            if not cls.looks_like_review(node):
                continue

            review_id = (
                node.get("reviewId")
                or node.get("review_id")
                or node.get("reviewUuid")
                or node.get("review_uuid")
                or node.get("uuid")
            )

            if review_id is not None:
                result.append(str(review_id))

        return list(dict.fromkeys(result))

    @staticmethod
    def looks_like_review(
        node: dict[str, Any],
    ) -> bool:
        keys = {
            str(key).lower()
            for key in node
        }

        has_id = bool(
            keys
            & {
                "reviewid",
                "review_id",
                "reviewuuid",
                "review_uuid",
            }
        )

        has_review_text = bool(
            keys
            & {
                "reviewtext",
                "review_text",
                "review",
                "comment",
                "advantages",
                "disadvantages",
            }
        )

        has_rating = bool(
            keys
            & {
                "rating",
                "score",
                "stars",
                "productrating",
                "product_rating",
            }
        )

        has_date = bool(
            keys
            & {
                "createdat",
                "created_at",
                "publishedat",
                "published_at",
                "date",
            }
        )

        return (
            has_id
            and (
                has_review_text
                or has_rating
                or has_date
            )
        )

    @staticmethod
    def walk_json(value: Any):
        yield value

        if isinstance(value, dict):
            for child in value.values():
                yield from BrowserJsonTransport.walk_json(
                    child
                )
            return

        if isinstance(value, list):
            for child in value:
                yield from BrowserJsonTransport.walk_json(
                    child
                )
            return

        if not isinstance(value, str):
            return

        text = value.strip()

        if not text or text[0] not in "[{":
            return

        try:
            decoded = json.loads(text)
        except (TypeError, ValueError):
            return

        yield from BrowserJsonTransport.walk_json(
            decoded
        )