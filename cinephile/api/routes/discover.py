"""Browsing backed by TMDB: search, trending, filters, title details, personal picks."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, status

from cinephile import recommend, tmdb
from cinephile.api.deps import UserDep
from cinephile.api.presenters import ui_item
from cinephile.api.schemas import MediaFilter, MediaType
from cinephile.storage import library

router = APIRouter(tags=["discover"])

Page = Annotated[int, Query(ge=1, le=500)]
SortBy = Literal["popularity.desc", "vote_average.desc", "primary_release_date.desc", "first_air_date.desc"]
SIMILAR_LIMIT = 12


def _wrap(results: list[dict]) -> dict:
    return {"results": [ui_item(r) for r in results]}


async def _tmdb(awaitable):
    """Await a TMDB call, turning its failure into a 502 for the client."""
    try:
        return await awaitable
    except tmdb.TMDBError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.get("/search")
async def search(
    q: Annotated[str, Query(max_length=200)],
    media_type: MediaFilter = "all",
    year: Annotated[int | None, Query(ge=1870, le=2100)] = None,
    page: Page = 1,
) -> dict:
    if not q.strip():
        return {"results": []}
    return _wrap(await _tmdb(tmdb.search(q.strip(), media_type, year, page)))


@router.get("/trending")
async def trending(media_type: MediaFilter = "all", window: Literal["day", "week"] = "week", page: Page = 1) -> dict:
    return _wrap(await _tmdb(tmdb.get_trending(media_type, window, page)))


@router.get("/discover")
async def discover(  # noqa: PLR0913, PLR0917 - one query parameter per filter
    media_type: MediaType = "movie",
    genre: Annotated[str | None, Query(max_length=60)] = None,
    year: Annotated[int | None, Query(ge=1870, le=2100)] = None,
    min_rating: Annotated[float | None, Query(ge=0, le=10)] = None,
    sort_by: SortBy = "popularity.desc",
    page: Page = 1,
) -> dict:
    genre_id = None
    if genre:
        genre_id = await _tmdb(tmdb.resolve_genre_id(genre, media_type))
        if genre_id is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown genre: {genre}")
    found = tmdb.discover(media_type, genre_id=genre_id, year=year, min_rating=min_rating, sort_by=sort_by, page=page)
    return _wrap(await _tmdb(found))


@router.get("/genres")
async def genres(media_type: MediaType = "movie") -> dict:
    found = await _tmdb(tmdb.get_genres(media_type))
    return {"genres": [g["name"] for g in found]}


@router.get("/titles/{media_type}/{tmdb_id}")
async def title_details(media_type: MediaType, tmdb_id: Annotated[int, Path(gt=0)], user_id: UserDep) -> dict:
    details = await _tmdb(tmdb.get_details(tmdb_id, media_type))
    review = library.get_review(user_id, tmdb_id, media_type)
    return {
        **ui_item(details, "w500"),
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
            "favorite": library.is_favorite(user_id, tmdb_id, media_type),
            "watching": library.get_watch_status(user_id, tmdb_id, media_type),
            "review": review["rating"] if review else None,
        },
    }


@router.get("/titles/{media_type}/{tmdb_id}/similar")
async def title_similar(media_type: MediaType, tmdb_id: Annotated[int, Path(gt=0)]) -> dict:
    return _wrap(await _tmdb(tmdb.get_related(tmdb_id, media_type, SIMILAR_LIMIT)))


@router.get("/for-you")
async def for_you(user_id: UserDep, limit: Annotated[int, Query(ge=1, le=20)] = 12) -> dict:
    """Personal picks from the user's saved titles ("because you liked ...")."""
    data = await _tmdb(recommend.personalized(user_id, limit))
    return {"strategy": data["strategy"], "seeds": data["seeds"], **_wrap(data["results"])}
