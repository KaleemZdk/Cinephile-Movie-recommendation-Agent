# Cinephile

A movie and TV recommendation assistant. Chat with an AI agent about what you feel like watching, browse TMDB, and
keep favorites, a watch list and reviews that the agent can read and use to personalize suggestions.

## Features

- Chat that streams its answer as it is written, shows what it is doing (searching, saving), and displays poster cards
  for the titles it recommends.
- Multiple chats (threads): each keeps its own memory, survives a page reload and a server restart, and can be renamed,
  pinned or deleted.
- Movies and TV shows, with details, trailer link and where to stream.
- Favorites, a watch list with statuses (plan to watch, watching, watched) and reviews rated 0 to 10.
- Discover: search, trending, genre and rating filters, and "Picked for you" based on what you saved.
- Ask in plain words: "add Severance to my favorites", "rate Dark 9 out of 10", "put Whiplash on my watch list".
- Light and dark themes, keyboard accessible, works at phone widths.

## Architecture

```
browser (frontend/)  ->  FastAPI (cinephile/api)  ->  agent (cinephile/agent: LangGraph + Gemini)
                                |                              |  MCP over stdio
                                |                              v
                                |                    MCP server (cinephile/mcp_server)
                                |                              |
                                +-------------+----------------+
                                              v
                            cinephile/tmdb.py (TMDB)   cinephile/storage (SQLite)
```

```
cinephile/
  config.py            settings, read once from the environment and .env
  tmdb.py              async TMDB client (pooled connection, cache, title resolution)
  recommend.py         personalized picks from the user's library
  storage/             db.py (schema, migrations), library.py, threads.py
  mcp_server/          server.py (19 MCP tools), __main__.py
  agent/               messages.py, prompts.py, models.py (model fallback), runtime.py, cli.py
  api/                 app.py (factory), schemas.py, presenters.py, deps.py, routes/
frontend/              index.html, css/, js/ (ES modules, no build step), fonts/
tests/                 pytest suites and Node tests for the frontend
```

- Chat memory: LangGraph SQLite checkpointer in `data/checkpoints.db`, keyed by thread id.
- Library, reviews and the chat list: `data/movies.db`. Both are local, git-ignored files.
- The agent tries a short chain of Gemini models and switches at once when one is out of quota.

## Setup

1. Python 3.12 or newer, a TMDB API key, and a Google AI Studio (Gemini) key.
2. Create `.env` in the project root:

   ```
   GEMINI_API_KEY=...
   GEMINI_MODEL=gemini-3.5-flash-lite
   TMDB_API_KEY=...
   ```

   Optional: `GEMINI_FALLBACK_MODELS` (comma separated), `TMDB_API_Read_Access_Token` (preferred over the v3 key),
   `TMDB_REGION` (default US), `LLM_TIMEOUT` (seconds, default 40), `LOG_LEVEL` (default INFO),
   `CINEPHILE_DATA_DIR` (where the SQLite files live), `CINEPHILE_SINGLE_USER` (see below).

   Each browser that opens the app is a separate visitor with its own private library and chats, identified by a
   random id in an HttpOnly cookie. No sign-up is needed; clearing cookies starts a fresh, empty visitor. On a
   machine only you use, `CINEPHILE_SINGLE_USER=true` makes every browser share one library (including data saved
   before visitors existed). Never set it on a public server.

   When hosting (for example on Render), set the keys as environment variables in the host's dashboard and point
   `CINEPHILE_DATA_DIR` at a persistent disk; otherwise saved data and chats are lost on every restart or redeploy.

3. Install and run (Windows commands; on macOS or Linux use `.venv/bin/python`):

   ```
   py -3.13 -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   .venv\Scripts\python -m cinephile
   ```

   Open http://127.0.0.1:8000. Use `--reload` while developing and `--port` to pick another port.

Other entry points:

```
.venv\Scripts\python -m cinephile.agent          terminal chat
.venv\Scripts\python -m cinephile.mcp_server     the MCP server on its own (add --http for HTTP on port 8001)
```

## Development

```
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest                   backend tests (model and TMDB are mocked)
.venv\Scripts\python -m ruff check .             lint
.venv\Scripts\python -m ruff format .            format
node --test "tests/frontend/*.test.mjs"          frontend tests
```

See `CONTRIBUTING.md` for the conventions, `PLAN.md` for the roadmap and measurements, and `CLAUDE.md` for project notes.
Work happens on the `ahad-working` branch.

## Agent tools

Find: `search_shows`, `search_movies`, `get_details`, `get_similar`, `get_trending`, `discover`, `list_genres`.

Library: `add_favorite`, `list_favorites`, `remove_favorite`, `set_watching`, `list_watching`, `remove_watching`,
`save_review`, `get_review`, `list_reviews`, `get_user_profile`, `recommend_for_user`.

Library tools accept a plain title; if it matches several titles the assistant asks which one you mean.

## License

See `LICENSE`. The bundled Inter font is under the SIL Open Font License (`frontend/fonts/OFL.txt`).
