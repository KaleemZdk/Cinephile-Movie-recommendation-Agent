"""FastAPI application factory.

Only chat goes through the agent. Everything else talks to SQLite or TMDB directly, so
it stays fast and uses no model quota.

Run with:  python -m cinephile          (or: uvicorn cinephile.api.app:app --reload)
"""

from __future__ import annotations

import logging
import mimetypes
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from cinephile import __version__, tmdb
from cinephile.agent import AgentRuntime
from cinephile.api.deps import TurnRegistry
from cinephile.api.routes import chat, discover, library, system, threads
from cinephile.api.visitors import visitor_middleware
from cinephile.config import configure_logging, get_settings
from cinephile.storage.db import init_db

log = logging.getLogger(__name__)

mimetypes.add_type("font/woff2", ".woff2")  # not in every platform's registry

# Applied to the web UI only, so the interactive API docs at /docs keep working.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' https://image.tmdb.org data:; style-src 'self'; font-src 'self'; "
    "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)
LOCAL_ORIGINS = r"https?://(localhost|127\.0\.0\.1)(:\d+)?"


class _NoCacheStatic(StaticFiles):
    """Serve the frontend so the browser revalidates instead of using stale files."""

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


async def _security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    path = request.url.path
    if path == "/" or path.startswith("/assets/"):
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    return response


def create_app(runtime: AgentRuntime | None = None) -> FastAPI:
    settings = get_settings()
    configure_logging()
    runtime = runtime or AgentRuntime(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        init_db()
        try:
            await runtime.start()  # start the MCP server now so the first chat is not slow
        except Exception:
            log.exception("The agent failed to start; chat will retry on the first message.")
        yield
        await runtime.shutdown()
        await tmdb.close()

    app = FastAPI(title="Cinephile API", version=__version__, lifespan=lifespan)
    app.state.runtime = runtime
    app.state.turns = TurnRegistry()

    app.middleware("http")(_security_headers)
    app.middleware("http")(visitor_middleware(settings.single_user))
    app.add_middleware(
        CORSMiddleware,  # the UI is same-origin; this only allows local tools
        allow_origin_regex=LOCAL_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    for module in (system, chat, threads, library, discover):
        app.include_router(module.router)

    frontend = settings.frontend_dir
    if frontend.exists():

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(frontend / "index.html", headers={"Cache-Control": "no-cache"})

        app.mount("/assets", _NoCacheStatic(directory=frontend), name="frontend")
    return app


app = create_app()
