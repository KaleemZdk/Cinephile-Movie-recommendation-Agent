import sqlite3

import pytest

from mcp_server import database as db


def test_favorites_roundtrip_and_duplicate():
    assert db.add_favorite(1, "Whiplash", "movie", "2014", "/p.jpg") is True
    assert db.add_favorite(1, "Whiplash", "movie") is False
    favs = db.list_favorites()
    assert len(favs) == 1
    assert favs[0]["title"] == "Whiplash" and favs[0]["poster_path"] == "/p.jpg"
    assert db.is_favorite(1, "movie") and not db.is_favorite(1, "tv")


def test_movie_and_tv_with_same_id_do_not_collide():
    assert db.add_favorite(100, "A Movie", "movie") is True
    assert db.add_favorite(100, "A Show", "tv") is True
    assert len(db.list_favorites()) == 2
    assert db.remove_favorite(100, "tv") is True
    assert [f["title"] for f in db.list_favorites()] == ["A Movie"]


def test_remove_without_media_type_removes_any():
    db.add_favorite(7, "X", "tv")
    assert db.remove_favorite(7) is True
    assert db.remove_favorite(7) is False


def test_watching_status_updates_on_repeat():
    assert db.set_watching(5, "Show", "tv", status="plan") is True
    assert db.set_watching(5, "Show", "tv", status="watched") is False
    items = db.list_watching()
    assert len(items) == 1 and items[0]["status"] == "watched"
    assert db.list_watching("plan") == []
    with pytest.raises(ValueError):
        db.set_watching(5, "Show", "tv", status="bogus")


def test_review_upsert_keeps_created_at_and_validates_rating():
    db.save_review(9, 7.5, "good", "movie", "Heat", "1995")
    first = db.get_review(9, "movie")
    db.save_review(9, 9, "better", "movie")
    second = db.get_review(9, "movie")
    assert second["rating"] == 9 and second["comment"] == "better"
    assert second["title"] == "Heat" and second["year"] == "1995"
    assert second["created_at"] == first["created_at"]
    assert len(db.list_reviews()) == 1
    with pytest.raises(ValueError):
        db.save_review(9, 10.5)
    with pytest.raises(ValueError):
        db.save_review(9, -1)


def test_normalize_media_type():
    assert db.normalize_media_type("TV Show") == "tv"
    assert db.normalize_media_type("film") == "movie"
    assert db.normalize_media_type(None) == "movie"
    assert db.normalize_media_type("???", default=None) is None


def test_migration_from_v1_keeps_rows(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.executescript("""
        CREATE TABLE favorites (id INTEGER PRIMARY KEY AUTOINCREMENT, tmdb_id INTEGER NOT NULL UNIQUE,
                                title TEXT NOT NULL, added_at TEXT NOT NULL);
        CREATE TABLE watching (id INTEGER PRIMARY KEY AUTOINCREMENT, tmdb_id INTEGER NOT NULL UNIQUE,
                               title TEXT NOT NULL, added_at TEXT NOT NULL);
        CREATE TABLE reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, tmdb_id INTEGER NOT NULL, rating REAL NOT NULL,
                              comment TEXT, created_at TEXT NOT NULL, UNIQUE(tmdb_id));
        CREATE TABLE known_titles (tmdb_id INTEGER PRIMARY KEY, title TEXT NOT NULL);
        INSERT INTO favorites VALUES (1, 1576, 'Resident Evil', '2026-01-01T00:00:00+00:00');
        INSERT INTO watching VALUES (1, 27205, 'Inception', '2026-01-02T00:00:00+00:00');
        INSERT INTO reviews VALUES (1, 244786, 9.0, 'wow', '2026-01-03T00:00:00+00:00');
        INSERT INTO known_titles VALUES (244786, 'Whiplash');
    """)
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "DB_PATH", legacy)
    db.init_db()
    db.init_db()  # running twice must be harmless

    fav = db.list_favorites()[0]
    assert (fav["tmdb_id"], fav["media_type"], fav["title"]) == (1576, "movie", "Resident Evil")
    assert db.list_watching()[0]["status"] == "watching"
    review = db.list_reviews()[0]
    assert review["title"] == "Whiplash" and review["rating"] == 9.0 and review["comment"] == "wow"
