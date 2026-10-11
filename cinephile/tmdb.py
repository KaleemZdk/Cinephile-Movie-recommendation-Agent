"""Async client for The Movie Database (TMDB) API, for movies and TV shows.

One pooled HTTP connection is reused for every request, and identical requests are
answered from a small time-limited cache.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from typing import Any

import httpx

from cinephile.config import get_settings

log = logging.getLogger(__name__)

TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p"
TMDB_CREDENTIAL = get_settings().tmdb_credential  # tests replace this
TMDB_REGION = get_settings().tmdb_region

MAX_ATTEMPTS = 3
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
OVERVIEW_MAX_CHARS = 160
LIST_TTL = 300  # seconds, for search and list endpoints
DETAILS_TTL = 3600  # seconds, for details and genres
CACHE_MAX_ENTRIES = 512

MediaType = str  # "movie" or "tv"


class TMDBError(Exception):
    """A TMDB request failed (bad response, network error, missing key)."""


# --------------------------------------------------------------------------- #
# HTTP transport
# --------------------------------------------------------------------------- #


def _auth() -> tuple[dict[str, str], dict[str, str]]:
    """(headers, query params) for the configured credential."""
    if not TMDB_CREDENTIAL:
        raise TMDBError("TMDB_API_KEY is not set. Add it to the .env file and restart the server.")
    if TMDB_CREDENTIAL.startswith("eyJ"):  # v4 read-access token (a JWT)
        return {"Authorization": f"Bearer {TMDB_CREDENTIAL}"}, {}
    return {}, {"api_key": TMDB_CREDENTIAL}


def _error_message(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("status_message") or resp.text[:200]
    except ValueError:
        detail = resp.text[:200]
    if resp.status_code == httpx.codes.UNAUTHORIZED:
        return "TMDB rejected the API key (401). Check TMDB_API_KEY."
    if resp.status_code == httpx.codes.NOT_FOUND:
        return f"Not found on TMDB (404): {detail} Verify the id and media_type before retrying."
    return f"TMDB error {resp.status_code}: {detail}"


def _retry_delay(resp: httpx.Response | None, attempt: int) -> float:
    retry_after = resp.headers.get("Retry-After") if resp is not None else None
    if retry_after:
        try:
            return min(float(retry_after), 5.0)
        except ValueError:
            pass
    return 0.5 * 2 ** (attempt - 1)


class _Transport:
    """A pooled HTTP client (one per event loop) and an LRU cache with expiry."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cache: OrderedDict[tuple, tuple[float, Any]] = OrderedDict()

    def client(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if self._client is None or self._loop is not loop or self._client.is_closed:
            headers, _ = _auth()
            self._client = httpx.AsyncClient(
                base_url=TMDB_BASE_URL,
                headers=headers,
                timeout=httpx.Timeout(10.0, connect=5.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
            self._loop = loop
        return self._client

    def _cached(self, key: tuple) -> Any | None:
        hit = self._cache.get(key)
        if not hit:
            return None
        expires, data = hit
        if expires <= time.monotonic():
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return data

    def _store(self, key: tuple, data: Any, ttl: float) -> None:
        self._cache[key] = (time.monotonic() + ttl, data)
        self._cache.move_to_end(key)
        while len(self._cache) > CACHE_MAX_ENTRIES:
            self._cache.popitem(last=False)

    async def get(self, endpoint: str, params: dict[str, Any] | None, ttl: float) -> Any:
        key = (endpoint, tuple(sorted((params or {}).items())))
        cached = self._cached(key)
        if cached is not None:
            return cached

        _, auth_params = _auth()
        query = {**(params or {}), **auth_params}
        client = self.client()
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = await client.get(endpoint, params=query)
            except httpx.HTTPError as e:
                if attempt == MAX_ATTEMPTS:
                    # Type name only: the message can contain the request URL (and a v3 key).
                    raise TMDBError(f"Could not reach TMDB ({type(e).__name__}).") from e
                await asyncio.sleep(_retry_delay(None, attempt))
                continue
            if resp.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                await asyncio.sleep(_retry_delay(resp, attempt))
                continue
            if resp.status_code != httpx.codes.OK:
                raise TMDBError(_error_message(resp))
            try:
                data = resp.json()
            except ValueError as e:
                raise TMDBError("TMDB returned a response that is not valid JSON.") from e
            self._store(key, data, ttl)
            return data
        raise TMDBError("TMDB request failed.")

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    def clear_cache(self) -> None:
        self._cache.clear()


_transport = _Transport()


async def _get(endpoint: str, params: dict[str, Any] | None = None, ttl: float = LIST_TTL) -> Any:
    return await _transport.get(endpoint, params, ttl)


async def close() -> None:
    """Close the pooled client (call on shutdown)."""
    await _transport.close()


def clear_cache() -> None:
    _transport.clear_cache()


# --------------------------------------------------------------------------- #
# Shaping results
# --------------------------------------------------------------------------- #

# TMDB genre ids are stable, so list results map ids to names without an extra request.
_MOVIE_GENRES = {
    28: "Action",
    12: "Adventure",
    16: "Animation",
    35: "Comedy",
    80: "Crime",
    99: "Documentary",
    18: "Drama",
    10751: "Family",
    14: "Fantasy",
    36: "History",
    27: "Horror",
    10402: "Music",
    9648: "Mystery",
    10749: "Romance",
    878: "Science Fiction",
    10770: "TV Movie",
    53: "Thriller",
    10752: "War",
    37: "Western",
}
_TV_GENRES = {
    10759: "Action & Adventure",
    16: "Animation",
    35: "Comedy",
    80: "Crime",
    99: "Documentary",
    18: "Drama",
    10751: "Family",
    10762: "Kids",
    9648: "Mystery",
    10763: "News",
    10764: "Reality",
    10765: "Sci-Fi & Fantasy",
    10766: "Soap",
    10767: "Talk",
    10768: "War & Politics",
    37: "Western",
}


def image_url(path: str | None, size: str = "w342") -> str | None:
    return f"{TMDB_IMAGE_BASE}/{size}{path}" if path else None


def normalize_title(text: str | None) -> str:
    """Lowercase, punctuation-free form of a title, for comparing titles."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def same_title(a: str | None, b: str | None) -> bool:
    return normalize_title(a) == normalize_title(b)


def shorten(text: str | None, limit: int = OVERVIEW_MAX_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _kind(item: dict, media_type: MediaType | None) -> MediaType:
    if media_type in ("movie", "tv"):
        return media_type
    if item.get("media_type") in ("movie", "tv"):
        return item["media_type"]
    looks_like_tv = ("first_air_date" in item or "name" in item) and "title" not in item
    return "tv" if looks_like_tv else "movie"


def normalize(item: dict, media_type: MediaType | None = None) -> dict:
    """Compact dict for a list result: cheap for the model to read, enough for a card."""
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
        "overview": shorten(item.get("overview")),
        "poster_path": item.get("poster_path"),
        "popularity": round(item.get("popularity") or 0.0, 1),
    }


def card(item: dict) -> dict:
    """A list result as the model and the UI see it: no ranking fields, no empty values."""
    return {k: v for k, v in item.items() if k != "popularity" and v not in (None, "", [])}


def _results(data: dict, media_type: MediaType | None = None) -> list[dict]:
    return [normalize(raw, media_type) for raw in data.get("results", []) if raw.get("media_type") != "person"]


def _check_type(media_type: str) -> MediaType:
    if media_type not in ("movie", "tv"):
        raise TMDBError("media_type must be 'movie' or 'tv'.")
    return media_type


# --------------------------------------------------------------------------- #
# Search, details, lists
# --------------------------------------------------------------------------- #


async def search(query: str, media_type: str = "all", year: int | None = None, page: int = 1) -> list[dict]:
    """Search movies and TV shows. media_type: 'all', 'movie' or 'tv'."""
    if media_type == "all":
        data = await _get("/search/multi", {"query": query, "page": page, "include_adult": False})
        return [r for r in _results(data) if r["media_type"] in ("movie", "tv")]
    _check_type(media_type)
    params: dict[str, Any] = {"query": query, "page": page, "include_adult": False}
    if year:
        params["year" if media_type == "movie" else "first_air_date_year"] = year
    return _results(await _get(f"/search/{media_type}", params), media_type)


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


async def get_details(tmdb_id: int, media_type: MediaType = "movie") -> dict:
    """Full details: cast, director or creators, runtime, keywords, trailer, where to watch."""
    _check_type(media_type)
    data = await _get(
        f"/{media_type}/{tmdb_id}",
        {"append_to_response": "credits,keywords,videos,watch/providers"},
        ttl=DETAILS_TTL,
    )
    credits = data.get("credits", {})
    keywords = data.get("keywords", {})
    result = normalize(data, media_type)
    result.update(
        {
            "overview": data.get("overview") or "",
            "backdrop_path": data.get("backdrop_path"),
            "tagline": data.get("tagline") or "",
            "status": data.get("status"),
            "genres": [g["name"] for g in data.get("genres", [])],
            "cast": [c["name"] for c in credits.get("cast", [])[:10]],
            "keywords": [k["name"] for k in (keywords.get("keywords") or keywords.get("results") or [])[:12]],
            "trailer_url": _trailer(data.get("videos", {})),
            "where_to_watch": _providers(data.get("watch/providers", {}), TMDB_REGION),
        }
    )
    if media_type == "movie":
        result["runtime"] = data.get("runtime")
        result["director"] = next((c["name"] for c in credits.get("crew", []) if c.get("job") == "Director"), None)
    else:
        runs = data.get("episode_run_time") or []
        result["runtime"] = runs[0] if runs else None
        result["created_by"] = [c["name"] for c in data.get("created_by", [])]
        result["seasons"] = data.get("number_of_seasons")
        result["episodes"] = data.get("number_of_episodes")
    return result


async def get_similar(tmdb_id: int, media_type: MediaType = "movie", page: int = 1) -> list[dict]:
    _check_type(media_type)
    return _results(await _get(f"/{media_type}/{tmdb_id}/similar", {"page": page}), media_type)


async def get_recommended(tmdb_id: int, media_type: MediaType = "movie", page: int = 1) -> list[dict]:
    _check_type(media_type)
    return _results(await _get(f"/{media_type}/{tmdb_id}/recommendations", {"page": page}), media_type)


async def get_related(tmdb_id: int, media_type: MediaType = "movie", limit: int = 12) -> list[dict]:
    """Recommendations first (better matches), topped up from the similar-titles list."""
    results = await get_recommended(tmdb_id, media_type)
    if len(results) < limit:
        seen = {r["tmdb_id"] for r in results}
        results += [r for r in await get_similar(tmdb_id, media_type) if r["tmdb_id"] not in seen]
    return results[:limit]


async def get_trending(media_type: str = "all", window: str = "week", page: int = 1) -> list[dict]:
    if media_type not in ("all", "movie", "tv"):
        media_type = "all"
    if window not in ("day", "week"):
        window = "week"
    data = await _get(f"/trending/{media_type}/{window}", {"page": page})
    results = _results(data, None if media_type == "all" else media_type)
    return [r for r in results if r["media_type"] in ("movie", "tv")]


async def discover(  # noqa: PLR0913 - one argument per TMDB filter
    media_type: MediaType = "movie",
    *,
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
    return _results(await _get(f"/discover/{media_type}", params), media_type)


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


async def get_genres(media_type: MediaType = "movie") -> list[dict]:
    _check_type(media_type)
    data = await _get(f"/genre/{media_type}/list", ttl=DETAILS_TTL)
    return list(data.get("genres", []))


async def resolve_genre_id(name: str, media_type: MediaType = "movie") -> int | None:
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

CLEAR_FAVOURITE_RATIO = 3  # a same-named title is auto-picked only when this much more popular


async def resolve_title(title: str, media_type: MediaType | None = None, year: int | str | None = None) -> dict:
    """Turn a free-text title into one confident match, or a short list of candidates.

    Returns {"match": item, "candidates": []} when one result is clearly right, otherwise
    {"match": None, "candidates": [...]} (no candidates means nothing was found).
    """
    results = await search(title, media_type if media_type in ("movie", "tv") else "all")
    if year:
        results = [r for r in results if r["year"] == str(year)] or results
    if not results:
        return {"match": None, "candidates": []}

    exact = [r for r in results if same_title(r["title"], title)]
    if len(exact) == 1:
        return {"match": exact[0], "candidates": []}
    if exact:
        exact.sort(key=lambda r: -r["popularity"])
        if exact[0]["popularity"] >= CLEAR_FAVOURITE_RATIO * max(exact[1]["popularity"], 0.1):
            return {"match": exact[0], "candidates": []}
        return {"match": None, "candidates": exact[:5]}
    if len(results) == 1:
        return {"match": results[0], "candidates": []}
    return {"match": None, "candidates": results[:5]}
