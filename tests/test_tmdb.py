import pytest

from mcp_server import tmdb


def raw_movie(id_, title, date="2010-07-16", pop=50.0, genres=(28, 878)):
    return {"id": id_, "title": title, "release_date": date, "vote_average": 8.36, "popularity": pop,
            "genre_ids": list(genres), "overview": "x" * 300, "poster_path": f"/{id_}.jpg"}


def raw_tv(id_, name, date="2017-12-01", pop=50.0):
    return {"id": id_, "name": name, "first_air_date": date, "vote_average": 8.4, "popularity": pop,
            "genre_ids": [10765, 9648], "overview": "tv", "poster_path": f"/{id_}.jpg"}


def test_normalize_movie_and_tv():
    movie = tmdb._normalize(raw_movie(1, "Inception"), "movie")
    assert movie["media_type"] == "movie" and movie["year"] == "2010"
    assert movie["genres"] == ["Action", "Science Fiction"]
    assert len(movie["overview"]) <= tmdb.OVERVIEW_MAX_CHARS
    assert movie["rating"] == 8.4

    tv = tmdb._normalize(raw_tv(2, "Dark"))
    assert tv["media_type"] == "tv" and tv["title"] == "Dark" and tv["year"] == "2017"
    assert tv["genres"] == ["Sci-Fi & Fantasy", "Mystery"]


def test_image_url():
    assert tmdb.image_url("/a.jpg", "w500") == "https://image.tmdb.org/t/p/w500/a.jpg"
    assert tmdb.image_url(None) is None


@pytest.fixture
def fake_search(monkeypatch):
    def install(results):
        async def fake(query, media_type="all", year=None, page=1):
            return [tmdb._normalize(r) for r in results]
        monkeypatch.setattr(tmdb, "search", fake)
    return install


async def test_resolve_single_exact_match(fake_search):
    fake_search([raw_movie(1, "Inception"), raw_movie(2, "Inception: The Cobol Job", pop=1)])
    result = await tmdb.resolve_title("inception")
    assert result["match"]["tmdb_id"] == 1 and result["candidates"] == []


async def test_resolve_picks_clear_favourite_among_same_names(fake_search):
    fake_search([raw_tv(70523, "Dark", pop=200), raw_movie(9, "Dark", pop=3)])
    result = await tmdb.resolve_title("Dark")
    assert result["match"]["media_type"] == "tv"


async def test_resolve_ambiguous_returns_candidates(fake_search):
    fake_search([raw_movie(1, "Halloween", "1978-10-25", pop=30), raw_movie(2, "Halloween", "2018-10-18", pop=25)])
    result = await tmdb.resolve_title("Halloween")
    assert result["match"] is None and len(result["candidates"]) == 2


async def test_resolve_year_narrows_ambiguity(fake_search):
    fake_search([raw_movie(1, "Halloween", "1978-10-25", pop=30), raw_movie(2, "Halloween", "2018-10-18", pop=25)])
    result = await tmdb.resolve_title("Halloween", year=2018)
    assert result["match"]["tmdb_id"] == 2


async def test_resolve_no_results(fake_search):
    fake_search([])
    result = await tmdb.resolve_title("zzzz")
    assert result == {"match": None, "candidates": []}


async def test_resolve_inexact_but_many_results_is_ambiguous(fake_search):
    fake_search([raw_movie(1, "The Matrix Reloaded"), raw_movie(2, "The Matrix Revolutions")])
    result = await tmdb.resolve_title("matrix")
    assert result["match"] is None and len(result["candidates"]) == 2


async def test_get_is_cached(monkeypatch):
    calls = {"n": 0}

    class Resp:
        status_code = 200
        headers = {}
        def json(self):
            return {"results": []}

    class Client:
        is_closed = False
        async def get(self, endpoint, params=None):
            calls["n"] += 1
            return Resp()

    monkeypatch.setattr(tmdb, "TMDB_API_KEY", "abc")
    monkeypatch.setattr(tmdb, "_get_client", lambda: Client())
    await tmdb._get("/x", {"a": 1})
    await tmdb._get("/x", {"a": 1})
    await tmdb._get("/x", {"a": 2})
    assert calls["n"] == 2
