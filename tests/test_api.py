"""HTTP API with a fake agent runtime and a fake TMDB (no network, no model quota)."""

import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from cinephile import tmdb
from cinephile.agent import LLMUnavailableError
from cinephile.api.app import create_app
from cinephile.api.presenters import cards_from_turn, messages_to_ui, ui_item
from cinephile.storage import library, threads
from cinephile.storage.library import TitleRef
from tests.factories import make_item


def tool_result(name, payload, call_id="c1"):
    return ToolMessage(content=json.dumps(payload), tool_call_id=call_id, name=name)


class FakeRuntime:
    """Stands in for AgentRuntime: scripted turns, in-memory thread messages."""

    ready = True

    def __init__(self):
        self.reply = "Heat (1995) rocks."
        self.fail: Exception | None = None
        self.messages = []
        self.deleted = []

    def model_status(self):
        return [{"name": "m1", "available": True, "retry_in": 0}]

    async def start(self):
        return None

    async def shutdown(self):
        return None

    async def thread_messages(self, thread_id):
        return self.messages

    async def delete_thread(self, thread_id):
        self.deleted.append(thread_id)

    async def stream_turn(self, message, thread_id, model=None):
        if self.fail:
            raise self.fail
        yield {"type": "tool_start", "tools": [{"name": "search_shows", "label": "Searching titles"}]}
        yield {"type": "tool_end", "names": ["search_shows"]}
        for word in self.reply.split(" "):
            yield {"type": "token", "text": word + " "}
        turn = [
            tool_result("search_shows", {"results": [make_item(1, "Heat", year="1995")]}),
            AIMessage(content=self.reply),
        ]
        yield {"type": "done", "reply": self.reply, "turn": turn}


@pytest.fixture
def runtime():
    return FakeRuntime()


@pytest.fixture
def client(runtime):
    return TestClient(create_app(runtime))  # no `with`: the lifespan (MCP start) is skipped


def parse_sse(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


# --------------------------------------------------------------- presenters


def test_ui_item_builds_poster_url():
    item = ui_item(make_item(1, "Heat"))
    assert item["poster_url"] == "https://image.tmdb.org/t/p/w342/1.jpg"
    assert item["id"] == 1 and item["media_type"] == "movie"
    assert ui_item({"tmdb_id": 2, "title": "X"})["poster_url"] is None


def test_cards_only_for_titles_the_reply_mentions_in_reply_order():
    results = [make_item(1, "Her"), make_item(2, "Ex Machina"), make_item(3, "Moon")]
    turn = [tool_result("search_shows", {"results": results})]
    cards = cards_from_turn(turn, "Try Moon first, then Ex Machina. I told her about it.")
    assert [c["title"] for c in cards] == ["Moon", "Ex Machina"]  # 'Her' must not match the word 'her'


def test_cards_dedupe_and_accept_single_details_payload():
    details = make_item(5, "Dark", "tv", "2017")
    turn = [tool_result("get_details", details), tool_result("get_details", details, "c2")]
    cards = cards_from_turn(turn, "Dark is great.")
    assert len(cards) == 1 and cards[0]["media_type"] == "tv"


def test_cards_keep_one_card_per_title():
    first = make_item(1, "Severance", "tv", "2022")
    others = [make_item(2, "Severance", "movie", "1989"), make_item(3, "Severance", "movie", "2006")]
    turn = [tool_result("get_similar", {"results": [first]}), tool_result("search_shows", {"results": others}, "c2")]
    cards = cards_from_turn(turn, "Watch Severance tonight.")
    assert [(c["id"], c["media_type"]) for c in cards] == [(1, "tv")]


def test_cards_ignore_library_tools_and_bad_json():
    turn = [
        tool_result("add_favorite", {"item": make_item(1, "Heat")}),
        ToolMessage(content="not json", tool_call_id="c9", name="search_shows"),
    ]
    assert cards_from_turn(turn, "Heat") == []


def test_messages_to_ui_groups_turns_and_hides_retried_failures():
    call = AIMessage(content="", tool_calls=[{"name": "search_shows", "args": {}, "id": "c1"}])
    history = [
        HumanMessage("find heat"),  # failed, then retried with the same text
        HumanMessage("find heat"),
        call,
        tool_result("search_shows", {"results": [make_item(1, "Heat")]}),
        AIMessage(content="Heat (1995) is a great crime film."),
        HumanMessage("and then?"),  # failed, not retried: still shown
    ]
    ui = messages_to_ui(history)
    assert [m["role"] for m in ui] == ["user", "assistant", "user"]
    assert ui[1]["recommendations"][0]["title"] == "Heat"


# ------------------------------------------------------------------ threads


def test_thread_lifecycle(client, runtime):
    runtime.messages = [HumanMessage("hello"), AIMessage(content="hi there")]
    created = client.post("/threads")
    assert created.status_code == 201
    tid = created.json()["id"]
    assert [t["id"] for t in client.get("/threads").json()["threads"]] == [tid]
    assert [m["role"] for m in client.get(f"/threads/{tid}").json()["messages"]] == ["user", "assistant"]
    assert client.patch(f"/threads/{tid}", json={"title": "Sci-fi night"}).json()["title"] == "Sci-fi night"
    assert client.patch(f"/threads/{tid}", json={"pinned": True}).json()["pinned"] is True
    assert client.get("/threads", params={"q": "sci"}).json()["threads"][0]["id"] == tid
    assert client.delete(f"/threads/{tid}").json() == {"success": True}
    assert runtime.deleted == [tid]
    assert client.get(f"/threads/{tid}").status_code == 404
    assert client.delete(f"/threads/{tid}").status_code == 404
    assert client.get("/threads/bad id!").status_code == 404


def test_busy_thread_cannot_be_deleted_or_answered_twice(client):
    tid = client.post("/threads").json()["id"]
    turns = client.app.state.turns
    with turns.hold(tid):
        assert client.delete(f"/threads/{tid}").status_code == 409
        events = parse_sse(client.post("/chat/stream", json={"message": "hi", "thread_id": tid}).text)
        assert events == [
            {"type": "error", "kind": "busy", "message": "This chat is still answering the last message."}
        ]
        assert client.post("/chat", json={"message": "hi", "thread_id": tid}).status_code == 409
    assert not turns.busy(tid)


# --------------------------------------------------------------------- chat


def test_chat_stream_creates_thread_and_sends_events_in_order(client):
    resp = client.post("/chat/stream", json={"message": "  find me heat  "})
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(resp.text)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "thread" and kinds[1] == "status" and kinds[-2:] == ["cards", "done"]
    assert "token" in kinds
    assert events[-2]["recommendations"][0]["title"] == "Heat"
    thread = events[0]["thread"]
    assert thread["title"] == "find me heat"
    assert threads.get(thread["id"])


def test_chat_stream_continues_an_existing_thread(client):
    tid = client.post("/threads").json()["id"]
    events = parse_sse(client.post("/chat/stream", json={"message": "hi", "thread_id": tid}).text)
    assert events[0]["thread"]["id"] == tid
    assert len(client.get("/threads").json()["threads"]) == 1


def test_chat_stream_reports_model_unavailable(client, runtime):
    runtime.fail = LLMUnavailableError("quota", 120)
    err = parse_sse(client.post("/chat/stream", json={"message": "hi"}).text)[-1]
    assert err["type"] == "error" and err["kind"] == "quota" and "minute" in err["message"]


def test_chat_stream_hides_unexpected_error_details(client, runtime):
    runtime.fail = RuntimeError("secret internals")
    err = parse_sse(client.post("/chat/stream", json={"message": "hi"}).text)[-1]
    assert err["kind"] == "server" and "secret" not in err["message"]


@pytest.mark.parametrize(
    "body",
    [
        {"message": "   "},
        {"message": "x" * 4001},
        {"message": "hi", "thread_id": "bad id"},
        {"message": "hi", "model": "gpt-4"},
        {},
    ],
)
def test_chat_validation(client, body):
    assert client.post("/chat/stream", json=body).status_code == 422


def test_plain_chat_endpoint_returns_one_response(client):
    data = client.post("/chat", json={"message": "find me heat"}).json()
    assert data["response"] == "Heat (1995) rocks."
    assert data["recommendations"][0]["title"] == "Heat"
    assert data["conversation_id"] == data["thread"]["id"]


# ------------------------------------------------------------------ library


@pytest.fixture
def fake_details(monkeypatch):
    async def details(tmdb_id, media_type="movie"):
        if tmdb_id == 404:
            raise tmdb.TMDBError("Not found on TMDB (404)")
        return {**make_item(tmdb_id, f"Title {tmdb_id}", media_type, "2001"), "backdrop_path": "/b.jpg"}

    monkeypatch.setattr(tmdb, "get_details", details)


def test_favorites_flow_with_details_filled_from_tmdb(client, fake_details):
    added = client.post("/favorites", json={"tmdb_id": 7, "media_type": "tv"}).json()
    assert added["success"] and not added["already_existed"] and added["item"]["title"] == "Title 7"
    assert client.post("/favorites", json={"tmdb_id": 7, "media_type": "tv"}).json()["already_existed"] is True
    items = client.get("/favorites").json()
    assert items[0]["poster_url"].endswith("/7.jpg") and items[0]["media_type"] == "tv"
    assert client.get("/library/ids").json()["favorites"] == ["tv:7"]
    assert client.delete("/favorites/tv/7").json() == {"success": True}
    assert client.get("/favorites").json() == []


def test_favorite_validation(client, fake_details):
    assert client.post("/favorites", json={"title": "x"}).status_code == 422
    assert client.post("/favorites", json={"tmdb_id": 1, "media_type": "book"}).status_code == 422
    assert client.post("/favorites", json={"tmdb_id": 404}).status_code == 404
    assert client.post("/favorites", json={"tmdb_id": 404, "title": "Kept"}).json()["success"]
    assert client.delete("/favorites/book/1").status_code == 422


def test_backfill_fills_missing_posters_of_older_rows(client, fake_details):
    library.add_favorite(TitleRef(11, "movie", "Title 11"))
    assert client.get("/favorites").json()[0]["poster_url"].endswith("/11.jpg")
    assert library.list_favorites()[0]["poster_path"] == "/11.jpg"


def test_backfill_ignores_a_different_title(client, fake_details):
    library.add_favorite(TitleRef(12, "movie", "Something Else Entirely"))
    assert client.get("/favorites").json()[0]["poster_url"] is None


def test_watching_status_flow(client, fake_details):
    assert client.post("/watching", json={"tmdb_id": 3, "status": "plan"}).json()["status"] == "plan"
    assert client.post("/watching", json={"tmdb_id": 3, "status": "bogus"}).status_code == 422
    assert client.patch("/watching/movie/3", json={"status": "watched"}).json() == {"success": True}
    assert client.get("/watching", params={"status": "watched"}).json()[0]["status"] == "watched"
    assert client.get("/watching", params={"status": "plan"}).json() == []
    assert client.patch("/watching/movie/99", json={"status": "plan"}).status_code == 404
    assert client.patch("/watching/movie/3", json={"status": "bogus"}).status_code == 422
    assert client.delete("/watching/movie/3").json() == {"success": True}


def test_review_flow_and_validation(client, fake_details):
    assert client.post("/reviews", json={"tmdb_id": 5, "rating": 11}).status_code == 422
    assert client.get("/reviews/movie/5").status_code == 404
    assert client.post("/reviews", json={"tmdb_id": 5, "rating": 8.5, "comment": "great"}).json()["success"]
    review = client.get("/reviews").json()[0]
    assert review["rating"] == 8.5 and review["comment"] == "great" and review["title"] == "Title 5"
    assert client.get("/reviews/movie/5").json()["comment"] == "great"
    assert client.get("/library/ids").json()["reviews"] == {"movie:5": 8.5}
    assert client.delete("/reviews/movie/5").json() == {"success": True}


# ----------------------------------------------------------------- discover


def test_search_trending_and_genres(client, monkeypatch):
    async def search(q, media_type="all", year=None, page=1):
        return [make_item(1, q)]

    async def trending(media_type="all", window="week", page=1):
        return [make_item(2, "Hot")]

    async def genres(media_type="movie"):
        return [{"id": 1, "name": "Drama"}]

    monkeypatch.setattr(tmdb, "search", search)
    monkeypatch.setattr(tmdb, "get_trending", trending)
    monkeypatch.setattr(tmdb, "get_genres", genres)
    assert client.get("/search", params={"q": "heat"}).json()["results"][0]["title"] == "heat"
    assert client.get("/search", params={"q": "  "}).json() == {"results": []}
    assert client.get("/search", params={"q": "x", "media_type": "book"}).status_code == 422
    assert client.get("/search", params={"q": "x", "page": 0}).status_code == 422
    assert client.get("/trending").json()["results"][0]["title"] == "Hot"
    assert client.get("/genres", params={"media_type": "tv"}).json() == {"genres": ["Drama"]}


def test_tmdb_failure_becomes_502(client, monkeypatch):
    async def boom(*args, **kwargs):
        raise tmdb.TMDBError("Could not reach TMDB (ConnectError).")

    monkeypatch.setattr(tmdb, "search", boom)
    resp = client.get("/search", params={"q": "heat"})
    assert resp.status_code == 502 and "TMDB" in resp.json()["detail"]


def test_title_details_include_library_flags(client, fake_details):
    ref = TitleRef(8, "movie", "Title 8")
    library.add_favorite(ref)
    library.set_watching(ref, "plan")
    library.save_review(ref, 9)
    data = client.get("/titles/movie/8").json()
    assert data["library"] == {"favorite": True, "watching": "plan", "review": 9.0}
    assert data["backdrop_url"].endswith("/b.jpg")
    assert client.get("/titles/book/8").status_code == 422
    assert client.get("/titles/movie/0").status_code == 422


# ------------------------------------------------------------------- system


def test_health(client):
    data = client.get("/health").json()
    assert data["status"] == "ok" and data["agent_ready"] is True and data["models"][0]["name"] == "m1"


def test_security_headers_on_the_ui_only(client):
    ui = client.get("/")
    assert ui.status_code == 200
    assert "default-src 'self'" in ui.headers["content-security-policy"]
    assert ui.headers["x-content-type-options"] == "nosniff"
    api = client.get("/health")
    assert "content-security-policy" not in api.headers
    assert api.headers["x-frame-options"] == "DENY"
