"""
database.py - SQLite storage for favorites, watching and reviews.

Titles are identified by (tmdb_id, media_type) because TMDB reuses the same numeric id
for a movie and a TV show. Both the API process and the MCP server process open this
file, so every connection uses WAL mode and a busy timeout.
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("MOVIES_DB_PATH") or PROJECT_ROOT / "data" / "movies.db")

SCHEMA_VERSION = 2
MEDIA_TYPES = ("movie", "tv")
WATCH_STATUSES = ("plan", "watching", "watched")

_TABLES = ("favorites", "watching", "reviews")


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #

@contextmanager
def connect():
    """Open a connection, commit on success, roll back on error, always close."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_connection():
    """Plain connection for callers that manage commit and close themselves."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def current_time() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_media_type(media_type: str | None, default: str | None = "movie") -> str | None:
    """Return 'movie' or 'tv'. Accepts a few common spellings; falls back to default."""
    value = (media_type or "").strip().lower()
    if value in ("tv", "show", "series", "tv show", "tv series"):
        return "tv"
    if value in ("movie", "film"):
        return "movie"
    return default


# --------------------------------------------------------------------------- #
# Schema and migration
# --------------------------------------------------------------------------- #

def _create_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tmdb_id INTEGER NOT NULL,
            media_type TEXT NOT NULL DEFAULT 'movie' CHECK (media_type IN ('movie', 'tv')),
            title TEXT NOT NULL,
            year TEXT,
            poster_path TEXT,
            added_at TEXT NOT NULL,
            UNIQUE (tmdb_id, media_type)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watching (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tmdb_id INTEGER NOT NULL,
            media_type TEXT NOT NULL DEFAULT 'movie' CHECK (media_type IN ('movie', 'tv')),
            title TEXT NOT NULL,
            year TEXT,
            poster_path TEXT,
            status TEXT NOT NULL DEFAULT 'watching' CHECK (status IN ('plan', 'watching', 'watched')),
            added_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (tmdb_id, media_type)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tmdb_id INTEGER NOT NULL,
            media_type TEXT NOT NULL DEFAULT 'movie' CHECK (media_type IN ('movie', 'tv')),
            title TEXT NOT NULL DEFAULT '',
            year TEXT,
            poster_path TEXT,
            rating REAL NOT NULL CHECK (rating >= 0 AND rating <= 10),
            comment TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (tmdb_id, media_type)
        )
    """)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Version 1 had no media_type, titles on reviews, or watch status. Keep every row."""
    legacy = [t for t in _TABLES if (cols := _table_columns(conn, t)) and "media_type" not in cols]
    for table in legacy:
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}_v1")

    _create_tables(conn)

    has_known_titles = bool(_table_columns(conn, "known_titles"))
    if "favorites" in legacy:
        conn.execute("""
            INSERT INTO favorites (tmdb_id, media_type, title, added_at)
            SELECT tmdb_id, 'movie', title, added_at FROM favorites_v1
        """)
    if "watching" in legacy:
        conn.execute("""
            INSERT INTO watching (tmdb_id, media_type, title, status, added_at, updated_at)
            SELECT tmdb_id, 'movie', title, 'watching', added_at, added_at FROM watching_v1
        """)
    if "reviews" in legacy:
        title_sql = (
            "COALESCE((SELECT k.title FROM known_titles k WHERE k.tmdb_id = r.tmdb_id), '')"
            if has_known_titles else "''"
        )
        conn.execute(f"""
            INSERT INTO reviews (tmdb_id, media_type, title, rating, comment, created_at, updated_at)
            SELECT r.tmdb_id, 'movie', {title_sql}, r.rating, r.comment, r.created_at, r.created_at
            FROM reviews_v1 r
        """)
    for table in legacy:
        conn.execute(f"DROP TABLE {table}_v1")


def init_db() -> None:
    """Create tables if needed and upgrade older databases in place."""
    with connect() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_VERSION:
            _migrate_v1_to_v2(conn)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        else:
            _create_tables(conn)


# --------------------------------------------------------------------------- #
# Favorites
# --------------------------------------------------------------------------- #

_ITEM_COLUMNS = "id, tmdb_id, media_type, title, year, poster_path"


def add_favorite(
    tmdb_id: int,
    title: str,
    media_type: str = "movie",
    year: str | None = None,
    poster_path: str | None = None,
) -> bool:
    """Add a title to favorites. Returns False if it was already there."""
    media_type = normalize_media_type(media_type)
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO favorites (tmdb_id, media_type, title, year, poster_path, added_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (tmdb_id, media_type, title, year, poster_path, current_time()),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def list_favorites() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            f"SELECT {_ITEM_COLUMNS}, added_at FROM favorites ORDER BY added_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def remove_favorite(tmdb_id: int, media_type: str | None = None) -> bool:
    """Remove a favorite. With media_type=None, matches either type."""
    return _delete("favorites", tmdb_id, media_type)


def is_favorite(tmdb_id: int, media_type: str | None = None) -> bool:
    return _exists("favorites", tmdb_id, media_type)


# --------------------------------------------------------------------------- #
# Watching
# --------------------------------------------------------------------------- #

def set_watching(
    tmdb_id: int,
    title: str,
    media_type: str = "movie",
    year: str | None = None,
    poster_path: str | None = None,
    status: str = "watching",
) -> bool:
    """Add a title to the watching list, or move it to a new status.

    Returns True when a new row was created, False when it already existed (its status
    is still updated to the one given).
    """
    media_type = normalize_media_type(media_type)
    if status not in WATCH_STATUSES:
        raise ValueError(f"Status must be one of: {', '.join(WATCH_STATUSES)}.")
    now = current_time()
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO watching (tmdb_id, media_type, title, year, poster_path, status, added_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (tmdb_id, media_type, title, year, poster_path, status, now, now),
            )
        return True
    except sqlite3.IntegrityError:
        update_watching_status(tmdb_id, status, media_type)
        return False


def update_watching_status(tmdb_id: int, status: str, media_type: str | None = None) -> bool:
    if status not in WATCH_STATUSES:
        raise ValueError(f"Status must be one of: {', '.join(WATCH_STATUSES)}.")
    sql = "UPDATE watching SET status = ?, updated_at = ? WHERE tmdb_id = ?"
    params: list = [status, current_time(), tmdb_id]
    if media_type:
        sql += " AND media_type = ?"
        params.append(normalize_media_type(media_type))
    with connect() as conn:
        return conn.execute(sql, params).rowcount > 0


def list_watching(status: str | None = None) -> list[dict]:
    sql = f"SELECT {_ITEM_COLUMNS}, status, added_at, updated_at FROM watching"
    params: list = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY updated_at DESC"
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def remove_watching(tmdb_id: int, media_type: str | None = None) -> bool:
    return _delete("watching", tmdb_id, media_type)


def is_watching(tmdb_id: int, media_type: str | None = None) -> bool:
    return _exists("watching", tmdb_id, media_type)


# --------------------------------------------------------------------------- #
# Reviews
# --------------------------------------------------------------------------- #

def save_review(
    tmdb_id: int,
    rating: float,
    comment: str = "",
    media_type: str = "movie",
    title: str | None = None,
    year: str | None = None,
    poster_path: str | None = None,
) -> bool:
    """Create or update the review for a title. Rating must be between 0 and 10."""
    if not 0 <= rating <= 10:
        raise ValueError("Rating must be between 0 and 10.")
    media_type = normalize_media_type(media_type)
    now = current_time()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO reviews (tmdb_id, media_type, title, year, poster_path, rating, comment, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (tmdb_id, media_type) DO UPDATE SET
                rating = excluded.rating,
                comment = excluded.comment,
                title = CASE WHEN excluded.title != '' THEN excluded.title ELSE reviews.title END,
                year = COALESCE(excluded.year, reviews.year),
                poster_path = COALESCE(excluded.poster_path, reviews.poster_path),
                updated_at = excluded.updated_at
            """,
            (tmdb_id, media_type, title or "", year, poster_path, rating, comment or "", now, now),
        )
    return True


_REVIEW_COLUMNS = f"{_ITEM_COLUMNS}, rating, comment, created_at, updated_at"


def get_review(tmdb_id: int, media_type: str | None = None) -> dict | None:
    sql = f"SELECT {_REVIEW_COLUMNS} FROM reviews WHERE tmdb_id = ?"
    params: list = [tmdb_id]
    if media_type:
        sql += " AND media_type = ?"
        params.append(normalize_media_type(media_type))
    with connect() as conn:
        row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def list_reviews() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            f"SELECT {_REVIEW_COLUMNS} FROM reviews ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def remove_review(tmdb_id: int, media_type: str | None = None) -> bool:
    return _delete("reviews", tmdb_id, media_type)


def update_meta(table: str, tmdb_id: int, media_type: str, year: str | None, poster_path: str | None) -> None:
    """Fill in a missing year or poster on a saved title (never overwrites existing values)."""
    if table not in _TABLES:
        raise ValueError(f"Unknown table: {table}")
    with connect() as conn:
        conn.execute(
            f"UPDATE {table} SET year = COALESCE(year, ?), poster_path = COALESCE(poster_path, ?) "
            "WHERE tmdb_id = ? AND media_type = ?",
            (year, poster_path, tmdb_id, normalize_media_type(media_type)),
        )


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _delete(table: str, tmdb_id: int, media_type: str | None) -> bool:
    sql = f"DELETE FROM {table} WHERE tmdb_id = ?"
    params: list = [tmdb_id]
    if media_type:
        sql += " AND media_type = ?"
        params.append(normalize_media_type(media_type))
    with connect() as conn:
        return conn.execute(sql, params).rowcount > 0


def _exists(table: str, tmdb_id: int, media_type: str | None) -> bool:
    sql = f"SELECT 1 FROM {table} WHERE tmdb_id = ?"
    params: list = [tmdb_id]
    if media_type:
        sql += " AND media_type = ?"
        params.append(normalize_media_type(media_type))
    with connect() as conn:
        return conn.execute(sql, params).fetchone() is not None


if __name__ == "__main__":
    init_db()
    print(f"Database ready: {DB_PATH}")
