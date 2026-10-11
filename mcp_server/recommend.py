"""
recommend.py - Personalized recommendations from the user's saved titles.

Used by the recommend_for_user MCP tool and by the web API ("because you liked ...").
"""

from __future__ import annotations

import asyncio

try:
    from . import database, tmdb
except ImportError:  # run as a script from inside mcp_server/
    import database
    import tmdb

MAX_SEEDS = 4


def _seeds() -> tuple[list[tuple[float, dict]], set[tuple[int, str]]]:
    """Weighted seed titles (best-rated reviews, then favorites, then watch list) and everything already saved."""
    reviews = database.list_reviews()
    favorites = database.list_favorites()
    watching = database.list_watching()

    saved = {(r["tmdb_id"], r["media_type"]) for r in reviews + favorites + watching}

    weighted: dict[tuple[int, str], tuple[float, dict]] = {}
    for row in reviews:
        if row["rating"] >= 6:
            weighted[(row["tmdb_id"], row["media_type"])] = (row["rating"] / 10.0, row)
    for row in favorites:
        weighted.setdefault((row["tmdb_id"], row["media_type"]), (0.8, row))
    for row in watching:
        weighted.setdefault((row["tmdb_id"], row["media_type"]), (0.6, row))

    return sorted(weighted.values(), key=lambda s: -s[0])[:MAX_SEEDS], saved


async def personalized(limit: int = 8) -> dict:
    """Ranked recommendations with a short reason each. Raises tmdb.TMDBError if TMDB is unreachable."""
    limit = max(1, min(limit, 20))
    seeds, saved = _seeds()

    if not seeds:
        trending = await tmdb.get_trending("all", "week")
        return {
            "strategy": "trending (the user has no saved titles yet)",
            "seeds": [],
            "count": len(trending[:limit]),
            "results": [tmdb.card(r) for r in trending[:limit]],
        }

    batches = await asyncio.gather(
        *(tmdb.get_recommended(s["tmdb_id"], s["media_type"]) for _, s in seeds),
        return_exceptions=True,
    )
    if all(isinstance(b, Exception) for b in batches):
        raise batches[0]

    candidates: dict[tuple[int, str], dict] = {}
    for (weight, seed), recs in zip(seeds, batches):
        if isinstance(recs, Exception):
            continue
        for rec in recs:
            key = (rec["tmdb_id"], rec["media_type"])
            if not rec["tmdb_id"] or key in saved:
                continue
            entry = candidates.setdefault(key, {"item": rec, "score": 0.0, "based_on": []})
            entry["score"] += weight + (rec.get("rating") or 0) / 10.0
            entry["based_on"].append(seed["title"])

    ranked = sorted(candidates.values(), key=lambda c: -c["score"])[:limit]
    return {
        "strategy": "personalized from the user's saved titles",
        "seeds": [s["title"] for _, s in seeds],
        "count": len(ranked),
        "results": [{**tmdb.card(c["item"]), "because_you_liked": c["based_on"][:2]} for c in ranked],
    }
