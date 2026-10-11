"""
api.py — FastAPI layer between the Cinephile frontend (frontend/index.html) and the backend.

Run from the project root:
    uvicorn api:app --reload --port 8000
then open http://127.0.0.1:8000

/chat goes through the LangGraph agent (agent.py -> MCP server).
/search, /favorites, /watching, /reviews and /similar are plain CRUD, so they talk to
database.py and tmdb.py directly instead of paying for an LLM round trip.
"""

import asyncio
import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from groq import RateLimitError
from langchain_core.messages import HumanMessage, ToolMessage
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
from mcp_server import database, tmdb  # noqa: E402  (a real package, so no sys.path tricks)
from agent import build_agent, extract_text, shutdown  # noqa: E402

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("api")

# Same database file the MCP server uses, so the UI and the agent see the same data.
DB_FILE = ROOT / "data" / "movies.db"
DB_FILE.parent.mkdir(exist_ok=True)
database.DB_PATH = DB_FILE


# --------------------------------------------------------------------------- #
# Titles + TMDB details (so cards have titles, posters and genres)
# --------------------------------------------------------------------------- #
# database.py stores no title for reviews, so the titles the UI sends are remembered here.
def _init_titles() -> None:
    conn = database.get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS known_titles (tmdb_id INTEGER PRIMARY KEY, title TEXT NOT NULL)")
    conn.commit()
    conn.close()


def remember_title(tmdb_id: int, title: str | None) -> None:
    if not title:
        return
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO known_titles (tmdb_id, title) VALUES (?, ?) "
        "ON CONFLICT(tmdb_id) DO UPDATE SET title = excluded.title",
        (tmdb_id, title),
    )
    conn.commit()
    conn.close()


def known_titles() -> dict[int, str]:
    conn = database.get_connection()
    rows = conn.execute("SELECT tmdb_id, title FROM known_titles").fetchall()
    conn.close()
    return {r["tmdb_id"]: r["title"] for r in rows}


_details_cache: dict[int, dict] = {}


async def _details(tmdb_id: int) -> dict | None:
    if tmdb_id in _details_cache:
        return _details_cache[tmdb_id]
    try:
        d = await tmdb.get_movie_details(tmdb_id)
    except tmdb.TMDBError:
        return None
    _details_cache[tmdb_id] = d
    return d


async def fill_details(items: list[dict], limit: int = 40) -> list[dict]:
    """Add poster / genres / rating to items that lack them.

    The title check matters: the database has no media type, so a TV show's id could
    match an unrelated movie on TMDB. Details are only used when the titles agree.
    """
    need = [m for m in items[:limit] if not m.get("poster_url")]
    for m, d in zip(need, await asyncio.gather(*(_details(m["tmdb_id"]) for m in need))):
        if not d:
            continue
        if m.get("title") and m["title"].strip().lower() != (d.get("title") or "").strip().lower():
            continue
        for key in ("poster_url", "genres", "rating", "overview", "release_date"):
            if not m.get(key):
                m[key] = d.get(key)
    return items


def ui_movie(d: dict) -> dict:
    """Shape a movie the way the frontend's normalizeMovie() expects."""
    return {
        "id": d.get("tmdb_id"),
        "tmdb_id": d.get("tmdb_id"),
        "title": d.get("title"),
        "release_date": d.get("release_date"),
        "vote_average": d.get("rating"),
        "overview": d.get("overview") or "",
        "poster_path": d.get("poster_url") or d.get("poster_path"),
        "genres": d.get("genres") or [],
        "media_type": "movie",
    }


# --------------------------------------------------------------------------- #
# App setup
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    _init_titles()
    try:
        await build_agent()  # start the MCP server now so the first chat isn't slow
        app.state.agent_ready = True
    except Exception:
        log.exception("Agent failed to start. /chat will retry on the first request.")
        app.state.agent_ready = False
    yield
    await shutdown()


app = FastAPI(title="Cinephile API", lifespan=lifespan)
app.add_middleware(  # open for local development; restrict allow_origins when you deploy
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


@app.get("/", include_in_schema=False)
async def index():
    page = ROOT / "frontend" / "index.html"
    if page.exists():
        return FileResponse(page)
    return {"message": "Cinephile API is running. Put index.html in the frontend/ folder to serve it."}


@app.get("/health")
async def health():
    return {"status": "ok", "agent_ready": getattr(app.state, "agent_ready", False)}


# --------------------------------------------------------------------------- #
# Chat (the LangGraph agent)
# --------------------------------------------------------------------------- #
class ChatIn(BaseModel):
    message: str
    conversation_id: str | None = None


# Tools whose results contain movies worth showing as cards.
MOVIE_TOOLS = {
    "search_movies", "get_movie_details", "get_similar_movies", "get_recommended_movies",
    "get_trending_movies", "discover_movies", "recommend_for_user",
}


def movies_from_turn(turn: list, reply: str) -> list[dict]:
    """Collect movies returned by tools this turn, keeping only those the reply mentions."""
    found: dict[int, tuple[int, dict]] = {}
    for m in turn:
        if not isinstance(m, ToolMessage) or m.name not in MOVIE_TOOLS:
            continue
        try:
            payload = json.loads(extract_text(m.content))
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        items = payload["results"] if "results" in payload else [payload] if "title" in payload else []
        for it in items:
            tid, title = it.get("tmdb_id"), it.get("title")
            if not tid or not title or tid in found:
                continue
            # Case-sensitive on purpose: otherwise the title "Her" would match the word "her".
            hit = re.search(rf"(?<!\w){re.escape(title)}(?!\w)", reply)
            if hit:
                found[tid] = (hit.start(), it)
    return [it for _, it in sorted(found.values(), key=lambda p: p[0])][:6]


@app.post("/chat")
async def chat(body: ChatIn):
    text = body.message.strip()
    if not text:
        raise HTTPException(400, "Message is empty.")
    thread_id = body.conversation_id or uuid.uuid4().hex

    try:
        agent = await build_agent()
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": text}]},
            config={"configurable": {"thread_id": thread_id}},
        )
    except RateLimitError:
        return {
            "response": "I've hit the Groq rate limit. Give it a minute and try again.",
            "recommendations": [],
            "conversation_id": thread_id,
        }
    except Exception as e:
        log.exception("Agent run failed")
        raise HTTPException(502, f"The agent failed ({type(e).__name__}). Check the server log.")

    msgs = result["messages"]
    last_human = max((i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)), default=-1)
    turn = msgs[last_human + 1:]  # only this turn's messages, not the whole thread
    reply = extract_text(msgs[-1].content).strip() or "I couldn't put together an answer. Try rephrasing."

    cards = await fill_details(movies_from_turn(turn, reply))
    return {
        "response": reply,
        "recommendations": [ui_movie(c) for c in cards],
        "conversation_id": thread_id,
    }


# --------------------------------------------------------------------------- #
# Search / similar (TMDB)
# --------------------------------------------------------------------------- #
class SearchIn(BaseModel):
    query: str


@app.post("/search")
async def search(body: SearchIn):
    q = body.query.strip()
    if not q:
        return {"results": []}
    try:
        items = await tmdb.search_movies(q)
    except tmdb.TMDBError as e:
        raise HTTPException(502, str(e))
    items = await fill_details(items, limit=10)
    return {"results": [ui_movie(i) for i in items]}


@app.get("/similar/{tmdb_id}")
async def similar(tmdb_id: int):
    try:
        items = await tmdb.get_recommended_movies(tmdb_id)
    except tmdb.TMDBError as e:
        raise HTTPException(502, str(e))
    items = await fill_details(items, limit=6)
    return {"results": [ui_movie(i) for i in items[:12]]}


# --------------------------------------------------------------------------- #
# Favorites / watching (SQLite)
# --------------------------------------------------------------------------- #
class LibraryIn(BaseModel):
    title: str = ""
    tmdb_id: int | None = None
    show_id: int | None = None


async def _library(kind: str) -> list[dict]:
    rows = database.list_favorites() if kind == "favorites" else database.list_watching()
    items = [{"tmdb_id": r["tmdb_id"], "title": r["title"], "saved_at": r["added_at"]} for r in rows]
    await fill_details(items)
    return [{**ui_movie(i), "saved_at": i["saved_at"]} for i in items]


def _add(kind: str, body: LibraryIn) -> dict:
    tid = body.tmdb_id or body.show_id
    if not tid:
        raise HTTPException(400, "tmdb_id is required.")
    title = body.title.strip() or f"Title {tid}"
    remember_title(tid, title)
    add = database.add_favorite if kind == "favorites" else database.set_watching
    added = add(tid, title)
    return {
        "success": True,
        "already_existed": not added,
        "item": {"id": tid, "tmdb_id": tid, "title": title},
    }


@app.get("/favorites")
async def list_favorites():
    return await _library("favorites")


@app.post("/favorites")
def add_favorite(body: LibraryIn):
    return _add("favorites", body)


@app.delete("/favorites/{tmdb_id}")
def remove_favorite(tmdb_id: int):
    return {"success": database.remove_favorite(tmdb_id)}


@app.get("/watching")
async def list_watching():
    return await _library("watching")


@app.post("/watching")
def add_watching(body: LibraryIn):
    return _add("watching", body)


@app.delete("/watching/{tmdb_id}")
def remove_watching(tmdb_id: int):
    return {"success": database.remove_watching(tmdb_id)}


# --------------------------------------------------------------------------- #
# Reviews (SQLite)
# --------------------------------------------------------------------------- #
class ReviewIn(BaseModel):
    tmdb_id: int | None = None
    show_id: int | None = None
    title: str | None = None
    rating: float
    comment: str = ""
    created_at: str | None = None  # ignored: the database stamps its own time


@app.post("/reviews")
def save_review(body: ReviewIn):
    tid = body.tmdb_id or body.show_id
    if not tid:
        raise HTTPException(400, "tmdb_id is required.")
    try:
        database.save_review(tid, body.rating, body.comment)
    except ValueError as e:
        raise HTTPException(400, str(e))
    remember_title(tid, body.title)
    return {"success": True}


@app.get("/reviews")
async def list_reviews():
    rows = database.list_reviews()
    titles = known_titles()
    for r in database.list_favorites() + database.list_watching():
        titles.setdefault(r["tmdb_id"], r["title"])
    missing = [r["tmdb_id"] for r in rows if r["tmdb_id"] not in titles]
    for tid, d in zip(missing, await asyncio.gather(*(_details(t) for t in missing))):
        if d:
            titles[tid] = d["title"]
    return [
        {
            "id": r["tmdb_id"],
            "tmdb_id": r["tmdb_id"],
            "title": titles.get(r["tmdb_id"], "Movie review"),
            "rating": r["rating"],
            "comment": r["comment"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #
@app.get("/history")
async def history():
    # The frontend keeps chat history in localStorage. Agent memory is saved per
    # conversation_id by the checkpointer, so a conversation continues when it is sent again.
    return []


if __name__ == "__main__":
    uvicorn.run("api:app", host="127.0.0.1", port=8000)