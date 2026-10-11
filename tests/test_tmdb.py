import pytest

from cinephile import tmdb


def raw_movie(id_, title, date="2010-07-16", pop=50.0, genres=(28, 878)):
    return {
        "id": id_,
        "title": title,
        "release_date": date,
        "vote_average": 8.36,
        "popularity": pop,
        "genre_ids": list(genres),
        "overview": "x" * 300,
        "poster_path": f"/{id_}.jpg",
    }


def raw_tv(id_, name, date="2017-12-01", pop=50.0):
    return {
        "id": id_,
        "name": name,
        "first_air_date": date,
        "vote_average": 8.4,
        "popularity": pop,
        "genre_ids": [10765, 9648],
        "overview": "tv",
        "poster_path": f"/{id_}.jpg",
    }


def test_normalize_movie_and_tv():
    movie = tmdb.normalize(raw_movie(1, "Inception"), "movie")
    assert movie["media_type"] == "movie" and movie["year"] == "2010"
    assert movie["genres"] == ["Action", "Science Fiction"]
    assert len(movie["overview"]) <= tmdb.OVERVIEW_MAX_CHARS
    assert movie["rating"] == 8.4

    tv = tmdb.normalize(raw_tv(2, "Dark"))
    assert tv["media_type"] == "tv" and tv["title"] == "Dark" and tv["year"] == "2017"
    assert tv["genres"] == ["Sci-Fi & Fantasy", "Mystery"]


def test_helpers():
    assert tmdb.image_url("/a.jpg", "w500") == "https://image.tmdb.org/t/p/w500/a.jpg"
    assert tmdb.image_url(None) is None
    assert tmdb.normalize_title("The Matrix: Reloaded!") == "the matrix reloaded"
    assert tmdb.same_title("Spider-Man", "spider man")
    assert tmdb.card({"a": 1, "b": None, "c": [], "popularity": 3, "rating": 0.0}) == {"a": 1, "rating": 0.0}


@pytest.fixture
def fake_search(monkeypatch):
    def install(results):
        async def fake(query, media_type="all", year=None, page=1):
            return [tmdb.normalize(r) for r in results]

        monkeypatch.setattr(tmdb, "search", fake)

    return install


async def test_resolve_single_exact_match(fake_search):
    fake_search([raw_movie(1, "Inception"), raw_movie(2, "Inception: The Cobol Job", pop=1)])
    result = await tmdb.resolve_title("inception")
    assert result["match"]["tmdb_id"] == 1 and result["candidates"] == []


async def test_resolve_picks_clear_favourite_among_same_names(fake_search):
    fake_search([raw_tv(70523, "Dark", pop=200), raw_movie(9, "Dark", pop=3)])
    assert (await tmdb.resolve_title("Dark"))["match"]["media_type"] == "tv"


async def test_resolve_ambiguous_returns_candidates(fake_search):
    fake_search([raw_movie(1, "Halloween", "1978-10-25", pop=30), raw_movie(2, "Halloween", "2018-10-18", pop=25)])
    result = await tmdb.resolve_title("Halloween")
    assert result["match"] is None and len(result["candidates"]) == 2


async def test_resolve_year_narrows_ambiguity(fake_search):
    fake_search([raw_movie(1, "Halloween", "1978-10-25", pop=30), raw_movie(2, "Halloween", "2018-10-18", pop=25)])
    assert (await tmdb.resolve_title("Halloween", year=2018))["match"]["tmdb_id"] == 2


async def test_resolve_no_results(fake_search):
    fake_search([])
    assert await tmdb.resolve_title("zzzz") == {"match": None, "candidates": []}


async def test_resolve_inexact_but_many_results_is_ambiguous(fake_search):
    fake_search([raw_movie(1, "The Matrix Reloaded"), raw_movie(2, "The Matrix Revolutions")])
    result = await tmdb.resolve_title("matrix")
    assert result["match"] is None and len(result["candidates"]) == 2


async def test_related_tops_up_recommendations_with_similar(monkeypatch):
    async def recommended(tmdb_id, media_type="movie", page=1):
        return [tmdb.normalize(raw_movie(1, "A"))]

    async def similar(tmdb_id, media_type="movie", page=1):
        return [tmdb.normalize(raw_movie(1, "A")), tmdb.normalize(raw_movie(2, "B"))]

    monkeypatch.setattr(tmdb, "get_recommended", recommended)
    monkeypatch.setattr(tmdb, "get_similar", similar)
    assert [r["title"] for r in await tmdb.get_related(5, "movie")] == ["A", "B"]


class _Response:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self.headers = {}
        self._payload = payload if payload is not None else {"results": []}
        self.text = ""

    def json(self):
        return self._payload


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.is_closed = False

    async def get(self, endpoint, params=None):
        self.calls += 1
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


@pytest.fixture
def fake_client(monkeypatch):
    def install(*responses):
        client = _Client(responses or [_Response()])
        monkeypatch.setattr(tmdb, "TMDB_CREDENTIAL", "abc")
        monkeypatch.setattr(tmdb._transport, "client", lambda: client)
        return client

    return install


async def test_get_is_cached_per_endpoint_and_params(fake_client):
    client = fake_client()
    await tmdb._get("/x", {"a": 1})
    await tmdb._get("/x", {"a": 1})
    await tmdb._get("/x", {"a": 2})
    assert client.calls == 2


async def test_errors_become_tmdb_errors(fake_client, monkeypatch):
    fake_client(_Response(401, {"status_message": "Invalid API key"}))
    with pytest.raises(tmdb.TMDBError, match="401"):
        await tmdb._get("/y")

    async def no_sleep(_):
        return None

    monkeypatch.setattr(tmdb.asyncio, "sleep", no_sleep)
    client = fake_client(_Response(503), _Response(200, {"ok": True}))
    assert await tmdb._get("/z") == {"ok": True}
    assert client.calls == 2  # retried once after the 503


async def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(tmdb, "TMDB_CREDENTIAL", None)
    with pytest.raises(tmdb.TMDBError, match="TMDB_API_KEY"):
        await tmdb._get("/nothing")
