# Cinephile

A movie and TV recommendation assistant. Chat with an AI agent about what you feel like watching, browse TMDB, and
keep favorites, a watch list and reviews that the agent can read and use to personalize suggestions.

## Features

- Chat that streams its answer as it is written, shows what it is doing (searching, saving), and displays poster cards
  for the titles it recommends.
- Multiple chats (threads): each one keeps its own memory, survives a page reload and a server restart, and can be
  renamed, pinned or deleted.
- Movies and TV shows, with details, trailer link and where to stream.
- Favorites, a watch list with statuses (plan to watch, watching, watched) and reviews with a 0 to 10 rating.
- Discover: search, trending, genre and rating filters, and "Picked for you" based on what you saved.
- You can ask the assistant to do the same things in plain words: "add Severance to my favorites", "rate Dark 9 out
  of 10", "put Whiplash on my watch list".
- Light and dark themes, keyboard friendly, works on phone widths.

## Architecture

```
browser (frontend/)  ->  FastAPI (api.py, backend/)  ->  LangGraph agent (agent.py, Google Gemini)
                                                               |  MCP over stdio
                                                               v
                                                MCP server (mcp_server/server.py)
                                                   |                    |
                                           TMDB API (tmdb.py)    SQLite (database.py)
```

- Chat memory: LangGraph SQLite checkpointer in `data/checkpoints.db`, keyed by thread id.
- Library, reviews and the chat list: `data/movies.db`.
- Both files are local data and are git-ignored.
- The agent tries a short chain of Gemini models and switches immediately when one is out of quota.

## Setup (Windows)

1. Python 3.13, a TMDB account (API key) and a Google AI Studio (Gemini) key.
2. Create a `.env` file in the project root:

   ```
   GEMINI_API_KEY=...
   GEMINI_MODEL=gemini-3.5-flash-lite
   TMDB_API_KEY=...
   ```

   Optional: `GEMINI_FALLBACK_MODELS` (comma separated) and `TMDB_REGION` (default US).

3. Install and run:

   ```
   py -3.13 -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   .venv\Scripts\python -m uvicorn api:app --reload --port 8000
   ```

   Open http://127.0.0.1:8000.

Terminal chat without the web UI: `.venv\Scripts\python agent.py`.

## Tests

```
.venv\Scripts\python -m pytest                    # backend; mocks the model and TMDB
node --test "tests/frontend/*.test.mjs"           # markdown renderer
```

## Agent tools

Find: `search_shows`, `search_movies`, `get_details`, `get_similar`, `get_trending`, `discover`, `list_genres`.

Your library: `add_favorite`, `list_favorites`, `remove_favorite`, `set_watching`, `list_watching`, `remove_watching`,
`save_review`, `get_review`, `list_reviews`, `get_user_profile`, `recommend_for_user`.

Library tools accept a plain title; if it matches several titles the assistant asks which one you mean.

## Development

Work happens on the `ahad-working` branch. See `PLAN.md` for the roadmap and measurements, and `CLAUDE.md` for project
notes and conventions.

## License

See `LICENSE`.
