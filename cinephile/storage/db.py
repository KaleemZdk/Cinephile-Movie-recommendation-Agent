"""SQLite connection, schema and migrations.

Both the web API process and the MCP server process open the same file, so every
connection uses WAL mode and a busy timeout.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from cinephile.config import get_settings

DB_PATH: Path = get_settings().library_db  # tests point this at a temporary file

SCHEMA_VERSION = 2
MEDIA_TYPES = ("movie", "tv")
WATCH_STATUSES = ("plan", "watching", "watched")
LIBRARY_TABLES = ("favorites", "watching", "reviews")

_TV_WORDS = frozenset({"tv", "show", "series", "tv show", "tv series"})
_MOVIE_WORDS = frozenset({"movie", "film"})


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
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


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def normalize_media_type(media_type: str | None, default: str | None = "movie") -> str | None:
    """Return 'movie' or 'tv'. Accepts a few common spellings; otherwise returns default."""
    value = (media_type or "").strip().lower()
    if value in _TV_WORDS:
        return "tv"
    if value in _MOVIE_WORDS:
        return "movie"
    return default


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

_SCHEMA = (
    """
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
    """,
    """
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
    """,
    """
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
    """,
    """
    CREATE TABLE IF NOT EXISTS threads (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        model TEXT,
        pinned INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
)


def _create_tables(conn: sqlite3.Connection) -> None:
    for statement in _SCHEMA:
        conn.execute(statement)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Version 1 had no media_type, no titles on reviews and no watch status. Every row is kept."""
    legacy = [t for t in LIBRARY_TABLES if (cols := _table_columns(conn, t)) and "media_type" not in cols]
    for table in legacy:
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}_v1")

    _create_tables(conn)

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
        # An early version of the API kept review titles in a side table.
        title_sql = (
            "COALESCE((SELECT k.title FROM known_titles k WHERE k.tmdb_id = r.tmdb_id), '')"
            if _table_columns(conn, "known_titles")
            else "''"
        )
        conn.execute(f"""
            INSERT INTO reviews (tmdb_id, media_type, title, rating, comment, created_at, updated_at)
            SELECT r.tmdb_id, 'movie', {title_sql}, r.rating, r.comment, r.created_at, r.created_at
            FROM reviews_v1 r
        """)
    for table in legacy:
        conn.execute(f"DROP TABLE {table}_v1")


def init_db() -> None:
    """Create missing tables and upgrade older databases in place. Safe to call repeatedly."""
    with connect() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 2:  # noqa: PLR2004 - schema version number
            _migrate_v1_to_v2(conn)
        _create_tables(conn)
        if version < SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
