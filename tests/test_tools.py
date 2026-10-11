"""MCP tools, called directly with a tiny fake TMDB."""

import json

import pytest

from cinephile import tmdb
from cinephile.mcp_server import server
from cinephile.storage import library
from tests.factories import make_item


@pytest.fixture
def catalog(monkeypatch):
    """Titles by name for resolve_title, details by (id, type)."""
    titles = {
        "whiplash": make_item(244786, "Whiplash", year="2014"),
        "dark": make_item(70523, "Dark", "tv", "2017", pop=200),
    }
    halloween = [make_item(1, "Halloween", year="1978", pop=30), make_item(2, "Halloween", year="2018", pop=25)]
    both = [make_item(55, "Same Id Movie"), make_item(55, "Same Id Show", "tv")]

    async def resolve(title, media_type=None, year=None):
        key = title.strip().lower()
        if key == "halloween":
            return {"match": None, "candidates": halloween}
        if key in titles:
            return {"match": titles[key], "candidates": []}
        return {"match": None, "candidates": []}

    async def details(tmdb_id, media_type="movie"):
        for item in [*titles.values(), *halloween, *both]:
            if item["tmdb_id"] == tmdb_id and item["media_type"] == media_type:
                return {**item, "cast": ["A"], "trailer_url": "https://youtu.be/x"}
        raise tmdb.TMDBError("Not found on TMDB (404)")

    monkeypatch.setattr(tmdb, "resolve_title", resolve)
    monkeypatch.setattr(tmdb, "get_details", details)
    return titles


def load(text):
    return json.loads(text)


async def test_add_favorite_by_title_only(catalog):
    out = load(await server.add_favorite(title="Whiplash"))
    assert out["success"] and not out["already_existed"] and out["item"]["tmdb_id"] == 244786
    assert library.list_favorites()[0]["poster_path"] == "/244786.jpg"
    assert load(await server.add_favorite(title="whiplash"))["already_existed"] is True


async def test_add_favorite_ambiguous_asks_for_clarification(catalog):
    out = load(await server.add_favorite(title="Halloween"))
    assert out["needs_clarification"] and len(out["candidates"]) == 2
    assert library.list_favorites() == []
    chosen = load(await server.add_favorite(title="Halloween", tmdb_id=2, media_type="movie"))
    assert chosen["success"] and chosen["item"]["year"] == "2018"


async def test_id_without_media_type_that_exists_as_both_asks(catalog):
    out = load(await server.add_favorite(tmdb_id=55))
    assert out["needs_clarification"] and {c["media_type"] for c in out["candidates"]} == {"movie", "tv"}
    assert load(await server.add_favorite(tmdb_id=55, title="Same Id Show"))["item"]["media_type"] == "tv"
    assert load(await server.add_favorite(tmdb_id=244786))["item"]["title"] == "Whiplash"  # only one type exists


async def test_unknown_title_and_missing_arguments(catalog):
    assert "error" in load(await server.add_favorite(title="nothing here"))
    assert "error" in load(await server.add_favorite())
    assert "error" in load(await server.add_favorite(tmdb_id=999999))


async def test_tv_show_is_saved_with_its_media_type(catalog):
    await server.add_favorite(title="Dark")
    fav = library.list_favorites()[0]
    assert (fav["tmdb_id"], fav["media_type"]) == (70523, "tv")


async def test_remove_favorite_by_title_uses_saved_list(catalog):
    await server.add_favorite(title="Whiplash")
    assert load(server.remove_favorite(title="whiplash"))["success"]
    assert library.list_favorites() == []
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
    review = library.get_review(244786, "movie")
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


async def test_get_similar_by_title(monkeypatch, catalog):
    async def related(tmdb_id, media_type="movie", limit=10):
        return [make_item(5, "Close Match", media_type)]

    monkeypatch.setattr(tmdb, "get_related", related)
    out = load(await server.get_similar(title="Dark"))
    assert out["based_on"]["title"] == "Dark" and out["results"][0]["title"] == "Close Match"
    assert "popularity" not in out["results"][0]


async def test_recommend_for_user_falls_back_to_trending_then_personalizes(monkeypatch, catalog):
    async def trending(media_type="all", window="week", page=1):
        return [make_item(9, "Trending One")]

    monkeypatch.setattr(tmdb, "get_trending", trending)
    out = load(await server.recommend_for_user())
    assert "trending" in out["strategy"] and out["results"][0]["title"] == "Trending One"

    await server.add_favorite(title="Whiplash")

    async def recommended(tmdb_id, media_type="movie", page=1):
        return [make_item(244786, "Whiplash", year="2014"), make_item(11, "Fresh Pick")]

    monkeypatch.setattr(tmdb, "get_recommended", recommended)
    out = load(await server.recommend_for_user())
    assert [r["title"] for r in out["results"]] == ["Fresh Pick"]
    assert out["results"][0]["because_you_liked"] == ["Whiplash"]


async def test_discover_unknown_genre_lists_valid_names(monkeypatch):
    async def resolve(name, media_type="movie"):
        return None

    async def genres(media_type="movie"):
        return [{"id": 1, "name": "Drama"}]

    monkeypatch.setattr(tmdb, "resolve_genre_id", resolve)
    monkeypatch.setattr(tmdb, "get_genres", genres)
    assert load(await server.discover(genre="Nope"))["valid_genres"] == ["Drama"]


async def test_tool_errors_are_reported_not_raised(monkeypatch):
    async def boom(*args, **kwargs):
        raise tmdb.TMDBError("Could not reach TMDB (ConnectError).")

    monkeypatch.setattr(tmdb, "search", boom)
    assert "TMDB" in load(await server.search_shows("x"))["error"]
