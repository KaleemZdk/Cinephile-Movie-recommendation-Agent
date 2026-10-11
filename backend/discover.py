"""
discover.py - Browsing endpoints backed by TMDB: search, trending, filters, details, personal picks.
"""

from fastapi import APIRouter, HTTPException

from backend.cards import ui_item
from mcp_server import database, recommend, tmdb

router = APIRouter(tags=["discover"])


def _wrap(results: list[dict]) -> dict:
    return {"results": [ui_item(r) for r in results]}


async def _tmdb(coro):
    try:
        return await coro
    except tmdb.TMDBError as e:
        raise HTTPException(502, str(e))


def _media(media_type: str, allowed=("all", "movie", "tv")) -> str:
    if media_type not in allowed:
        raise HTTPException(400, f"media_type must be one of: {', '.join(allowed)}.")
    return media_type


@router.get("/search")
async def search(q: str, media_type: str = "all", year: int | None = None, page: int = 1):
    if not q.strip():
        return {"results": []}
    return _wrap(await _tmdb(tmdb.search(q.strip(), _media(media_type), year, page)))


@router.get("/trending")
async def trending(media_type: str = "all", window: str = "week", page: int = 1):
    return _wrap(await _tmdb(tmdb.get_trending(_media(media_type), window, page)))


@router.get("/discover")
async def discover(
    media_type: str = "movie",
    genre: str | None = None,
    year: int | None = None,
    min_rating: float | None = None,
    sort_by: str = "popularity.desc",
    page: int = 1,
):
    kind = _media(media_type, ("movie", "tv"))
    genre_id = None
    if genre:
        genre_id = await _tmdb(tmdb.resolve_genre_id(genre, kind))
        if genre_id is None:
            raise HTTPException(400, f"Unknown genre: {genre}")
    return _wrap(await _tmdb(tmdb.discover(kind, genre_id, year, min_rating, sort_by, page)))


@router.get("/genres")
async def genres(media_type: str = "movie"):
    found = await _tmdb(tmdb.get_genres(_media(media_type, ("movie", "tv"))))
    return {"genres": [g["name"] for g in found]}


@router.get("/titles/{media_type}/{tmdb_id}")
async def title_details(media_type: str, tmdb_id: int):
    kind = _media(media_type, ("movie", "tv"))
    details = await _tmdb(tmdb.get_details(tmdb_id, kind))
    item = ui_item(details, "w500")
    item.update({
        "backdrop_url": tmdb.image_url(details.get("backdrop_path"), "w780"),
        "tagline": details.get("tagline"),
        "runtime": details.get("runtime"),
        "status": details.get("status"),
        "cast": details.get("cast", []),
        "director": details.get("director"),
        "created_by": details.get("created_by", []),
        "seasons": details.get("seasons"),
        "episodes": details.get("episodes"),
        "keywords": details.get("keywords", []),
        "trailer_url": details.get("trailer_url"),
        "where_to_watch": details.get("where_to_watch"),
        "library": {
            "favorite": database.is_favorite(tmdb_id, kind),
            "review": (database.get_review(tmdb_id, kind) or {}).get("rating"),
            "watching": next((w["status"] for w in database.list_watching() if w["tmdb_id"] == tmdb_id and w["media_type"] == kind), None),
        },
    })
    return item


@router.get("/titles/{media_type}/{tmdb_id}/similar")
async def title_similar(media_type: str, tmdb_id: int):
    kind = _media(media_type, ("movie", "tv"))
    results = await _tmdb(tmdb.get_recommended(tmdb_id, kind))
    if len(results) < 6:
        results += await _tmdb(tmdb.get_similar(tmdb_id, kind))
    seen, unique = set(), []
    for r in results:
        if r["tmdb_id"] not in seen:
            seen.add(r["tmdb_id"])
            unique.append(r)
    return _wrap(unique[:12])


@router.get("/for-you")
async def for_you(limit: int = 12):
    """Personal picks from the user's saved titles ('because you liked ...')."""
    data = await _tmdb(recommend.personalized(limit))
    return {"strategy": data["strategy"], "seeds": data["seeds"], **_wrap(data["results"])}
