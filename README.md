# Cinephile

A movie and TV recommendation assistant. Chat with an AI agent about what you feel like watching, search TMDB,
and keep favorites, a watching list and reviews that the agent can read and use to personalize suggestions.

## Architecture

```
browser (frontend/)  ->  FastAPI (api.py)  ->  LangGraph agent (agent.py, Google Gemini)
                                                      |  MCP over stdio
                                                      v
                                       MCP server (mcp_server/server.py)
                                          |                    |
                                  TMDB API (tmdb.py)    SQLite (database.py)
```

- Chat memory: LangGraph SQLite checkpointer in `data/checkpoints.db`, keyed by thread id.
- User library (favorites, watching, reviews): `data/movies.db`.
- Both files are local data and are git-ignored.

## Setup (Windows)

1. Python 3.13 and a TMDB account plus a Google AI Studio (Gemini) key.
2. Create a `.env` file in the project root:

   ```
   GEMINI_API_KEY=...
   GEMINI_MODEL=gemini-3.5-flash-lite
   TMDB_API_KEY=...
   ```

3. Install and run:

   ```
   py -3.13 -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   .venv\Scripts\python -m uvicorn api:app --reload --port 8000
   ```

   Open http://127.0.0.1:8000.

Terminal chat without the web UI: `.venv\Scripts\python agent.py`.

## Agent tools

Search and discovery: `search_movies`, `get_movie_details`, `get_similar_movies`, `get_recommended_movies`,
`get_trending_movies`, `discover_movies`, `list_genres`.

Your library: `add_favorite`, `list_favorites`, `remove_favorite`, `set_watching`, `list_watching`,
`remove_watching`, `save_review`, `get_review`, `list_reviews`, `get_user_profile`, `recommend_for_user`.

## Development

Work happens on the `ahad-working` branch. See `PLAN.md` for the roadmap and `CLAUDE.md` for project notes and
conventions.

## License

See `LICENSE`.
