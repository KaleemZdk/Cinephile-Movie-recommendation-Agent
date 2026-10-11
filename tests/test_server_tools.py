import json

import pytest

from mcp_server import database, server, tmdb


def item(id_, title, kind="movie", year="2010", rating=8.0, pop=10.0):
    return {"tmdb_id": id_, "media_type": kind, "title": title, "year": year, "rating": rating,
            "genres": ["Drama"], "overview": "o", "poster_path": f"/{id_}.jpg", "popularity": pop}


@pytest.fixture
def catalog(monkeypatch):
    """A tiny fake TMDB: titles by name, details by (id, type)."""
    titles = {
        "whiplash": item(244786, "Whiplash", year="2014"),
        "dark": item(70523, "Dark", "tv", "2017", pop=200),
        "halloween": None,
    }
    halloween = [item(1, "Halloween", year="1978", pop=30), item(2, "Halloween", year="2018", pop=25)]

    async def resolve(title, media_type=None, year=None):
        key = title.strip().lower()
        if key == "halloween":
            return {"match": None, "candidates": halloween}
        if titles.get(key):
            return {"match": titles[key], "candidates": []}
        return {"match": None, "candidates": []}

    async def details(tmdb_id, media_type="movie"):
        for t in list(titles.values()) + halloween:
            if t and t["tmdb_id"] == tmdb_id and t["media_type"] == media_type:
                return {**t, "cast": ["A"], "trailer_url": "https://youtu.be/x"}
        raise tmdb.TMDBError("Not found on TMDB (404)")

    monkeypatch.setattr(tmdb, "resolve_title", resolve)
    monkeypatch.setattr(tmdb, "get_details", details)
    return titles


def load(text):
    return json.loads(text)


async def test_add_favorite_by_title_only(catalog):
    out = load(await server.add_favorite(title="Whiplash"))
    assert out["success"] and not out["already_existed"] and out["item"]["tmdb_id"] == 244786
    assert database.list_favorites()[0]["poster_path"] == "/244786.jpg"
    again = load(await server.add_favorite(title="whiplash"))
    assert again["already_existed"] is True


async def test_add_favorite_ambiguous_asks_for_clarification(catalog):
    out = load(await server.add_favorite(title="Halloween"))
    assert out["needs_clarification"] and len(out["candidates"]) == 2
    assert database.list_favorites() == []
    chosen = load(await server.add_favorite(title="Halloween", tmdb_id=2, media_type="movie"))
    assert chosen["success"] and chosen["item"]["year"] == "2018"


async def test_add_favorite_unknown_title_and_missing_args(catalog):
    assert "error" in load(await server.add_favorite(title="nothing here"))
    assert "error" in load(await server.add_favorite())


async def test_tv_show_saved_with_media_type(catalog):
    await server.add_favorite(title="Dark")
    fav = database.list_favorites()[0]
    assert (fav["tmdb_id"], fav["media_type"]) == (70523, "tv")


async def test_remove_favorite_by_title_uses_saved_list(catalog):
    await server.add_favorite(title="Whiplash")
    out = load(server.remove_favorite(title="whiplash"))
    assert out["success"] and database.list_favorites() == []
    assert "error" in load(server.remove_favorite(title="whiplash"))


async def test_set_watching_status_flow(catalog):
    first = load(await server.set_watching(title="Dark", status="plan"))
    assert first["success"] and not first["already_existed"]
    second = load(await server.set_watching(title="Dark", status="watched"))
    assert second["already_existed"] and second["status"] == "watched"
    assert load(server.list_watching("watched"))["count"] == 1
    assert load(server.list_watching("plan"))["count"] == 0
    assert "error" in load(await server.set_watching(title="Dark", status="nope"))
    assert "error" in load(server.list_watching("nope"))


async def test_save_review_validates_rating_and_accepts_show_id(catalog):
    assert "error" in load(await server.save_review(rating=11, title="Whiplash"))
    out = load(await server.save_review(rating=9.5, comment="Superb", show_id=244786, media_type="movie"))
    assert out["success"]
    review = database.get_review(244786, "movie")
    assert review["rating"] == 9.5 and review["title"] == "Whiplash"
    reviews = load(server.list_reviews())
    assert reviews["count"] == 1 and reviews["reviews"][0]["comment"] == "Superb"
    assert load(server.get_review(title="whiplash"))["review"]["rating"] == 9.5
    assert load(server.get_review(title="unknown"))["review"] is None


async def test_user_profile_contains_all_sections(catalog):
    await server.add_favorite(title="Whiplash")
    await server.set_watching(title="Dark")
    profile = load(server.get_user_profile())
    assert set(profile) == {"favorites", "watching", "reviews"}
    assert profile["favorites"][0]["title"] == "Whiplash" and profile["watching"][0]["media_type"] == "tv"


async def test_get_similar_by_title_and_fallback(monkeypatch, catalog):
    async def similar(tmdb_id, media_type="movie", page=1):
        return [item(5, "Close Match", media_type)]

    async def recommended(tmdb_id, media_type="movie", page=1):
        return []

    monkeypatch.setattr(tmdb, "get_similar", similar)
    monkeypatch.setattr(tmdb, "get_recommended", recommended)
    out = load(await server.get_similar(title="Dark"))
    assert out["based_on"]["title"] == "Dark" and out["results"][0]["title"] == "Close Match"
    # recommendations are tried first and topped up from the similar list
    assert [r["title"] for r in out["results"]] == ["Close Match"]


async def test_recommend_for_user_excludes_saved_and_falls_back_to_trending(monkeypatch, catalog):
    async def trending(media_type="all", window="week", page=1):
        return [item(9, "Trending One")]

    monkeypatch.setattr(tmdb, "get_trending", trending)
    out = load(await server.recommend_for_user())
    assert "trending" in out["strategy"] and out["results"][0]["title"] == "Trending One"

    await server.add_favorite(title="Whiplash")

    async def recommended(tmdb_id, media_type="movie", page=1):
        return [item(244786, "Whiplash", year="2014"), item(11, "Fresh Pick")]

    monkeypatch.setattr(tmdb, "get_recommended", recommended)
    out = load(await server.recommend_for_user())
    titles = [r["title"] for r in out["results"]]
    assert titles == ["Fresh Pick"]
    assert out["results"][0]["because_you_liked"] == ["Whiplash"]


async def test_discover_unknown_genre_lists_valid_names(monkeypatch):
    async def resolve(name, media_type="movie"):
        return None

    async def genres(media_type="movie"):
        return [{"id": 1, "name": "Drama"}]

    monkeypatch.setattr(tmdb, "resolve_genre_id", resolve)
    monkeypatch.setattr(tmdb, "get_genres", genres)
    out = load(await server.discover(genre="Nope"))
    assert out["valid_genres"] == ["Drama"]
