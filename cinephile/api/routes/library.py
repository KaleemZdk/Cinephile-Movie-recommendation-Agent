"""The current visitor's favorites, watch list and reviews.

Plain CRUD over SQLite; no model call is involved. A title is addressed as
/{media_type}/{tmdb_id} because TMDB reuses ids between movies and shows.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, status

from cinephile import tmdb
from cinephile.api.deps import UserDep
from cinephile.api.presenters import saved_item, ui_item
from cinephile.api.schemas import MediaType, ReviewIn, StatusIn, TitleIn, WatchIn, WatchStatus
from cinephile.storage import library
from cinephile.storage.library import TitleRef

router = APIRouter(tags=["library"])

TmdbId = Path(gt=0)
BACKFILL_BATCH = 20


async def _complete(body: TitleIn) -> TitleRef:
    """The title to save, with a missing title, year or poster filled in from TMDB."""
    ref = TitleRef(body.id, body.media_type, body.title.strip(), body.year, body.poster_path)
    if ref.title and ref.poster_path:
        return ref
    try:
        details = await tmdb.get_details(ref.tmdb_id, ref.media_type)
    except tmdb.TMDBError as exc:
        if not ref.title:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "That title was not found.") from exc
        return ref
    if ref.title and not tmdb.same_title(ref.title, details["title"]):
        return ref  # the id belongs to something else; keep what the client sent
    return TitleRef(
        ref.tmdb_id,
        ref.media_type,
        ref.title or details["title"],
        ref.year or details["year"],
        ref.poster_path or details["poster_path"],
    )


async def _backfill(table: str, user_id: str, rows: list[dict]) -> None:
    """Older saved rows have no poster or year; fetch them once and store them."""
    missing = [row for row in rows if not row.get("poster_path")][:BACKFILL_BATCH]
    if not missing:
        return

    async def fetch(row: dict) -> dict | None:
        try:
            return await tmdb.get_details(row["tmdb_id"], row["media_type"])
        except tmdb.TMDBError:
            return None

    for row, details in zip(missing, await asyncio.gather(*(fetch(r) for r in missing)), strict=True):
        if details and tmdb.same_title(row["title"], details["title"]):
            row["poster_path"] = details["poster_path"]
            row["year"] = row.get("year") or details["year"]
            library.fill_missing_metadata(table, user_id, TitleRef.from_mapping(row))


def _saved_response(ref: TitleRef, added: bool, **extra: str) -> dict:
    return {"success": True, "already_existed": not added, "item": ui_item(asdict(ref)), **extra}


# ---------------------------------------------------------------- favorites


@router.get("/favorites")
async def list_favorites(user_id: UserDep) -> list[dict]:
    rows = library.list_favorites(user_id)
    await _backfill("favorites", user_id, rows)
    return [saved_item(r) for r in rows]


@router.post("/favorites")
async def add_favorite(body: TitleIn, user_id: UserDep) -> dict:
    ref = await _complete(body)
    return _saved_response(ref, library.add_favorite(user_id, ref))


@router.delete("/favorites/{media_type}/{tmdb_id}")
def remove_favorite(user_id: UserDep, media_type: MediaType, tmdb_id: int = TmdbId) -> dict:
    return {"success": library.remove_favorite(user_id, tmdb_id, media_type)}


# ----------------------------------------------------------------- watching


@router.get("/watching")
async def list_watching(
    user_id: UserDep, status_filter: Annotated[WatchStatus | None, Query(alias="status")] = None
) -> list[dict]:
    rows = library.list_watching(user_id, status_filter)
    await _backfill("watching", user_id, rows)
    return [saved_item(r) for r in rows]


@router.post("/watching")
async def add_watching(body: WatchIn, user_id: UserDep) -> dict:
    ref = await _complete(body)
    return _saved_response(ref, library.set_watching(user_id, ref, body.status), status=body.status)


@router.patch("/watching/{media_type}/{tmdb_id}")
def change_status(body: StatusIn, user_id: UserDep, media_type: MediaType, tmdb_id: int = TmdbId) -> dict:
    if not library.update_watching_status(user_id, tmdb_id, body.status, media_type):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That title is not on your watch list.")
    return {"success": True}


@router.delete("/watching/{media_type}/{tmdb_id}")
def remove_watching(user_id: UserDep, media_type: MediaType, tmdb_id: int = TmdbId) -> dict:
    return {"success": library.remove_watching(user_id, tmdb_id, media_type)}


# ------------------------------------------------------------------ reviews


@router.get("/reviews")
async def list_reviews(user_id: UserDep) -> list[dict]:
    rows = library.list_reviews(user_id)
    await _backfill("reviews", user_id, rows)
    return [saved_item(r) for r in rows]


@router.get("/reviews/{media_type}/{tmdb_id}")
def get_review(user_id: UserDep, media_type: MediaType, tmdb_id: int = TmdbId) -> dict:
    review = library.get_review(user_id, tmdb_id, media_type)
    if not review:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No review for this title yet.")
    return saved_item(review)


@router.post("/reviews")
async def save_review(body: ReviewIn, user_id: UserDep) -> dict:
    ref = await _complete(body)
    library.save_review(user_id, ref, body.rating, body.comment)
    return {"success": True}


@router.delete("/reviews/{media_type}/{tmdb_id}")
def remove_review(user_id: UserDep, media_type: MediaType, tmdb_id: int = TmdbId) -> dict:
    return {"success": library.remove_review(user_id, tmdb_id, media_type)}


# ------------------------------------------------------------------ summary


@router.get("/library/ids")
def library_ids(user_id: UserDep) -> dict:
    """Compact lookup the UI uses to mark cards as saved, keyed 'type:id'."""

    def key(row: dict) -> str:
        return f"{row['media_type']}:{row['tmdb_id']}"

    return {
        "favorites": [key(r) for r in library.list_favorites(user_id)],
        "watching": {key(r): r["status"] for r in library.list_watching(user_id)},
        "reviews": {key(r): r["rating"] for r in library.list_reviews(user_id)},
    }
