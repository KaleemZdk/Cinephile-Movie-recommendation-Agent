"""Each user's library: favorites, watch list and reviews.

Every function takes the id of the user it acts for and never touches another user's
rows. Titles are identified by (tmdb_id, media_type) because TMDB reuses the same
numeric id for a movie and a TV show.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cinephile.storage.db import (
    LIBRARY_TABLES,
    WATCH_STATUSES,
    connect,
    normalize_media_type,
    now_iso,
)


@dataclass(frozen=True, slots=True)
class TitleRef:
    """What the library stores about a title."""

    tmdb_id: int
    media_type: str = "movie"
    title: str = ""
    year: str | None = None
    poster_path: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> TitleRef:
        return cls(
            tmdb_id=int(data["tmdb_id"]),
            media_type=normalize_media_type(data.get("media_type")),
            title=data.get("title") or "",
            year=data.get("year"),
            poster_path=data.get("poster_path"),
        )


def _check_status(status: str) -> None:
    if status not in WATCH_STATUSES:
        raise ValueError(f"Status must be one of: {', '.join(WATCH_STATUSES)}.")


def _match(sql: str, user_id: str, tmdb_id: int, media_type: str | None) -> tuple[str, list[Any]]:
    """Add the user and id filters, and the media type filter when one is given (None matches either)."""
    params: list[Any] = [user_id, tmdb_id]
    sql += " WHERE user_id = ? AND tmdb_id = ?"
    if media_type:
        sql += " AND media_type = ?"
        params.append(normalize_media_type(media_type))
    return sql, params


def _rows(sql: str, params: list[Any] | tuple = ()) -> list[dict]:
    with connect() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _delete(table: str, user_id: str, tmdb_id: int, media_type: str | None) -> bool:
    sql, params = _match(f"DELETE FROM {table}", user_id, tmdb_id, media_type)
    with connect() as conn:
        return conn.execute(sql, params).rowcount > 0


def _exists(table: str, user_id: str, tmdb_id: int, media_type: str | None) -> bool:
    sql, params = _match(f"SELECT 1 FROM {table}", user_id, tmdb_id, media_type)
    with connect() as conn:
        return conn.execute(sql, params).fetchone() is not None


_ITEM_COLUMNS = "id, tmdb_id, media_type, title, year, poster_path"
_REVIEW_COLUMNS = f"{_ITEM_COLUMNS}, rating, comment, created_at, updated_at"


# --------------------------------------------------------------------------- #
# Favorites
# --------------------------------------------------------------------------- #


def add_favorite(user_id: str, item: TitleRef) -> bool:
    """Add a title to favorites. Returns False if it was already there."""
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO favorites (user_id, tmdb_id, media_type, title, year, poster_path, added_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, item.tmdb_id, item.media_type, item.title, item.year, item.poster_path, now_iso()),
            )
    except sqlite3.IntegrityError:
        return False
    return True


def list_favorites(user_id: str) -> list[dict]:
    sql = f"SELECT {_ITEM_COLUMNS}, added_at FROM favorites WHERE user_id = ? ORDER BY added_at DESC"
    return _rows(sql, [user_id])


def remove_favorite(user_id: str, tmdb_id: int, media_type: str | None = None) -> bool:
    return _delete("favorites", user_id, tmdb_id, media_type)


def is_favorite(user_id: str, tmdb_id: int, media_type: str | None = None) -> bool:
    return _exists("favorites", user_id, tmdb_id, media_type)


# --------------------------------------------------------------------------- #
# Watch list
# --------------------------------------------------------------------------- #


def set_watching(user_id: str, item: TitleRef, status: str = "watching") -> bool:
    """Add a title to the watch list, or move it to a new status.

    Returns True when a new row was created and False when the title was already on
    the list (its status is still updated).
    """
    _check_status(status)
    now = now_iso()
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO watching "
                "(user_id, tmdb_id, media_type, title, year, poster_path, status, added_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, item.tmdb_id, item.media_type, item.title, item.year, item.poster_path, status, now, now),
            )
    except sqlite3.IntegrityError:
        update_watching_status(user_id, item.tmdb_id, status, item.media_type)
        return False
    return True


def update_watching_status(user_id: str, tmdb_id: int, status: str, media_type: str | None = None) -> bool:
    _check_status(status)
    sql, params = _match("UPDATE watching SET status = ?, updated_at = ?", user_id, tmdb_id, media_type)
    with connect() as conn:
        return conn.execute(sql, [status, now_iso(), *params]).rowcount > 0


def list_watching(user_id: str, status: str | None = None) -> list[dict]:
    sql = f"SELECT {_ITEM_COLUMNS}, status, added_at, updated_at FROM watching WHERE user_id = ?"
    params: list[Any] = [user_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    return _rows(sql + " ORDER BY updated_at DESC", params)


def get_watch_status(user_id: str, tmdb_id: int, media_type: str | None = None) -> str | None:
    sql, params = _match("SELECT status FROM watching", user_id, tmdb_id, media_type)
    rows = _rows(sql, params)
    return rows[0]["status"] if rows else None


def remove_watching(user_id: str, tmdb_id: int, media_type: str | None = None) -> bool:
    return _delete("watching", user_id, tmdb_id, media_type)


# --------------------------------------------------------------------------- #
# Reviews
# --------------------------------------------------------------------------- #


def save_review(user_id: str, item: TitleRef, rating: float, comment: str = "") -> None:
    """Create or update the review for a title. Rating must be between 0 and 10."""
    if not 0 <= rating <= 10:  # noqa: PLR2004 - rating scale
        raise ValueError("Rating must be between 0 and 10.")
    now = now_iso()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO reviews
                (user_id, tmdb_id, media_type, title, year, poster_path, rating, comment, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (user_id, tmdb_id, media_type) DO UPDATE SET
                rating = excluded.rating,
                comment = excluded.comment,
                title = CASE WHEN excluded.title != '' THEN excluded.title ELSE reviews.title END,
                year = COALESCE(excluded.year, reviews.year),
                poster_path = COALESCE(excluded.poster_path, reviews.poster_path),
                updated_at = excluded.updated_at
            """,
            (
                user_id,
                item.tmdb_id,
                item.media_type,
                item.title,
                item.year,
                item.poster_path,
                rating,
                comment or "",
                now,
                now,
            ),
        )


def get_review(user_id: str, tmdb_id: int, media_type: str | None = None) -> dict | None:
    sql, params = _match(f"SELECT {_REVIEW_COLUMNS} FROM reviews", user_id, tmdb_id, media_type)
    rows = _rows(sql, params)
    return rows[0] if rows else None


def list_reviews(user_id: str) -> list[dict]:
    return _rows(f"SELECT {_REVIEW_COLUMNS} FROM reviews WHERE user_id = ? ORDER BY updated_at DESC", [user_id])


def remove_review(user_id: str, tmdb_id: int, media_type: str | None = None) -> bool:
    return _delete("reviews", user_id, tmdb_id, media_type)


# --------------------------------------------------------------------------- #
# Maintenance
# --------------------------------------------------------------------------- #


def fill_missing_metadata(table: str, user_id: str, item: TitleRef) -> None:
    """Fill in a missing year or poster on a saved title (never overwrites existing values)."""
    if table not in LIBRARY_TABLES:
        raise ValueError(f"Unknown table: {table}")
    with connect() as conn:
        conn.execute(
            f"UPDATE {table} SET year = COALESCE(year, ?), poster_path = COALESCE(poster_path, ?) "
            "WHERE user_id = ? AND tmdb_id = ? AND media_type = ?",
            (item.year, item.poster_path, user_id, item.tmdb_id, item.media_type),
        )


def saved_keys(user_id: str) -> set[tuple[int, str]]:
    """(tmdb_id, media_type) of everything the user has saved anywhere."""
    keys: set[tuple[int, str]] = set()
    for table in LIBRARY_TABLES:
        rows = _rows(f"SELECT tmdb_id, media_type FROM {table} WHERE user_id = ?", [user_id])
        keys |= {(r["tmdb_id"], r["media_type"]) for r in rows}
    return keys
