"""Mixin for PublicPageTransport."""
from __future__ import annotations

from typing import Any


class CardReadingMixin:
    # ------------------------------------------------------------------
    # DOM card reader
    # ------------------------------------------------------------------
    async def _read_review_cards(self, review_locator) -> list[dict[str, Any]]:
        """Read all ``[data-review-uuid]`` cards on the current page.

        Each card becomes a dict with:
          - ``uuid``: review UUID (from ``data-review-uuid``)
          - ``published_at``: Unix timestamp (from ``publishedat``)
          - ``status_id``: moderation status (from ``statusid``)
          - ``text``: raw inner_text of the card (avatar initials,
            author, date, review text, "Вам помог этот отзыв?",
            "Да N Нет M")
          - ``rating``: 1-5 (from SVG star colors) or None
          - ``images``: list of img src URLs

        The adapter's ``parse_ozon_dom_card`` will further extract
        ``author``, ``date``, ``review_text`` from the lines.
        """
        result: list[dict[str, Any]] = []
        count = await review_locator.count()

        for index in range(count):
            card = review_locator.nth(index)

            uuid = await card.get_attribute("data-review-uuid")
            published_at = await card.get_attribute("publishedat")
            status_id = await card.get_attribute("statusid")
            text = await card.inner_text()
            rating = await self._read_review_rating(card)
            images = await self._read_images(card)

            result.append({
                "uuid": uuid,
                "published_at": published_at,
                "status_id": status_id,
                "text": text,
                "rating": rating,
                "images": images,
            })

        return result

    # Star-rating extractor. Ozon rotates the obfuscated CSS class
    # names of the star container (rpProducta9c → a5d5_5_1-a → …),
    # so any class-based selector dies within weeks. What does NOT
    # rotate: the star glyph path data (one ``d`` attribute shared
    # by all 5 star slots of a row, filled = currentColor with the
    # computed orange rgb(255, 168, 0)) — measured 2026-09-15.
    _RATING_JS = """
    (card) => {
        const byGlyph = new Map();
        card.querySelectorAll('svg path').forEach(p => {
            const d = p.getAttribute('d');
            if (!d) return;
            if (!byGlyph.has(d)) byGlyph.set(d, []);
            byGlyph.get(d).push(getComputedStyle(p).fill);
        });
        // The star row is the glyph that appears 3-6 times; other
        // svg icons in a card appear once or twice.
        let starFills = null;
        for (const fills of byGlyph.values()) {
            if (fills.length >= 3 && fills.length <= 6) {
                if (!starFills || fills.length > starFills.length) {
                    starFills = fills;
                }
            }
        }
        if (!starFills) return null;
        let orange = 0;
        for (const f of starFills) {
            const m = f.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
            if (!m) continue;
            const r = +m[1], g = +m[2], b = +m[3];
            if (r >= 200 && g >= 120 && g <= 220 && b <= 100) orange++;
        }
        return orange > 0 ? orange : null;
    }
    """

    async def _read_review_rating(self, card) -> int | None:
        """Read the per-review star rating by counting orange-filled
        star SVGs (see ``_RATING_JS`` for why this is glyph-based).
        """
        try:
            return await card.evaluate(self._RATING_JS)
        except Exception:
            return None

    # Batch readers for the widget flow: one evaluate per PAGE
    # instead of ~240 sequential per-card attribute round-trips
    # (3 attributes + inner_text + rating + images per card × 30
    # cards) — the dominant per-page cost after navigation itself.
    _UUIDS_JS = (
        "() => [...document.querySelectorAll("
        "'[data-review-uuid]')]"
        ".map(e => e.getAttribute('data-review-uuid'))"
    )

    _READ_CARDS_JS = """
    () => {
        const ratingOf = (card) => {
            const byGlyph = new Map();
            card.querySelectorAll('svg path').forEach(p => {
                const d = p.getAttribute('d');
                if (!d) return;
                if (!byGlyph.has(d)) byGlyph.set(d, []);
                byGlyph.get(d).push(getComputedStyle(p).fill);
            });
            let starFills = null;
            for (const fills of byGlyph.values()) {
                if (fills.length >= 3 && fills.length <= 6) {
                    if (!starFills || fills.length > starFills.length)
                        starFills = fills;
                }
            }
            const countOrange = fills => {
                let orange = 0;
                for (const f of fills) {
                    const m = f.match(
                        /rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/,
                    );
                    if (!m) continue;
                    const r = +m[1], g = +m[2], b = +m[3];
                    if (r >= 200 && g >= 120 && g <= 220 && b <= 100)
                        orange++;
                }
                return orange;
            };
            if (starFills) {
                const orange = countOrange(starFills);
                return orange > 0 ? orange : null;
            }

            // Some Ozon layouts render only the filled star for a
            // one-star review, so there is no 3–6-item glyph group.
            // In that variant the count across the card is the rating.
            const orange = countOrange(
                [...card.querySelectorAll('svg path')]
                    .map(p => getComputedStyle(p).fill),
            );
            return orange >= 1 && orange <= 5 ? orange : null;
        };
        return [...document.querySelectorAll('[data-review-uuid]')]
            .map(card => ({
                uuid: card.getAttribute('data-review-uuid'),
                published_at: card.getAttribute('publishedat'),
                status_id: card.getAttribute('statusid'),
                text: card.innerText || '',
                rating: ratingOf(card),
                images: [...card.querySelectorAll('img')]
                    .map(i => i.getAttribute('src')).filter(Boolean),
            }));
    }
    """

    async def _page_uuids_fast(self, page) -> list[str]:
        """UUIDs of the currently rendered cards (one round-trip)."""
        try:
            out = await page.evaluate(self._UUIDS_JS)
            return [u for u in (out or []) if u]
        except Exception:
            return await self._locator_uuids(
                page.locator("[data-review-uuid]")
            )

    async def _read_cards_fast(self, page) -> list[dict[str, Any]]:
        """All rendered cards with rating, in one round-trip."""
        try:
            return await page.evaluate(self._READ_CARDS_JS) or []
        except Exception:
            return await self._read_review_cards(
                page.locator("[data-review-uuid]")
            )

    @staticmethod
    def _is_filled_star(color: dict[str, Any] | None) -> bool:
        """Heuristic for deciding whether a star SVG is filled."""
        if not color:
            return False
        values = " ".join(
            str(value).lower()
            for value in color.values()
            if value is not None
        )
        yellow_markers = (
            "rgb(255, 168",
            "rgb(255, 170",
            "rgb(255, 184",
            "rgb(255, 185",
            "#f",
            "currentcolor",
        )
        return any(marker in values for marker in yellow_markers)

    async def _read_images(self, card) -> list[str]:
        """Collect img src URLs from the card."""
        result: list[str] = []
        images = card.locator("img")
        for index in range(await images.count()):
            src = await images.nth(index).get_attribute("src")
            if src:
                result.append(src)
        return result

    async def _locator_uuids(self, locator) -> list[str]:
        """UUIDs of all cards the locator currently matches."""
        n = await locator.count()
        out = []
        for i in range(n):
            uuid = await locator.nth(i).get_attribute(
                "data-review-uuid"
            )
            if uuid:
                out.append(uuid)
        return out
