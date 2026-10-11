"""
library.py - The user's favorites, watch list and reviews.

Plain CRUD over SQLite; no model call is involved, so these are fast.
Items are identified by (tmdb_id, media_type) because TMDB reuses ids between movies and shows.
"""

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.cards import ui_item
from mcp_server import database, tmdb

router = APIRouter(tags=["library"])


class SaveIn(BaseModel):
    tmdb_id: int | None = None
    show_id: int | None = None  # older name for tmdb_id
    media_type: str = "movie"
    title: str = ""
    year: str | None = None
    poster_path: str | None = None
    status: str = "watching"


class StatusIn(BaseModel):
    status: str


class ReviewIn(SaveIn):
    rating: float
    comment: str = ""


def _kind(media_type: str | None) -> str:
    return database.normalize_media_type(media_type)


def _same_title(a: str, b: str) -> bool:
    return tmdb._norm_title(a) == tmdb._norm_title(b)


async def _complete(body: SaveIn) -> tuple[int, str, str, str | None, str | None]:
    """Resolve id, type, title, year and poster for a save request, asking TMDB if anything is missing."""
    tmdb_id = body.tmdb_id or body.show_id
    if not tmdb_id:
        raise HTTPException(400, "tmdb_id is required.")
    kind = _kind(body.media_type)
    title, year, poster = body.title.strip(), body.year, body.poster_path
    if not title or not poster:
        try:
            details = await tmdb.get_details(tmdb_id, kind)
            if not title or _same_title(title, details["title"]):
                title = title or details["title"]
                year = year or details["year"]
                poster = poster or details["poster_path"]
        except tmdb.TMDBError:
            if not title:
                raise HTTPException(404, "That title was not found.")
    return tmdb_id, kind, title, year, poster


async def _backfill(table: str, rows: list[dict]) -> None:
    """Older saved rows have no poster or year; fetch them once and store them."""
    missing = [r for r in rows if not r.get("poster_path")][:20]
    if not missing:
        return

    async def fetch(row):
        try:
            return await tmdb.get_details(row["tmdb_id"], row["media_type"])
        except tmdb.TMDBError:
            return None

    for row, details in zip(missing, await asyncio.gather(*(fetch(r) for r in missing))):
        if details and _same_title(row["title"], details["title"]):
            row["poster_path"] = details["poster_path"]
            row["year"] = row.get("year") or details["year"]
            database.update_meta(table, row["tmdb_id"], row["media_type"], row["year"], row["poster_path"])


def _saved(row: dict) -> dict:
    item = ui_item(row)
    for key in ("status", "added_at", "updated_at", "comment"):
        if row.get(key) is not None:
            item[key] = row[key]
    if "rating" in row and "comment" in row:
        item["rating"] = row["rating"]
        item["review"] = True
    item["saved_at"] = row.get("added_at") or row.get("created_at")
    return item


# ---------------------------------------------------------------- favorites

@router.get("/favorites")
async def list_favorites():
    rows = database.list_favorites()
    await _backfill("favorites", rows)
    return [_saved(r) for r in rows]


@router.post("/favorites")
async def add_favorite(body: SaveIn):
    tmdb_id, kind, title, year, poster = await _complete(body)
    added = database.add_favorite(tmdb_id, title, kind, year, poster)
    return {"success": True, "already_existed": not added,
            "item": ui_item({"tmdb_id": tmdb_id, "media_type": kind, "title": title, "year": year, "poster_path": poster})}


@router.delete("/favorites/{tmdb_id}")
def remove_favorite(tmdb_id: int, media_type: str | None = None):
    return {"success": database.remove_favorite(tmdb_id, media_type)}


# ----------------------------------------------------------------- watching

@router.get("/watching")
async def list_watching(status: str | None = None):
    if status and status not in database.WATCH_STATUSES:
        raise HTTPException(400, f"status must be one of: {', '.join(database.WATCH_STATUSES)}.")
    rows = database.list_watching(status)
    await _backfill("watching", rows)
    return [_saved(r) for r in rows]


@router.post("/watching")
async def add_watching(body: SaveIn):
    if body.status not in database.WATCH_STATUSES:
        raise HTTPException(400, f"status must be one of: {', '.join(database.WATCH_STATUSES)}.")
    tmdb_id, kind, title, year, poster = await _complete(body)
    added = database.set_watching(tmdb_id, title, kind, year, poster, body.status)
    return {"success": True, "already_existed": not added, "status": body.status,
            "item": ui_item({"tmdb_id": tmdb_id, "media_type": kind, "title": title, "year": year, "poster_path": poster})}


@router.patch("/watching/{tmdb_id}")
def change_status(tmdb_id: int, body: StatusIn, media_type: str | None = None):
    try:
        changed = database.update_watching_status(tmdb_id, body.status, media_type)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not changed:
        raise HTTPException(404, "That title is not on your watch list.")
    return {"success": True}


@router.delete("/watching/{tmdb_id}")
def remove_watching(tmdb_id: int, media_type: str | None = None):
    return {"success": database.remove_watching(tmdb_id, media_type)}


# ------------------------------------------------------------------ reviews

@router.get("/reviews")
async def list_reviews():
    rows = database.list_reviews()
    await _backfill("reviews", rows)
    return [_saved(r) for r in rows]


@router.post("/reviews")
async def save_review(body: ReviewIn):
    tmdb_id, kind, title, year, poster = await _complete(body)
    try:
        database.save_review(tmdb_id, body.rating, body.comment, kind, title, year, poster)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"success": True}


@router.delete("/reviews/{tmdb_id}")
def remove_review(tmdb_id: int, media_type: str | None = None):
    return {"success": database.remove_review(tmdb_id, media_type)}


# ---------------------------------------------------------------- summary

@router.get("/library/ids")
def library_ids():
    """Compact lookup the UI uses to mark cards as saved: 'type:id' keys."""
    def key(row):
        return f"{row['media_type']}:{row['tmdb_id']}"

    return {
        "favorites": [key(r) for r in database.list_favorites()],
        "watching": {key(r): r["status"] for r in database.list_watching()},
        "reviews": {key(r): r["rating"] for r in database.list_reviews()},
    }
