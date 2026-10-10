(cards) => {
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
                        /rgba?\((\d+),\s*(\d+),\s*(\d+)/,
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
        return cards
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
