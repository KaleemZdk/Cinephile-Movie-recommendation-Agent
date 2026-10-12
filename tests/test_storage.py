import sqlite3

import pytest

from cinephile.storage import db, library, threads
from cinephile.storage.library import TitleRef

U = "user-1"


def ref(id_, title="T", kind="movie", year=None, poster=None):
    return TitleRef(id_, kind, title, year, poster)


# ------------------------------------------------------------------ library


def test_favorites_roundtrip_and_duplicate():
    assert library.add_favorite(U, ref(1, "Whiplash", year="2014", poster="/p.jpg")) is True
    assert library.add_favorite(U, ref(1, "Whiplash")) is False
    favs = library.list_favorites(U)
    assert len(favs) == 1
    assert favs[0]["title"] == "Whiplash" and favs[0]["poster_path"] == "/p.jpg"
    assert library.is_favorite(U, 1, "movie") and not library.is_favorite(U, 1, "tv")


def test_movie_and_tv_with_same_id_do_not_collide():
    assert library.add_favorite(U, ref(100, "A Movie", "movie")) is True
    assert library.add_favorite(U, ref(100, "A Show", "tv")) is True
    assert len(library.list_favorites(U)) == 2
    assert library.remove_favorite(U, 100, "tv") is True
    assert [f["title"] for f in library.list_favorites(U)] == ["A Movie"]


def test_remove_without_media_type_removes_any():
    library.add_favorite(U, ref(7, "X", "tv"))
    assert library.remove_favorite(U, 7) is True
    assert library.remove_favorite(U, 7) is False


def test_watching_status_updates_on_repeat():
    assert library.set_watching(U, ref(5, "Show", "tv"), "plan") is True
    assert library.set_watching(U, ref(5, "Show", "tv"), "watched") is False
    items = library.list_watching(U)
    assert len(items) == 1 and items[0]["status"] == "watched"
    assert library.get_watch_status(U, 5, "tv") == "watched"
    assert library.get_watch_status(U, 5, "movie") is None
    assert library.list_watching(U, "plan") == []
    with pytest.raises(ValueError):
        library.set_watching(U, ref(5, "Show", "tv"), "bogus")
    with pytest.raises(ValueError):
        library.update_watching_status(U, 5, "bogus")


def test_review_upsert_keeps_created_at_and_validates_rating():
    library.save_review(U, ref(9, "Heat", year="1995"), 7.5, "good")
    first = library.get_review(U, 9, "movie")
    library.save_review(U, ref(9, ""), 9, "better")
    second = library.get_review(U, 9, "movie")
    assert second["rating"] == 9 and second["comment"] == "better"
    assert second["title"] == "Heat" and second["year"] == "1995"
    assert second["created_at"] == first["created_at"]
    assert len(library.list_reviews(U)) == 1
    for bad in (10.5, -1):
        with pytest.raises(ValueError):
            library.save_review(U, ref(9), bad)


def test_fill_missing_metadata_never_overwrites():
    library.add_favorite(U, ref(3, "Old", year="1999"))
    library.fill_missing_metadata("favorites", U, ref(3, "Old", year="2020", poster="/new.jpg"))
    row = library.list_favorites(U)[0]
    assert row["year"] == "1999" and row["poster_path"] == "/new.jpg"
    with pytest.raises(ValueError):
        library.fill_missing_metadata("threads", U, ref(3))


def test_saved_keys_cover_every_list():
    library.add_favorite(U, ref(1, kind="tv"))
    library.set_watching(U, ref(2))
    library.save_review(U, ref(3), 5)
    assert library.saved_keys(U) == {(1, "tv"), (2, "movie"), (3, "movie")}


def test_title_ref_from_mapping_normalizes_type():
    item = TitleRef.from_mapping({"tmdb_id": "12", "media_type": "TV Show", "title": "Dark"})
    assert item == TitleRef(12, "tv", "Dark", None, None)


def test_normalize_media_type():
    assert db.normalize_media_type("TV Show") == "tv"
    assert db.normalize_media_type("film") == "movie"
    assert db.normalize_media_type(None) == "movie"
    assert db.normalize_media_type("???", default=None) is None


# ---------------------------------------------------------------- migration


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

    fav = library.list_favorites(db.LOCAL_USER)[0]
    assert (fav["tmdb_id"], fav["media_type"], fav["title"]) == (1576, "movie", "Resident Evil")
    assert library.list_watching(db.LOCAL_USER)[0]["status"] == "watching"
    review = library.list_reviews(db.LOCAL_USER)[0]
    assert review["title"] == "Whiplash" and review["rating"] == 9.0 and review["comment"] == "wow"
    assert threads.list_all(db.LOCAL_USER) == []  # the threads table exists after migration


# ------------------------------------------------------------------ threads


def test_thread_lifecycle():
    created = threads.create(U)
    assert created["title"] == threads.DEFAULT_TITLE and created["pinned"] is False
    tid = created["id"]
    assert threads.rename(U, tid, "  Sci-fi   night ")["title"] == "Sci-fi night"
    assert threads.set_pinned(U, tid, True)["pinned"] is True
    assert threads.delete(U, tid) is True
    assert threads.get(U, tid) is None
    assert threads.delete(U, tid) is False


def test_ensure_creates_with_title_then_reuses():
    first = threads.ensure(U, None, "Recommend something like Dark")
    assert first["title"] == "Recommend something like Dark"
    again = threads.ensure(U, first["id"], "a different message")
    assert again["id"] == first["id"] and again["title"] == first["title"]
    unknown = threads.ensure(U, "my-own-id", "hello")
    assert unknown["id"] != "my-own-id", "thread ids come from the server, never from the client"


def test_list_orders_pinned_first_and_searches_literally():
    a = threads.create(U, "100% sci-fi")
    b = threads.create(U, "Plain chat")
    threads.set_pinned(U, b["id"], True)
    assert [t["id"] for t in threads.list_all(U)] == [b["id"], a["id"]]
    assert [t["id"] for t in threads.list_all(U, "100%")] == [a["id"]]
    assert threads.list_all(U, "_") == []  # '_' is a literal, not a wildcard


def test_make_title_and_valid_id():
    assert threads.make_title("   ") == threads.DEFAULT_TITLE
    long = "recommend me something mysterious and slow burning with great atmosphere please " * 2
    title = threads.make_title(long)
    assert len(title) <= threads.TITLE_MAX + 3 and title.endswith("...")
    assert threads.valid_id("abc-DEF_123")
    assert not threads.valid_id("bad id")
    assert not threads.valid_id(None)


# -------------------------------------------------------------------- users


def test_users_never_see_each_others_library():
    other = "user-2"
    assert library.add_favorite(U, ref(1, "Heat")) is True
    assert library.add_favorite(other, ref(1, "Heat")) is True, "two users can save the same title"
    library.set_watching(U, ref(2), "plan")
    library.save_review(U, ref(3, "Up"), 8)
    library.save_review(other, ref(3, "Up"), 2)
    assert library.get_review(U, 3, "movie")["rating"] == 8
    assert library.get_review(other, 3, "movie")["rating"] == 2
    assert library.list_watching(other) == [] and library.get_watch_status(other, 2) is None
    assert library.update_watching_status(other, 2, "watched") is False
    assert library.remove_favorite(other, 1) is True
    assert library.is_favorite(U, 1) and not library.is_favorite(other, 1)
    assert library.saved_keys(other) == {(3, "movie")}


def test_threads_belong_to_their_owner():
    mine = threads.create(U, "My chat")
    other = "user-2"
    assert "user_id" not in mine
    assert threads.get(other, mine["id"]) is None
    assert threads.list_all(other) == []
    assert threads.rename(other, mine["id"], "taken") is None
    assert threads.set_pinned(other, mine["id"], True) is None
    assert threads.delete(other, mine["id"]) is False
    assert threads.ensure(other, mine["id"], "hello")["id"] != mine["id"]
    assert threads.get(U, mine["id"])["title"] == "My chat"


V2_DATABASE = """
    CREATE TABLE favorites (id INTEGER PRIMARY KEY AUTOINCREMENT, tmdb_id INTEGER NOT NULL,
        media_type TEXT NOT NULL DEFAULT 'movie', title TEXT NOT NULL, year TEXT, poster_path TEXT,
        added_at TEXT NOT NULL, UNIQUE (tmdb_id, media_type));
    CREATE TABLE watching (id INTEGER PRIMARY KEY AUTOINCREMENT, tmdb_id INTEGER NOT NULL,
        media_type TEXT NOT NULL DEFAULT 'movie', title TEXT NOT NULL, year TEXT, poster_path TEXT,
        status TEXT NOT NULL DEFAULT 'watching', added_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE (tmdb_id, media_type));
    CREATE TABLE reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, tmdb_id INTEGER NOT NULL,
        media_type TEXT NOT NULL DEFAULT 'movie', title TEXT NOT NULL DEFAULT '', year TEXT, poster_path TEXT,
        rating REAL NOT NULL, comment TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE (tmdb_id, media_type));
    CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT NOT NULL, model TEXT,
        pinned INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    INSERT INTO favorites VALUES (1, 70523, 'tv', 'Dark', '2017', '/d.jpg', '2026-01-01');
    INSERT INTO watching VALUES (1, 27205, 'movie', 'Inception', '2010', NULL, 'plan', '2026-01-02', '2026-01-02');
    INSERT INTO reviews VALUES (1, 244786, 'movie', 'Whiplash', '2014', NULL, 9, 'wow', '2026-01-03', '2026-01-03');
    INSERT INTO threads VALUES ('t1', 'Old chat', NULL, 1, '2026-01-04', '2026-01-04');
    PRAGMA user_version = 2;
"""


def test_migration_from_v2_gives_the_shared_library_to_the_local_user(tmp_path, monkeypatch):
    path = tmp_path / "v2.db"
    conn = sqlite3.connect(path)
    conn.executescript(V2_DATABASE)
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    db.init_db()  # running twice must be harmless

    local = db.LOCAL_USER
    favorites = [(f["tmdb_id"], f["media_type"], f["poster_path"]) for f in library.list_favorites(local)]
    assert favorites == [(70523, "tv", "/d.jpg")]
    assert library.get_watch_status(local, 27205, "movie") == "plan"
    assert library.get_review(local, 244786, "movie")["comment"] == "wow"
    thread = threads.get(local, "t1")
    assert thread["title"] == "Old chat" and thread["pinned"] is True
    assert library.list_favorites(U) == [] and threads.list_all(U) == []
    assert library.add_favorite(U, ref(70523, "Dark", "tv")) is True
    with db.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
