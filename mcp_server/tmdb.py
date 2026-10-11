"""
tmdb.py - Async wrapper around The Movie Database (TMDB) API for movies and TV shows.

Reads TMDB_API_KEY (v3 key or v4 token) or TMDB_API_Read_Access_Token from the project
root .env file. One pooled HTTP client is reused for every request and short-lived
caches avoid repeating identical calls.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

TMDB_API_KEY = os.getenv("TMDB_API_Read_Access_Token") or os.getenv("TMDB_API_KEY")
TMDB_REGION = os.getenv("TMDB_REGION", "US")
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p"

MAX_ATTEMPTS = 3
RETRY_STATUSES = {429, 500, 502, 503, 504}
OVERVIEW_MAX_CHARS = 160
CACHE_TTL = 300          # seconds, for search / list endpoints
DETAILS_TTL = 3600       # seconds, for details and genres
CACHE_MAX_ENTRIES = 512

# httpx logs every request URL at INFO level. With a v3 key the URL contains
# ?api_key=..., so keep that logger quiet or the key ends up in your logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

if not TMDB_API_KEY:
    # stderr only: in stdio mode, stdout is the MCP protocol channel.
    print("WARNING: TMDB_API_KEY is not set; TMDB tools will return an error.", file=sys.stderr)


class TMDBError(Exception):
    """Raised when a TMDB request fails (bad response, network error, missing key)."""


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #

_client: httpx.AsyncClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None
_cache: dict[tuple, tuple[float, Any]] = {}


def _auth() -> tuple[dict[str, str], dict[str, str]]:
    """Return (headers, params) for the configured credential."""
    if not TMDB_API_KEY:
        raise TMDBError("TMDB_API_KEY is not set. Add it to the .env file and restart the server.")
    if TMDB_API_KEY.startswith("eyJ"):
        # v4 read-access token (a JWT): send as a header, keeping it out of the URL.
        return {"Authorization": f"Bearer {TMDB_API_KEY}"}, {}
    return {}, {"api_key": TMDB_API_KEY}


def _get_client() -> httpx.AsyncClient:
    """One pooled client per event loop (a client is tied to the loop it was created on)."""
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client_loop is not loop or _client.is_closed:
        headers, _ = _auth()
        _client = httpx.AsyncClient(
            base_url=TMDB_BASE_URL,
            headers=headers,
            timeout=httpx.Timeout(10.0, connect=5.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        _client_loop = loop
    return _client


async def close() -> None:
    """Close the shared client (call on shutdown)."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def clear_cache() -> None:
    _cache.clear()


def _error_message(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("status_message") or resp.text[:200]
    except ValueError:
        detail = resp.text[:200]

    if resp.status_code == 401:
        return "TMDB rejected the API key (401). Check TMDB_API_KEY."
    if resp.status_code == 404:
        return f"Not found on TMDB (404): {detail} Verify the id and media_type before retrying."
    return f"TMDB error {resp.status_code}: {detail}"


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        try:
            return min(float(retry_after), 5.0)
        except ValueError:
            pass
    return 0.5 * 2 ** (attempt - 1)


async def _get(endpoint: str, params: dict[str, Any] | None = None, ttl: float = CACHE_TTL) -> Any:
    key = (endpoint, tuple(sorted((params or {}).items())))
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]

    _, auth_params = _auth()
    query = {**(params or {}), **auth_params}
    client = _get_client()

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = await client.get(endpoint, params=query)
        except httpx.HTTPError as e:
            if attempt == MAX_ATTEMPTS:
                # Type name only: the full message can contain the request URL.
                raise TMDBError(f"Could not reach TMDB ({type(e).__name__}).") from e
            await asyncio.sleep(0.5 * 2 ** (attempt - 1))
            continue

        if resp.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
            await asyncio.sleep(_retry_delay(resp, attempt))
            continue

        if resp.status_code != 200:
            raise TMDBError(_error_message(resp))

        try:
            data = resp.json()
        except ValueError as e:
            raise TMDBError("TMDB returned a response that is not valid JSON.") from e

        if len(_cache) >= CACHE_MAX_ENTRIES:
            _cache.pop(next(iter(_cache)))
        _cache[key] = (time.monotonic() + ttl, data)
        return data

    raise TMDBError("TMDB request failed.")


# --------------------------------------------------------------------------- #
# Normalizers
# --------------------------------------------------------------------------- #

# TMDB genre ids are stable, so list results map ids to names without an extra request.
_MOVIE_GENRES = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime", 99: "Documentary",
    18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History", 27: "Horror", 10402: "Music",
    9648: "Mystery", 10749: "Romance", 878: "Science Fiction", 10770: "TV Movie", 53: "Thriller",
    10752: "War", 37: "Western",
}
_TV_GENRES = {
    10759: "Action & Adventure", 16: "Animation", 35: "Comedy", 80: "Crime", 99: "Documentary",
    18: "Drama", 10751: "Family", 10762: "Kids", 9648: "Mystery", 10763: "News", 10764: "Reality",
    10765: "Sci-Fi & Fantasy", 10766: "Soap", 10767: "Talk", 10768: "War & Politics", 37: "Western",
}


def image_url(path: str | None, size: str = "w342") -> str | None:
    return f"{TMDB_IMAGE_BASE}/{size}{path}" if path else None


def _short(text: str | None, limit: int = OVERVIEW_MAX_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _kind(item: dict, media_type: str | None) -> str:
    """Decide 'movie' or 'tv' for a raw TMDB item."""
    if media_type in ("movie", "tv"):
        return media_type
    if item.get("media_type") in ("movie", "tv"):
        return item["media_type"]
    return "tv" if ("first_air_date" in item or "name" in item) and "title" not in item else "movie"


def _normalize(item: dict, media_type: str | None = None) -> dict:
    """Compact dict for list results: cheap for the agent to read, enough for a card."""
    kind = _kind(item, media_type)
    date = item.get("release_date") or item.get("first_air_date") or None
    genre_map = _TV_GENRES if kind == "tv" else _MOVIE_GENRES
    genres = [genre_map[g] for g in item.get("genre_ids", []) if g in genre_map]
    if not genres and item.get("genres"):
        genres = [g["name"] for g in item["genres"]]
    return {
        "tmdb_id": item.get("id"),
        "media_type": kind,
        "title": item.get("title") or item.get("name"),
        "year": date[:4] if date else None,
        "rating": round(item.get("vote_average") or 0.0, 1),
        "genres": genres[:3],
        "overview": _short(item.get("overview")),
        "poster_path": item.get("poster_path"),
        "popularity": round(item.get("popularity") or 0.0, 1),
    }


def card(item: dict) -> dict:
    """A list result as the model and the UI see it: no ranking fields, no empty values."""
    return {k: v for k, v in item.items() if k != "popularity" and v not in (None, "", [])}


def _results(data: dict, media_type: str | None = None) -> list[dict]:
    out = []
    for raw in data.get("results", []):
        if raw.get("media_type") == "person":
            continue
        out.append(_normalize(raw, media_type))
    return out


# --------------------------------------------------------------------------- #
# Search, details, lists
# --------------------------------------------------------------------------- #

def _check_type(media_type: str) -> str:
    if media_type not in ("movie", "tv"):
        raise TMDBError("media_type must be 'movie' or 'tv'.")
    return media_type


async def search(query: str, media_type: str = "all", year: int | None = None, page: int = 1) -> list[dict]:
    """Search movies and TV shows. media_type: 'all', 'movie' or 'tv'."""
    if media_type == "all":
        data = await _get("/search/multi", {"query": query, "page": page, "include_adult": False})
        return [r for r in _results(data) if r["media_type"] in ("movie", "tv")]
    _check_type(media_type)
    params: dict[str, Any] = {"query": query, "page": page, "include_adult": False}
    if year:
        params["year" if media_type == "movie" else "first_air_date_year"] = year
    data = await _get(f"/search/{media_type}", params)
    return _results(data, media_type)


async def search_movies(query: str, page: int = 1) -> list[dict]:
    return await search(query, "movie", page=page)


def _trailer(videos: dict) -> str | None:
    items = [v for v in videos.get("results", []) if v.get("site") == "YouTube"]
    items.sort(key=lambda v: (v.get("type") != "Trailer", not v.get("official", False)))
    return f"https://www.youtube.com/watch?v={items[0]['key']}" if items else None


def _providers(block: dict, region: str) -> dict:
    entry = (block.get("results") or {}).get(region) or {}
    return {
        "region": region,
        "link": entry.get("link"),
        "stream": [p["provider_name"] for p in entry.get("flatrate", [])],
        "rent": [p["provider_name"] for p in entry.get("rent", [])],
        "buy": [p["provider_name"] for p in entry.get("buy", [])],
    }


async def get_details(tmdb_id: int, media_type: str = "movie") -> dict:
    """Full details: cast, director or creators, runtime, keywords, trailer, where to watch."""
    _check_type(media_type)
    data = await _get(
        f"/{media_type}/{tmdb_id}",
        {"append_to_response": "credits,keywords,videos,watch/providers"},
        ttl=DETAILS_TTL,
    )
    result = _normalize(data, media_type)
    result["overview"] = data.get("overview") or ""
    result["backdrop_path"] = data.get("backdrop_path")
    result["tagline"] = data.get("tagline") or ""
    result["status"] = data.get("status")
    result["genres"] = [g["name"] for g in data.get("genres", [])]
    result["cast"] = [c["name"] for c in data.get("credits", {}).get("cast", [])[:10]]
    kw = data.get("keywords", {})
    result["keywords"] = [k["name"] for k in (kw.get("keywords") or kw.get("results") or [])[:12]]
    result["trailer_url"] = _trailer(data.get("videos", {}))
    result["where_to_watch"] = _providers(data.get("watch/providers", {}), TMDB_REGION)

    if media_type == "movie":
        result["runtime"] = data.get("runtime")
        result["director"] = next(
            (c["name"] for c in data.get("credits", {}).get("crew", []) if c.get("job") == "Director"),
            None,
        )
    else:
        runs = data.get("episode_run_time") or []
        result["runtime"] = runs[0] if runs else None
        result["created_by"] = [c["name"] for c in data.get("created_by", [])]
        result["seasons"] = data.get("number_of_seasons")
        result["episodes"] = data.get("number_of_episodes")
    return result


async def get_similar(tmdb_id: int, media_type: str = "movie", page: int = 1) -> list[dict]:
    _check_type(media_type)
    data = await _get(f"/{media_type}/{tmdb_id}/similar", {"page": page})
    return _results(data, media_type)


async def get_recommended(tmdb_id: int, media_type: str = "movie", page: int = 1) -> list[dict]:
    _check_type(media_type)
    data = await _get(f"/{media_type}/{tmdb_id}/recommendations", {"page": page})
    return _results(data, media_type)


async def get_trending(media_type: str = "all", window: str = "week", page: int = 1) -> list[dict]:
    if media_type not in ("all", "movie", "tv"):
        media_type = "all"
    if window not in ("day", "week"):
        window = "week"
    data = await _get(f"/trending/{media_type}/{window}", {"page": page})
    out = _results(data, None if media_type == "all" else media_type)
    return [r for r in out if r["media_type"] in ("movie", "tv")]


async def discover(
    media_type: str = "movie",
    genre_id: int | None = None,
    year: int | None = None,
    min_rating: float | None = None,
    sort_by: str = "popularity.desc",
    page: int = 1,
) -> list[dict]:
    _check_type(media_type)
    params: dict[str, Any] = {"sort_by": sort_by, "page": page}
    if genre_id is not None:
        params["with_genres"] = genre_id
    if year is not None:
        params["primary_release_year" if media_type == "movie" else "first_air_date_year"] = year
    if min_rating is not None:
        params["vote_average.gte"] = min_rating
        params["vote_count.gte"] = 100
    data = await _get(f"/discover/{media_type}", params)
    return _results(data, media_type)


# Older names kept so existing callers keep working.
async def get_movie_details(tmdb_id: int) -> dict:
    return await get_details(tmdb_id, "movie")


async def get_similar_movies(tmdb_id: int, page: int = 1) -> list[dict]:
    return await get_similar(tmdb_id, "movie", page)


async def get_recommended_movies(tmdb_id: int, page: int = 1) -> list[dict]:
    return await get_recommended(tmdb_id, "movie", page)


async def get_trending_movies(window: str = "week", page: int = 1) -> list[dict]:
    return await get_trending("movie", window, page)


async def discover_movies(
    genre_id: int | None = None,
    year: int | None = None,
    min_rating: float | None = None,
    sort_by: str = "popularity.desc",
    page: int = 1,
) -> list[dict]:
    return await discover("movie", genre_id, year, min_rating, sort_by, page)


# --------------------------------------------------------------------------- #
# Genres
# --------------------------------------------------------------------------- #

_GENRE_ALIASES = {
    "sci-fi": "science fiction",
    "scifi": "science fiction",
    "sci fi": "science fiction",
    "animated": "animation",
    "doc": "documentary",
}


async def get_genres(media_type: str = "movie") -> list[dict]:
    _check_type(media_type)
    data = await _get(f"/genre/{media_type}/list", ttl=DETAILS_TTL)
    return list(data.get("genres", []))


async def resolve_genre_id(name: str, media_type: str = "movie") -> int | None:
    """Map a genre name like 'sci-fi' or 'Science Fiction' to a TMDB genre id."""
    wanted = name.strip().lower()
    wanted = _GENRE_ALIASES.get(wanted, wanted)
    for genre in await get_genres(media_type):
        if genre["name"].lower() == wanted:
            return genre["id"]
    return None


# --------------------------------------------------------------------------- #
# Title resolution
# --------------------------------------------------------------------------- #

def _norm_title(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


async def resolve_title(
    title: str,
    media_type: str | None = None,
    year: int | str | None = None,
) -> dict:
    """Turn a free-text title into one confident match, or a short list of candidates.

    Returns {"match": item} when exactly one result is clearly the right one, otherwise
    {"match": None, "candidates": [...]} (empty candidates means nothing was found).
    """
    results = await search(title, media_type if media_type in ("movie", "tv") else "all")
    if year:
        same_year = [r for r in results if r["year"] == str(year)]
        results = same_year or results
    if not results:
        return {"match": None, "candidates": []}

    wanted = _norm_title(title)
    exact = [r for r in results if _norm_title(r["title"]) == wanted]

    if len(exact) == 1:
        return {"match": exact[0], "candidates": []}
    if len(exact) > 1:
        exact.sort(key=lambda r: -r["popularity"])
        # Several titles share the name (a movie and a show, or remakes): only auto-pick a
        # clear favourite.
        if exact[0]["popularity"] >= 3 * max(exact[1]["popularity"], 0.1):
            return {"match": exact[0], "candidates": []}
        return {"match": None, "candidates": exact[:5]}
    if len(results) == 1:
        return {"match": results[0], "candidates": []}
    return {"match": None, "candidates": results[:5]}
