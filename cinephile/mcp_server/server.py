"""MCP tools for the Cinephile agent.

Combines TMDB lookups (movies and TV) with the user's library in SQLite. Tools that save
or look up a title accept a plain `title` (optionally with `media_type` and `year`), so
the agent does not need to search first. When a title is ambiguous the tool returns
`needs_clarification` with candidates instead of guessing.

Every tool returns compact JSON text: it is read by the model, and size costs latency.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from cinephile import recommend, tmdb
from cinephile.storage import library
from cinephile.storage.db import WATCH_STATUSES, normalize_media_type
from cinephile.storage.library import TitleRef

LIST_LIMIT = 10  # results handed to the model per call

mcp = FastMCP(
    "cinephile",
    instructions=(
        "Tools for a movie and TV recommendation agent.\n"
        "- Find titles: search_shows, get_details, get_similar, get_trending, discover, list_genres.\n"
        "- The user's library: add_favorite, list_favorites, remove_favorite, set_watching, "
        "list_watching, remove_watching, save_review, get_review, list_reviews.\n"
        "- get_user_profile returns everything the user saved; recommend_for_user builds a "
        "personalized list from it.\n"
        "Library tools take a plain title; no search is needed first."
    ),
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def _err(message: str, **extra: Any) -> str:
    return _dumps({"error": message, **extra})


def _listing(results: list[dict], **extra: Any) -> str:
    shown = results[:LIST_LIMIT]
    return _dumps({**extra, "count": len(shown), "results": [tmdb.card(r) for r in shown]})


def _brief(item: dict) -> dict:
    """Identity of a title: enough to refer back to it in a later tool call."""
    return {
        "tmdb_id": item["tmdb_id"],
        "media_type": item["media_type"],
        "title": item["title"],
        "year": item.get("year"),
    }


def _saved_brief(row: dict) -> dict:
    out = _brief(row)
    for key in ("status", "rating", "comment", "added_at", "updated_at"):
        if row.get(key) not in (None, ""):
            out[key] = row[key]
    return out


def _clarify(message: str, candidates: list[dict]) -> str:
    return _dumps({"needs_clarification": True, "message": message, "candidates": candidates})


def _bad_status(status: str | None) -> str | None:
    if status and status not in WATCH_STATUSES:
        return _err(f"status must be one of: {', '.join(WATCH_STATUSES)}.")
    return None


async def _details_or_none(tmdb_id: int, media_type: str) -> dict | tmdb.TMDBError:
    try:
        return await tmdb.get_details(tmdb_id, media_type)
    except tmdb.TMDBError as e:
        return e


async def _lookup_by_id(tmdb_id: int, kind: str | None, title: str | None) -> tuple[dict | None, str | None]:
    kinds = [kind] if kind else ["movie", "tv"]
    outcomes = await asyncio.gather(*(_details_or_none(tmdb_id, k) for k in kinds))
    found = [o for o in outcomes if isinstance(o, dict)]
    if not found:
        return None, _err(str(outcomes[0]), tmdb_id=tmdb_id)
    if title:
        named = [f for f in found if tmdb.same_title(f["title"], title)]
        if named:
            return named[0], None
    if len(found) == 1:
        return found[0], None
    # The id exists as both a movie and a show and nothing says which one is meant.
    return None, _clarify(
        "This id matches a movie and a TV show. Call again with media_type.",
        [_brief(f) for f in found],
    )


async def _lookup(
    title: str | None = None,
    tmdb_id: int | None = None,
    media_type: str | None = None,
    year: int | str | None = None,
) -> tuple[dict | None, str | None]:
    """Resolve a title or id on TMDB. Returns (item, None) or (None, json_message_for_the_model)."""
    kind = normalize_media_type(media_type, default=None)
    if tmdb_id:
        return await _lookup_by_id(tmdb_id, kind, title)
    if not title or not title.strip():
        return None, _err("Provide a title or a tmdb_id.")
    try:
        resolved = await tmdb.resolve_title(title.strip(), kind, year)
    except tmdb.TMDBError as e:
        return None, _err(str(e))
    if resolved["match"]:
        return resolved["match"], None
    if not resolved["candidates"]:
        return None, _err(f"No movie or TV show found for '{title}'. Ask the user to check the spelling.")
    return None, _clarify(
        f"'{title}' matches several titles. Ask the user which one, then call again with tmdb_id and media_type.",
        [tmdb.card(c) for c in resolved["candidates"]],
    )


def _find_saved(
    rows: list[dict],
    title: str | None,
    tmdb_id: int | None,
    media_type: str | None,
) -> tuple[dict | None, str | None]:
    """Find one entry in the user's own saved list by title or id."""
    kind = normalize_media_type(media_type, default=None)
    matches = [r for r in rows if not kind or r["media_type"] == kind]
    if tmdb_id:
        matches = [r for r in matches if r["tmdb_id"] == tmdb_id]
    elif title:
        wanted = tmdb.normalize_title(title)
        exact = [r for r in matches if tmdb.normalize_title(r["title"]) == wanted]
        matches = exact or [r for r in matches if wanted and wanted in tmdb.normalize_title(r["title"])]
    else:
        return None, _err("Provide a title or a tmdb_id.")

    if not matches:
        return None, _err("That title is not in the user's saved list.")
    if len(matches) > 1:
        return None, _clarify("Several saved titles match. Ask the user which one.", [_brief(m) for m in matches])
    return matches[0], None


# --------------------------------------------------------------------------- #
# Finding titles
# --------------------------------------------------------------------------- #


@mcp.tool()
async def search_shows(query: str, media_type: str = "all", year: int | None = None, page: int = 1) -> str:
    """Search TMDB for movies and TV shows by title or keyword.

    Use this to browse or look up titles. Results include tmdb_id, media_type, year,
    rating and genres. You do NOT need to search before saving a title to the user's
    library; the library tools accept a title directly.

    Args:
        query: Free-text query, e.g. "Blade Runner", "Dark".
        media_type: "all" (default), "movie" or "tv".
        year: Optional release year to narrow the search (needs media_type movie or tv).
        page: 1-based page number.
    """
    try:
        results = await tmdb.search(query, media_type, year, page)
    except tmdb.TMDBError as e:
        return _err(str(e))
    return _listing(results, query=query)


@mcp.tool()
async def search_movies(query: str, page: int = 1) -> str:
    """Search TMDB for movies only. Prefer search_shows, which covers movies and TV.

    Args:
        query: Free-text query.
        page: 1-based page number.
    """
    return await search_shows(query, "movie", None, page)


@mcp.tool()
async def get_details(
    title: str | None = None,
    tmdb_id: int | None = None,
    media_type: str | None = None,
    year: int | None = None,
) -> str:
    """Full details for one movie or TV show: overview, cast, director or creators,
    runtime, trailer link and where to stream it.

    Give either a title (optionally with media_type and year) or a tmdb_id.

    Args:
        title: Title to look up.
        tmdb_id: TMDB id, if known from an earlier result.
        media_type: "movie" or "tv". Needed when an id could belong to either.
        year: Release year, to disambiguate remakes.
    """
    item, problem = await _lookup(title, tmdb_id, media_type, year)
    if problem:
        return problem
    try:
        details = item if "cast" in item else await tmdb.get_details(item["tmdb_id"], item["media_type"])
    except tmdb.TMDBError as e:
        return _err(str(e), tmdb_id=item["tmdb_id"])
    return _dumps(tmdb.card({k: v for k, v in details.items() if k != "backdrop_path"}))


@mcp.tool()
async def get_similar(show_id: int | None = None, media_type: str | None = None, title: str | None = None) -> str:
    """Titles similar to a given movie or TV show ("something like X", "more like this").

    Pass the title directly; no search is needed first. Results combine TMDB's
    recommendations with its similar-titles list, best matches first.

    Args:
        show_id: TMDB id of the reference title, if you have it from an earlier result.
        media_type: "movie" or "tv".
        title: Reference title, e.g. "Dark".
    """
    item, problem = await _lookup(title, show_id, media_type)
    if problem:
        return problem
    try:
        results = await tmdb.get_related(item["tmdb_id"], item["media_type"], LIST_LIMIT)
    except tmdb.TMDBError as e:
        return _err(str(e), tmdb_id=item["tmdb_id"])
    return _listing(results, based_on=_brief(item))


@mcp.tool()
async def get_trending(media_type: str = "all", window: str = "week", page: int = 1) -> str:
    """What is trending right now on TMDB.

    Args:
        media_type: "all" (default), "movie" or "tv".
        window: "day" or "week" (default "week").
        page: 1-based page number.
    """
    try:
        results = await tmdb.get_trending(media_type, window, page)
    except tmdb.TMDBError as e:
        return _err(str(e))
    return _listing(results, window=window)


@mcp.tool()
async def discover(  # noqa: PLR0913, PLR0917 - a tool's signature is its schema for the model
    media_type: str = "movie",
    genre: str | None = None,
    year: int | None = None,
    min_rating: float | None = None,
    sort_by: str = "popularity.desc",
    page: int = 1,
) -> str:
    """Browse movies or TV shows by genre, year and rating.

    Args:
        media_type: "movie" (default) or "tv".
        genre: Genre name such as "Thriller", "Sci-Fi", "Comedy" (resolved automatically).
        year: Release year (first air year for TV).
        min_rating: Minimum vote average 0-10 (only titles with 100+ votes).
        sort_by: "popularity.desc", "vote_average.desc" or "primary_release_date.desc"
                 (use "first_air_date.desc" for newest TV).
        page: 1-based page number.
    """
    try:
        genre_id = None
        if genre:
            genre_id = await tmdb.resolve_genre_id(genre, media_type)
            if genre_id is None:
                names = [g["name"] for g in await tmdb.get_genres(media_type)]
                return _err(f"Unknown genre '{genre}'.", valid_genres=names)
        results = await tmdb.discover(
            media_type, genre_id=genre_id, year=year, min_rating=min_rating, sort_by=sort_by, page=page
        )
    except tmdb.TMDBError as e:
        return _err(str(e))
    filters = {"media_type": media_type, "genre": genre, "year": year, "min_rating": min_rating, "sort_by": sort_by}
    return _listing(results, filters={k: v for k, v in filters.items() if v})


@mcp.tool()
async def list_genres(media_type: str = "movie") -> str:
    """List TMDB genre names. discover() accepts a genre name directly, so this is rarely needed.

    Args:
        media_type: "movie" (default) or "tv" (the two lists differ).
    """
    try:
        genres = await tmdb.get_genres(media_type)
    except tmdb.TMDBError as e:
        return _err(str(e))
    return _dumps({"genres": [g["name"] for g in genres]})


# --------------------------------------------------------------------------- #
# Library: favorites
# --------------------------------------------------------------------------- #


@mcp.tool()
async def add_favorite(
    title: str | None = None,
    tmdb_id: int | None = None,
    media_type: str | None = None,
    year: int | None = None,
) -> str:
    """Save a movie or TV show to the user's favorites. Pass just the title; no search needed.

    If you already showed the title, pass its year and media_type so it resolves in one
    call. If the result has needs_clarification and candidates, ask the user which one
    they mean, then call again with tmdb_id and media_type.

    Args:
        title: Title the user named, e.g. "Whiplash".
        tmdb_id: Exact TMDB id (only if already known).
        media_type: "movie" or "tv", to avoid ambiguity.
        year: Release year, to pick between remakes.
    """
    item, problem = await _lookup(title, tmdb_id, media_type, year)
    if problem:
        return problem
    added = library.add_favorite(TitleRef.from_mapping(item))
    return _dumps({"success": True, "already_existed": not added, "item": _brief(item)})


@mcp.tool()
def list_favorites() -> str:
    """List every movie and TV show the user has favorited."""
    favs = library.list_favorites()
    return _dumps({"count": len(favs), "favorites": [_saved_brief(f) for f in favs]})


@mcp.tool()
def remove_favorite(title: str | None = None, tmdb_id: int | None = None, media_type: str | None = None) -> str:
    """Remove a title from the user's favorites. Pass the title as the user said it.

    Args:
        title: Title to remove.
        tmdb_id: Exact TMDB id, if known.
        media_type: "movie" or "tv", if both exist with the same name.
    """
    row, problem = _find_saved(library.list_favorites(), title, tmdb_id, media_type)
    if problem:
        return problem
    removed = library.remove_favorite(row["tmdb_id"], row["media_type"])
    return _dumps({"success": removed, "item": _brief(row)})


# --------------------------------------------------------------------------- #
# Library: watch list
# --------------------------------------------------------------------------- #


@mcp.tool()
async def set_watching(
    title: str | None = None,
    tmdb_id: int | None = None,
    media_type: str | None = None,
    year: int | None = None,
    status: str = "watching",
) -> str:
    """Put a movie or TV show on the user's watch list, or change its status.

    Status values: "watching" (default, in progress), "plan" (want to watch later),
    "watched" (finished). Calling it again for the same title just updates the status.

    Args:
        title: Title the user named.
        tmdb_id: Exact TMDB id (only if already known).
        media_type: "movie" or "tv".
        year: Release year, to pick between remakes.
        status: "watching", "plan" or "watched".
    """
    if problem := _bad_status(status):
        return problem
    item, problem = await _lookup(title, tmdb_id, media_type, year)
    if problem:
        return problem
    added = library.set_watching(TitleRef.from_mapping(item), status)
    return _dumps({"success": True, "already_existed": not added, "status": status, "item": _brief(item)})


@mcp.tool()
def list_watching(status: str | None = None) -> str:
    """List titles on the user's watch list, optionally filtered by status.

    Args:
        status: Optional filter: "watching", "plan" or "watched".
    """
    if problem := _bad_status(status):
        return problem
    items = library.list_watching(status)
    return _dumps({"count": len(items), "watching": [_saved_brief(i) for i in items]})


@mcp.tool()
def remove_watching(title: str | None = None, tmdb_id: int | None = None, media_type: str | None = None) -> str:
    """Remove a title from the user's watch list.

    Args:
        title: Title to remove.
        tmdb_id: Exact TMDB id, if known.
        media_type: "movie" or "tv", if both exist with the same name.
    """
    row, problem = _find_saved(library.list_watching(), title, tmdb_id, media_type)
    if problem:
        return problem
    removed = library.remove_watching(row["tmdb_id"], row["media_type"])
    return _dumps({"success": removed, "item": _brief(row)})


# --------------------------------------------------------------------------- #
# Library: reviews
# --------------------------------------------------------------------------- #


@mcp.tool()
async def save_review(  # noqa: PLR0913, PLR0917 - a tool's signature is its schema for the model
    rating: float,
    comment: str = "",
    title: str | None = None,
    tmdb_id: int | None = None,
    show_id: int | None = None,
    media_type: str | None = None,
    year: int | None = None,
) -> str:
    """Save or update the user's review of a movie or TV show (rating 0 to 10).

    Pass the title the user named; there is one review per title and saving again
    replaces it. If the user gives a score out of 5, convert it to a 0-10 rating first.

    Args:
        rating: Number from 0 to 10.
        comment: The user's own words about it (optional).
        title: Title being reviewed.
        tmdb_id: Exact TMDB id, if known (show_id is accepted as the same thing).
        show_id: Alias for tmdb_id.
        media_type: "movie" or "tv".
        year: Release year, to pick between remakes.
    """
    if not 0 <= rating <= 10:  # noqa: PLR2004 - rating scale
        return _err("Rating must be between 0 and 10.")
    item, problem = await _lookup(title, tmdb_id or show_id, media_type, year)
    if problem:
        return problem
    library.save_review(TitleRef.from_mapping(item), rating, comment)
    return _dumps({"success": True, "item": _brief(item), "rating": rating})


@mcp.tool()
def get_review(title: str | None = None, tmdb_id: int | None = None, media_type: str | None = None) -> str:
    """Get the user's review for one title (null if they have not reviewed it).

    Args:
        title: Title to look up in the user's reviews.
        tmdb_id: Exact TMDB id, if known.
        media_type: "movie" or "tv".
    """
    row, problem = _find_saved(library.list_reviews(), title, tmdb_id, media_type)
    if problem:
        return problem if "needs_clarification" in problem else _dumps({"review": None})
    return _dumps({"review": _saved_brief(row)})


@mcp.tool()
def list_reviews() -> str:
    """List all reviews the user has written."""
    reviews = library.list_reviews()
    return _dumps({"count": len(reviews), "reviews": [_saved_brief(r) for r in reviews]})


# --------------------------------------------------------------------------- #
# Personalization
# --------------------------------------------------------------------------- #


def _profile() -> dict:
    return {
        "favorites": [_saved_brief(f) for f in library.list_favorites()],
        "watching": [_saved_brief(w) for w in library.list_watching()],
        "reviews": [_saved_brief(r) for r in library.list_reviews()],
    }


@mcp.tool()
def get_user_profile() -> str:
    """The user's full saved state: favorites, watch list and reviews.

    Call this when you need context to personalize recommendations.
    """
    return _dumps(_profile())


@mcp.tool()
async def recommend_for_user(limit: int = 8) -> str:
    """Build a personalized list from what the user rated, favorited and is watching.

    Seeds are the user's best-rated reviews, then favorites, then watch-list titles.
    TMDB recommendations for each seed are merged, ranked, and anything the user
    already saved is removed. Falls back to trending titles if they have no data yet.

    Args:
        limit: Maximum number of results (1-20).
    """
    try:
        return _dumps(await recommend.personalized(limit))
    except tmdb.TMDBError as e:
        return _err(str(e))


# --------------------------------------------------------------------------- #
# Resources and prompt
# --------------------------------------------------------------------------- #


@mcp.resource("movies://user-profile")
def user_profile_resource() -> str:
    """The user's saved state (favorites / watching / reviews) as JSON."""
    return _dumps(_profile())


@mcp.resource("movies://genres")
async def genres_resource() -> str:
    """TMDB genres for movies and TV as JSON."""
    try:
        return _dumps({"movie": await tmdb.get_genres("movie"), "tv": await tmdb.get_genres("tv")})
    except tmdb.TMDBError as e:
        return _err(str(e))


@mcp.prompt()
def recommend_prompt(taste: str) -> str:
    """Reusable prompt template for a recommendation turn."""
    return (
        f"The user describes their taste as: {taste}\n\n"
        "Workflow:\n"
        "1. Call `get_user_profile` to see what they already like.\n"
        "2. Call `recommend_for_user` for a personalized baseline, or `discover` / `search_shows` "
        "for what they asked.\n"
        "3. Enrich the top picks with `get_details`.\n\n"
        "Return at most 5 titles. For each: title, year, TMDB rating, a one-sentence "
        "spoiler-free pitch, and why it fits their taste."
    )
