import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import agent
from backend import chat as chat_module
from backend import threads
from backend.cards import cards_from_turn, messages_to_ui, ui_item
from mcp_server import database, tmdb

import api


def movie(id_, title, kind="movie", year="2010"):
    return {"tmdb_id": id_, "media_type": kind, "title": title, "year": year, "rating": 8.1,
            "genres": ["Drama"], "overview": "o", "poster_path": f"/{id_}.jpg", "popularity": 5.0}


def tool_result(name, payload, call_id="c1"):
    return ToolMessage(content=json.dumps(payload), tool_call_id=call_id, name=name)


@pytest.fixture
def client():
    threads.init_threads()
    chat_module._locks.clear()
    return TestClient(api.app)  # no `with`: skip lifespan so no MCP server or model is started


# ------------------------------------------------------------------ cards

def test_ui_item_builds_poster_url():
    item = ui_item(movie(1, "Heat"))
    assert item["poster_url"] == "https://image.tmdb.org/t/p/w342/1.jpg"
    assert item["id"] == 1 and item["media_type"] == "movie"
    assert ui_item({"tmdb_id": 2, "title": "X"})["poster_url"] is None


def test_cards_only_for_titles_the_reply_mentions_in_reply_order():
    results = [movie(1, "Her"), movie(2, "Ex Machina"), movie(3, "Moon")]
    turn = [tool_result("search_shows", {"results": results})]
    cards = cards_from_turn(turn, "Try Moon first, then Ex Machina. I told her about it.")
    assert [c["title"] for c in cards] == ["Moon", "Ex Machina"]  # 'Her' must not match the word 'her'


def test_cards_dedupe_and_accept_single_details_payload():
    details = movie(5, "Dark", "tv", "2017")
    turn = [tool_result("get_details", details), tool_result("get_details", details, "c2")]
    cards = cards_from_turn(turn, "Dark is great.")
    assert len(cards) == 1 and cards[0]["media_type"] == "tv"


def test_cards_keep_one_card_per_title():
    first = movie(1, "Severance", "tv", "2022")
    others = [movie(2, "Severance", "movie", "1989"), movie(3, "Severance", "movie", "2006")]
    turn = [tool_result("get_similar", {"results": [first]}), tool_result("search_shows", {"results": others}, "c2")]
    cards = cards_from_turn(turn, "Watch Severance tonight.")
    assert [(c["id"], c["media_type"]) for c in cards] == [(1, "tv")]


def test_cards_ignore_library_tools_and_bad_json():
    turn = [tool_result("add_favorite", {"item": movie(1, "Heat")}),
            ToolMessage(content="not json", tool_call_id="c9", name="search_shows")]
    assert cards_from_turn(turn, "Heat") == []


def test_messages_to_ui_groups_turns_and_skips_unanswered():
    call = AIMessage(content="", tool_calls=[{"name": "search_shows", "args": {}, "id": "c1"}])
    msgs = [
        HumanMessage("find heat"), call,
        tool_result("search_shows", {"results": [movie(1, "Heat")]}),
        AIMessage(content="Heat (1995) is a great crime film."),
        HumanMessage("and then?"),  # failed turn: no reply
    ]
    ui = messages_to_ui(msgs)
    assert [m["role"] for m in ui] == ["user", "assistant", "user"]
    assert ui[1]["recommendations"][0]["title"] == "Heat"


# ---------------------------------------------------------------- threads

def test_thread_lifecycle(client, monkeypatch):
    async def fake_messages(thread_id):
        return [HumanMessage("hello"), AIMessage(content="hi there")]

    deleted = []

    async def fake_delete(thread_id):
        deleted.append(thread_id)

    monkeypatch.setattr(agent, "get_thread_messages", fake_messages)
    monkeypatch.setattr(agent, "delete_thread", fake_delete)

    created = client.post("/threads").json()
    tid = created["id"]
    assert created["title"] == "New chat"
    assert [t["id"] for t in client.get("/threads").json()["threads"]] == [tid]

    opened = client.get(f"/threads/{tid}").json()
    assert [m["role"] for m in opened["messages"]] == ["user", "assistant"]

    assert client.patch(f"/threads/{tid}", json={"title": "Sci-fi night"}).json()["title"] == "Sci-fi night"
    assert client.patch(f"/threads/{tid}", json={"pinned": True}).json()["pinned"] is True
    assert client.get("/threads", params={"q": "sci"}).json()["threads"][0]["id"] == tid
    assert client.get("/threads", params={"q": "zzz"}).json()["threads"] == []

    assert client.delete(f"/threads/{tid}").json() == {"success": True}
    assert deleted == [tid]
    assert client.get(f"/threads/{tid}").status_code == 404
    assert client.delete(f"/threads/{tid}").status_code == 404
    assert client.get("/threads/bad id!").status_code == 404


def test_pinned_threads_sort_first(client):
    a = client.post("/threads").json()["id"]
    b = client.post("/threads").json()["id"]
    client.patch(f"/threads/{a}", json={"pinned": True})
    assert [t["id"] for t in client.get("/threads").json()["threads"]] == [a, b]


def test_make_title_trims_on_word_boundary():
    assert threads.make_title("   ") == "New chat"
    long = "recommend me something mysterious and slow burning with great atmosphere please " * 2
    title = threads.make_title(long)
    assert len(title) <= threads.TITLE_MAX + 3 and title.endswith("...")


# ------------------------------------------------------------------- chat

def fake_stream(reply="Heat (1995) rocks.", fail=None):
    async def stream_turn(message, thread_id, model=None):
        if fail:
            raise fail
        yield {"type": "tool_start", "tools": [{"name": "search_shows", "label": "Searching titles"}]}
        yield {"type": "tool_end", "names": ["search_shows"]}
        for word in reply.split(" "):
            yield {"type": "token", "text": word + " "}
        turn = [tool_result("search_shows", {"results": [movie(1, "Heat", year="1995")]}), AIMessage(content=reply)]
        yield {"type": "done", "reply": reply, "turn": turn}
    return stream_turn


def parse_sse(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


def test_chat_stream_creates_thread_and_sends_events_in_order(client, monkeypatch):
    monkeypatch.setattr(agent, "stream_turn", fake_stream())
    resp = client.post("/chat/stream", json={"message": "find me heat"})
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(resp.text)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "thread" and kinds[1] == "status" and kinds[-1] == "done" and kinds[-2] == "cards"
    assert "token" in kinds
    cards = next(e for e in events if e["type"] == "cards")["recommendations"]
    assert cards[0]["title"] == "Heat"
    thread = events[0]["thread"]
    assert thread["title"] == "find me heat"
    assert threads.get(thread["id"])


def test_chat_stream_continues_an_existing_thread(client, monkeypatch):
    monkeypatch.setattr(agent, "stream_turn", fake_stream())
    tid = client.post("/threads").json()["id"]
    events = parse_sse(client.post("/chat/stream", json={"message": "hi", "thread_id": tid}).text)
    assert events[0]["thread"]["id"] == tid
    assert len(client.get("/threads").json()["threads"]) == 1


def test_chat_stream_reports_model_unavailable(client, monkeypatch):
    monkeypatch.setattr(agent, "stream_turn", fake_stream(fail=agent.LLMUnavailableError("quota", 120)))
    events = parse_sse(client.post("/chat/stream", json={"message": "hi"}).text)
    err = events[-1]
    assert err["type"] == "error" and err["kind"] == "quota" and "minute" in err["message"]


def test_chat_stream_reports_unexpected_errors_without_details(client, monkeypatch):
    monkeypatch.setattr(agent, "stream_turn", fake_stream(fail=RuntimeError("secret internals")))
    err = parse_sse(client.post("/chat/stream", json={"message": "hi"}).text)[-1]
    assert err["kind"] == "server" and "secret" not in err["message"]


def test_chat_validation(client):
    assert client.post("/chat/stream", json={"message": "   "}).status_code == 400
    assert client.post("/chat/stream", json={"message": "x" * 4001}).status_code == 400
    assert client.post("/chat/stream", json={"message": "hi", "thread_id": "bad id"}).status_code == 400
    assert client.post("/chat/stream", json={"message": "hi", "model": "gpt-4"}).status_code == 400


def test_plain_chat_endpoint_returns_one_response(client, monkeypatch):
    monkeypatch.setattr(agent, "stream_turn", fake_stream())
    data = client.post("/chat", json={"message": "find me heat"}).json()
    assert data["response"] == "Heat (1995) rocks."
    assert data["recommendations"][0]["title"] == "Heat"
    assert data["conversation_id"] == data["thread"]["id"]


# ---------------------------------------------------------------- library

@pytest.fixture
def fake_details(monkeypatch):
    async def details(tmdb_id, media_type="movie"):
        if tmdb_id == 404:
            raise tmdb.TMDBError("Not found on TMDB (404)")
        return {**movie(tmdb_id, f"Title {tmdb_id}", media_type, "2001"), "backdrop_path": "/b.jpg"}
    monkeypatch.setattr(tmdb, "get_details", details)


def test_favorites_flow_with_title_and_poster_filled_from_tmdb(client, fake_details):
    added = client.post("/favorites", json={"tmdb_id": 7, "media_type": "tv"}).json()
    assert added["success"] and not added["already_existed"] and added["item"]["title"] == "Title 7"
    assert client.post("/favorites", json={"tmdb_id": 7, "media_type": "tv"}).json()["already_existed"] is True
    items = client.get("/favorites").json()
    assert items[0]["poster_url"].endswith("/7.jpg") and items[0]["media_type"] == "tv"
    assert client.get("/library/ids").json()["favorites"] == ["tv:7"]
    assert client.delete("/favorites/7", params={"media_type": "tv"}).json() == {"success": True}
    assert client.get("/favorites").json() == []


def test_favorite_requires_id_and_known_title(client, fake_details):
    assert client.post("/favorites", json={"title": "x"}).status_code == 400
    assert client.post("/favorites", json={"tmdb_id": 404}).status_code == 404
    assert client.post("/favorites", json={"tmdb_id": 404, "title": "Kept"}).json()["success"]


def test_backfill_fills_missing_posters_of_older_rows(client, fake_details):
    database.add_favorite(11, "Title 11", "movie")  # saved without a poster, like version 1 rows
    first = client.get("/favorites").json()[0]
    assert first["poster_url"].endswith("/11.jpg")
    assert database.list_favorites()[0]["poster_path"] == "/11.jpg"


def test_backfill_ignores_a_different_title(client, fake_details):
    database.add_favorite(12, "Something Else Entirely", "movie")
    assert client.get("/favorites").json()[0]["poster_url"] is None


def test_watching_status_flow(client, fake_details):
    assert client.post("/watching", json={"tmdb_id": 3, "status": "plan"}).json()["status"] == "plan"
    assert client.post("/watching", json={"tmdb_id": 3, "status": "bogus"}).status_code == 400
    assert client.patch("/watching/3", json={"status": "watched"}).json() == {"success": True}
    assert client.get("/watching", params={"status": "watched"}).json()[0]["status"] == "watched"
    assert client.get("/watching", params={"status": "plan"}).json() == []
    assert client.patch("/watching/99", json={"status": "plan"}).status_code == 404
    assert client.patch("/watching/3", json={"status": "bogus"}).status_code == 400


def test_review_flow_and_validation(client, fake_details):
    assert client.post("/reviews", json={"tmdb_id": 5, "rating": 11}).status_code == 400
    assert client.post("/reviews", json={"tmdb_id": 5, "rating": 8.5, "comment": "great"}).json()["success"]
    review = client.get("/reviews").json()[0]
    assert review["rating"] == 8.5 and review["comment"] == "great" and review["title"] == "Title 5"
    assert client.get("/library/ids").json()["reviews"] == {"movie:5": 8.5}
    assert client.delete("/reviews/5").json() == {"success": True}


# --------------------------------------------------------------- discover

def test_search_trending_and_genres(client, monkeypatch):
    async def search(q, media_type="all", year=None, page=1):
        return [movie(1, q)]

    async def trending(media_type="all", window="week", page=1):
        return [movie(2, "Hot")]

    async def genres(media_type="movie"):
        return [{"id": 1, "name": "Drama"}]

    monkeypatch.setattr(tmdb, "search", search)
    monkeypatch.setattr(tmdb, "get_trending", trending)
    monkeypatch.setattr(tmdb, "get_genres", genres)
    assert client.get("/search", params={"q": "heat"}).json()["results"][0]["title"] == "heat"
    assert client.get("/search", params={"q": "  "}).json() == {"results": []}
    assert client.get("/search", params={"q": "x", "media_type": "book"}).status_code == 400
    assert client.get("/trending").json()["results"][0]["title"] == "Hot"
    assert client.get("/genres", params={"media_type": "tv"}).json() == {"genres": ["Drama"]}


def test_tmdb_failure_becomes_502(client, monkeypatch):
    async def boom(*args, **kwargs):
        raise tmdb.TMDBError("Could not reach TMDB (ConnectError).")

    monkeypatch.setattr(tmdb, "search", boom)
    resp = client.get("/search", params={"q": "heat"})
    assert resp.status_code == 502 and "TMDB" in resp.json()["detail"]


def test_title_details_include_library_flags(client, fake_details):
    database.add_favorite(8, "Title 8", "movie")
    database.set_watching(8, "Title 8", "movie", status="plan")
    database.save_review(8, 9, "", "movie", "Title 8")
    data = client.get("/titles/movie/8").json()
    assert data["library"] == {"favorite": True, "review": 9.0, "watching": "plan"}
    assert data["backdrop_url"].endswith("/b.jpg")
    assert client.get("/titles/book/8").status_code == 400


def test_health_lists_models(client):
    data = client.get("/health").json()
    assert data["status"] == "ok" and data["models"][0]["name"]
