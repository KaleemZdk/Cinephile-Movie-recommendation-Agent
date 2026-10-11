"""Personalized recommendations from the user's saved titles.

Used by the recommend_for_user MCP tool and by the web API ("Picked for you").
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from cinephile import tmdb
from cinephile.storage import library

MAX_SEEDS = 4
MIN_SEED_RATING = 6
FAVORITE_WEIGHT = 0.8
WATCHING_WEIGHT = 0.6


@dataclass(frozen=True, slots=True)
class Seed:
    weight: float
    tmdb_id: int
    media_type: str
    title: str


def _seeds() -> list[Seed]:
    """Best-rated reviews first, then favorites, then the watch list."""
    weighted: dict[tuple[int, str], Seed] = {}

    def add(row: dict, weight: float) -> None:
        key = (row["tmdb_id"], row["media_type"])
        if key not in weighted:
            weighted[key] = Seed(weight, row["tmdb_id"], row["media_type"], row["title"])

    for row in library.list_reviews():
        if row["rating"] >= MIN_SEED_RATING:
            add(row, row["rating"] / 10.0)
    for row in library.list_favorites():
        add(row, FAVORITE_WEIGHT)
    for row in library.list_watching():
        add(row, WATCHING_WEIGHT)
    return sorted(weighted.values(), key=lambda s: -s.weight)[:MAX_SEEDS]


async def personalized(limit: int = 8) -> dict:
    """Ranked recommendations with a short reason each. Raises tmdb.TMDBError if TMDB is unreachable."""
    limit = max(1, min(limit, 20))
    seeds = _seeds()

    if not seeds:
        trending = await tmdb.get_trending("all", "week")
        return {
            "strategy": "trending (the user has no saved titles yet)",
            "seeds": [],
            "count": len(trending[:limit]),
            "results": [tmdb.card(r) for r in trending[:limit]],
        }

    batches = await asyncio.gather(
        *(tmdb.get_recommended(s.tmdb_id, s.media_type) for s in seeds),
        return_exceptions=True,
    )
    failures = [b for b in batches if isinstance(b, BaseException)]
    if len(failures) == len(batches):
        raise failures[0]

    saved = library.saved_keys()
    candidates: dict[tuple[int, str], dict] = {}
    for seed, recs in zip(seeds, batches, strict=True):
        if isinstance(recs, BaseException):
            continue
        for rec in recs:
            key = (rec["tmdb_id"], rec["media_type"])
            if not rec["tmdb_id"] or key in saved:
                continue
            entry = candidates.setdefault(key, {"item": rec, "score": 0.0, "based_on": []})
            entry["score"] += seed.weight + (rec.get("rating") or 0) / 10.0
            entry["based_on"].append(seed.title)

    ranked = sorted(candidates.values(), key=lambda c: -c["score"])[:limit]
    return {
        "strategy": "personalized from the user's saved titles",
        "seeds": [s.title for s in seeds],
        "count": len(ranked),
        "results": [{**tmdb.card(c["item"]), "because_you_liked": c["based_on"][:2]} for c in ranked],
    }
