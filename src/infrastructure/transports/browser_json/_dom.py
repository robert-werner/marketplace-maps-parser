"""Mixin."""
from __future__ import annotations

from pathlib import Path
from typing import Any

_READ_CARDS_JS = Path(__file__).with_name("read_cards.js").read_text(
    encoding="utf-8",
)


class CardReadingMixin:
    async def _read_review_cards(
            self,
            review_locator: Any,
    ) -> list[dict[str, Any]]:
        # Fetch the whole card set in a single browser round-trip.
        evaluate_all = getattr(review_locator, "evaluate_all", None)
        if evaluate_all is not None:
            result = await evaluate_all(_READ_CARDS_JS)
            if isinstance(result, list) and all(
                isinstance(card, dict) for card in result
            ):
                return result
            raise ValueError("Invalid batch DOM card result")
        # Compatibility with wrappers/test doubles lacking evaluate_all.
        result: list[dict[str, Any]] = []

        for index in range(await review_locator.count()):
            card = review_locator.nth(index)

            result.append(
                {
                    "uuid": await card.get_attribute(
                        "data-review-uuid"
                    ),
                    "published_at": await card.get_attribute(
                        "publishedat"
                    ),
                    "status_id": await card.get_attribute(
                        "statusid"
                    ),
                    "text": await card.inner_text(),
                    "rating": await self._read_review_rating(card),
                    "images": await self._read_images(card),
                }
            )

        return result

    async def _read_review_rating(self, card: Any) -> int | None:
        """Read the per-review star rating from the DOM card.

        Each star is an SVG; the filled vs unfilled star has a
        different computed color (yellow vs grey). We count the
        yellow (filled) stars. The selector matches the rating
        container that Ozon wraps around the stars.

        This locator reader remains a compatibility fallback.
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

        for star_index in range(star_count):
            star = stars.nth(star_index)

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
        """Heuristic for deciding whether a star SVG is filled yellow
        (counted) or unfilled grey (not counted).
        """
        if not color:
            return False

        values = " ".join(
            str(value).lower()
            for value in color.values()
            if value is not None
        )

        # Yellow star markers — Ozon may change the exact RGB.
        yellow_markers = (
            "rgb(255, 198, 0)",
            "rgb(255, 198, 51)",
            "#ffc600",
            "#ffc633",
            "#ffce00",
            "ffc600",
            "ffc633",
            "ffce00",
        )
        if any(marker in values for marker in yellow_markers):
            return True

        # Class-based marker used by some Ozon layouts.
        if "filled" in values and "empty" not in values:
            return True

        return False

    async def _read_images(
            self,
            card: Any,
    ) -> list[str]:
        result: list[str] = []
        images = card.locator("img")

        for index in range(await images.count()):
            src = await images.nth(index).get_attribute(
                "src"
            )

            if src:
                result.append(src)

        return result
