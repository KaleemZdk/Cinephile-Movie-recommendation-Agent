"""
api.py - FastAPI app for Cinephile.

Run from the project root:
    uvicorn api:app --reload --port 8000
then open http://127.0.0.1:8000

Routes live in backend/:
    chat.py      POST /chat/stream (live answer), POST /chat
    threads.py   /threads  (list, open, rename, pin, delete chats)
    library.py   /favorites /watching /reviews /library/ids
    discover.py  /search /trending /discover /genres /titles/... /for-you

Only chat goes through the agent. Everything else talks to SQLite or TMDB directly, so
it stays fast and does not use model quota.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import agent
from backend import chat, discover, library, threads
from mcp_server import database, tmdb

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("mcp").setLevel(logging.WARNING)
log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    threads.init_threads()
    try:
        await agent.build_agent()  # start the MCP server now so the first chat is not slow
        app.state.agent_ready = True
    except Exception:
        log.exception("Agent failed to start. /chat will retry on the first request.")
        app.state.agent_ready = False
    yield
    await agent.shutdown()
    await tmdb.close()


app = FastAPI(title="Cinephile API", lifespan=lifespan)
app.add_middleware(  # the UI is served from the same origin; this only allows local tools
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(threads.router)
app.include_router(library.router)
app.include_router(discover.router)


class NoCacheStatic(StaticFiles):
    """Serve the frontend without letting the browser reuse stale files while developing."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "agent_ready": getattr(app.state, "agent_ready", False),
        "models": agent.model_status(),
    }


@app.get("/", include_in_schema=False)
async def index():
    page = FRONTEND / "index.html"
    if page.exists():
        return FileResponse(page, headers={"Cache-Control": "no-cache"})
    return {"message": "Cinephile API is running. Put index.html in the frontend/ folder to serve it."}


if FRONTEND.exists():
    app.mount("/assets", NoCacheStatic(directory=FRONTEND), name="frontend")


if __name__ == "__main__":
    uvicorn.run("api:app", host="127.0.0.1", port=8000)
