# src/infrastructure/transports/browser_dom.py
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from typing_extensions import AsyncIterator


def _import_invisible_playwright():
    """Lazy import; see transports/browser_json.py for rationale.
    Wrapped with GPU-safe software-rendering prefs (see
    transports/gpu_safety.py)."""
    from invisible_playwright.async_api import InvisiblePlaywright

    from infrastructure.transports.gpu_safety import make_gpu_safe
    return make_gpu_safe(InvisiblePlaywright)


class BrowserDomTransport:
    def __init__(
        self,
        *,
        timeout_ms: int = 90_000,
        settle_ms: int = 10_000,
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

    async def get_reviews_dom(
        self,
        page_url: str,
    ) -> dict[str, Any]:
        self.debug_dir.mkdir(parents=True, exist_ok=True)

        async with _import_invisible_playwright()(
            proxy=self.proxy,
            seed=self.seed,
            pin=self.pin,
            humanize=self.humanize,
        ) as browser:
            page = await browser.new_page()

            await page.goto(
                page_url,
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            )

            await asyncio.sleep(self.settle_ms / 1000)

            review_locator = page.locator(
                "[data-review-uuid]"
            )

            try:
                await review_locator.first.wait_for(
                    state="attached",
                    timeout=30_000,
                )
            except Exception as exc:
                await self._save_debug(page)
                raise RuntimeError(
                    "Карточки отзывов не найдены. "
                    f"URL: {page.url}; "
                    f"HTML сохранён в {self.debug_dir}"
                ) from exc

            review_count = await review_locator.count()
            reviews: list[dict[str, Any]] = []

            for index in range(review_count):
                card = review_locator.nth(index)
                reviews.append(
                    await self._read_review_card(card)
                )

            average_rating = await self._read_average_rating(page)

            result = {
                "average_rating": average_rating,
                "review_count": len(reviews),
                "reviews": reviews,
            }

            (self.debug_dir / "reviews_dom.json").write_text(
                json.dumps(
                    result,
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            await page.screenshot(
                path=str(self.debug_dir / "reviews_page.png"),
                full_page=True,
            )

            return result

    async def _read_review_card(self, card) -> dict[str, Any]:
        uuid = await card.get_attribute("data-review-uuid")
        published_at = await card.get_attribute("publishedat")
        status_id = await card.get_attribute("statusid")

        text = await card.inner_text()

        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip()
        ]

        author = self._extract_author(lines)
        review_date = self._extract_date(lines)
        review_text = self._extract_review_text(
            lines=lines,
            author=author,
            review_date=review_date,
        )

        images_locator = card.locator("img")
        image_count = await images_locator.count()

        images: list[str] = []

        for index in range(image_count):
            src = await images_locator.nth(index).get_attribute("src")
            if src:
                images.append(src)

        rating = await self._read_review_rating(card)

        return {
            "uuid": uuid,
            "published_at": published_at,
            "status_id": status_id,
            "author": author,
            "date": review_date,
            "rating": rating,
            "text": review_text,
            "images": images,
            "raw_text": text,
        }

    async def _read_review_rating(self, card) -> int | None:
        """
        Читает индивидуальный рейтинг отзыва.

        В сохранённом HTML каждая звезда представлена SVG.
        У заполненной и незаполненной звезды различается
        computed color, поэтому проверяем цвет через evaluate.
        """
        rating_container = card.locator(
            '[class*="rpProducta9c"]'
        )

        if await rating_container.count() == 0:
            return None

        stars = rating_container.locator("svg")
        star_count = await stars.count()

        if star_count == 0:
            return None

        filled = 0

        for index in range(star_count):
            star = stars.nth(index)

            try:
                color = await star.evaluate(
                    """
                    (element) => {
                        const path = element.querySelector("path");
                        if (!path) {
                            return null;
                        }

                        return {
                            elementColor:
                                getComputedStyle(element).color,
                            pathFill:
                                getComputedStyle(path).fill,
                            pathAttribute:
                                path.getAttribute("fill"),
                            className:
                                element.getAttribute("class") || ""
                        };
                    }
                    """
                )
            except Exception:
                continue

            if self._is_filled_star(color):
                filled += 1

        return filled if filled else None

    @staticmethod
    def _is_filled_star(color: dict[str, Any] | None) -> bool:
        if not color:
            return False

        values = " ".join(
            str(value).lower()
            for value in color.values()
            if value is not None
        )

        # Жёлтая звезда обычно имеет один из этих признаков.
        # Конкретные RGB-значения могут измениться у Ozon.
        yellow_markers = (
            "rgb(255, 168",
            "rgb(255, 170",
            "rgb(255, 184",
            "rgb(255, 185",
            "#f",
            "currentcolor",
        )

        return any(marker in values for marker in yellow_markers)

    async def _read_average_rating(self, page) -> float | None:
        score = page.locator(
            '[data-widget="webReviewProductScore"]'
        )

        if await score.count() == 0:
            return None

        text = await score.inner_text()

        import re

        match = re.search(
            r"(\d+(?:[.,]\d+)?)\s*/\s*5",
            text,
        )

        if not match:
            return None

        return float(match.group(1).replace(",", "."))

    @staticmethod
    def _extract_author(lines: list[str]) -> str | None:
        if len(lines) >= 2:
            # Первая строка — инициалы в аватаре,
            # вторая — отображаемое имя.
            return lines[1]

        return lines[0] if lines else None

    @staticmethod
    def _extract_date(lines: list[str]) -> str | None:
        import re

        date_pattern = re.compile(
            r"^\d{1,2}\s+"
            r"(?:января|февраля|марта|апреля|мая|июня|"
            r"июля|августа|сентября|октября|ноября|декабря)"
            r"\s+\d{4}$",
            re.IGNORECASE,
        )

        for line in lines:
            if date_pattern.match(line):
                return line

        return None

    @staticmethod
    def _extract_review_text(
        *,
        lines: list[str],
        author: str | None,
        review_date: str | None,
    ) -> str | None:
        excluded = {
            author,
            review_date,
            "Вам помог этот отзыв?",
        }

        result: list[str] = []

        for line in lines:
            if line in excluded:
                continue

            if line in {"Да", "Нет"}:
                continue

            if line.startswith("Да ") or line.startswith("Нет "):
                continue

            result.append(line)

        return "\n".join(result) or None

    async def _save_debug(self, page) -> None:
        (self.debug_dir / "reviews_page.html").write_text(
            await page.content(),
            encoding="utf-8",
        )

        await page.screenshot(
            path=str(self.debug_dir / "reviews_page.png"),
            full_page=True,
        )

    async def _read_review_cards(
        self,
        review_locator,
    ) -> list[dict[str, Any]]:
        count = await review_locator.count()
        result: list[dict[str, Any]] = []

        for index in range(count):
            card = review_locator.nth(index)

            uuid = await card.get_attribute(
                "data-review-uuid"
            )
            published_at = await card.get_attribute(
                "publishedat"
            )
            status_id = await card.get_attribute(
                "statusid"
            )

            text = await card.inner_text()

            images_locator = card.locator("img")
            image_count = await images_locator.count()
            images: list[str] = []

            for image_index in range(image_count):
                src = await images_locator.nth(
                    image_index
                ).get_attribute("src")

                if src:
                    images.append(src)

            result.append(
                {
                    "uuid": uuid,
                    "published_at": published_at,
                    "status_id": status_id,
                    "text": text,
                    "images": images,
                }
            )

        return result

    async def iter_reviews(
            self,
            page_url: str,
            *,
            max_reviews: int | None = None,
            max_idle_rounds: int = 4,
            scroll_step: int = 900,
            scroll_pause_ms: int = 600,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        async with _import_invisible_playwright()(
                humanize=True,
        ) as browser:
            page = await browser.new_page()

            await page.goto(
                page_url,
                wait_until="domcontentloaded",
                timeout=10_000,
            )

            review_locator = page.locator(
                "[data-review-uuid]"
            )

            try:
                await review_locator.first.wait_for(
                    state="attached",
                    timeout=30_000,
                )
            except Exception as exc:
                raise RuntimeError(
                    "На странице нет элементов [data-review-uuid]"
                ) from exc

            emitted_ids: set[str] = set()
            idle_rounds = 0

            while True:
                cards = await self._read_review_cards(
                    review_locator,
                )

                new_cards = []

                for card in cards:
                    review_id = card.get("uuid")

                    if not review_id:
                        continue

                    if review_id in emitted_ids:
                        continue

                    emitted_ids.add(review_id)
                    new_cards.append(card)

                if new_cards:
                    idle_rounds = 0
                    yield new_cards
                else:
                    idle_rounds += 1

                if (
                        max_reviews is not None
                        and len(emitted_ids) >= max_reviews
                ):
                    return

                if idle_rounds >= max_idle_rounds:
                    return

                before_count = await review_locator.count()

                await page.mouse.wheel(0, scroll_step)
                await asyncio.sleep(
                    scroll_pause_ms / 1000
                )

                after_count = await review_locator.count()

                if after_count <= before_count:
                    # Даём lazy-load ещё один шанс.
                    await asyncio.sleep(2)

                    final_count = await review_locator.count()

                    if final_count <= before_count:
                        idle_rounds += 1
