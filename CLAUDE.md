# CLAUDE.md

Project memory for Cinephile (Movie_Recommendations_AI_Agent). Read this first. Keep the "Status" section current.

## What this is

A movie and TV recommendation chat app.
- `frontend/` single-page web UI, served by FastAPI at `/`.
- `api.py` FastAPI app: chat, search, favorites, watching, reviews, health.
- `agent.py` LangGraph agent (Google Gemini) with a SQLite checkpointer for per-thread chat memory. It talks to the
  MCP server over stdio.
- `mcp_server/` MCP server (FastMCP): `server.py` tools, `tmdb.py` async TMDB client, `database.py` SQLite CRUD.

Request flow: browser -> `POST /chat` -> `agent.build_agent()` graph (agent node <-> tools node) -> MCP stdio
subprocess -> TMDB / SQLite -> reply plus movie cards (`api.movies_from_turn`).

## Run it (Windows, from the project root)

```
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m uvicorn api:app --reload --port 8000     # open http://127.0.0.1:8000
.venv\Scripts\python agent.py                                    # terminal chat (thread id "cli")
.venv\Scripts\python -m pytest                                   # tests
```

`requirements.txt` lists direct dependencies only. `requirements-lock.txt` is the old full freeze (not installable on
Windows because of `uvloop`; kept for reference).

## Configuration (.env, git-ignored, never commit or print values)

Keys: `GEMINI_API_KEY`, `GEMINI_MODEL`, `TMDB_API_KEY`, `TMDB_API_Read_Access_Token`, `GROQ_API_KEY` (legacy, unused).

Gemini notes (measured 2026-10-11 on the free tier):
- Free tier quotas are per model per day. `gemini-3.8-flash` allows 20 requests per day, then returns 429 with a
  retry delay of hours. A tool-using chat turn costs 2 or more requests.
- Full flash models are slow for a trivial prompt (3 to 20 seconds); `gemini-3.5-flash-lite` answers in under 1.5 s.
- `gemini-2.5-flash` is no longer available to this key.
- `agent.py` must fail fast on 429/503 and fall back to another model instead of retrying for a long time.

## Data

- `data/movies.db`: favorites, watching, reviews (shared by the API process and the MCP subprocess).
- `data/checkpoints.db`: LangGraph chat checkpoints, keyed by `thread_id`.
- Both are git-ignored local data. Do not commit them.

## MCP tools

TMDB: `search_movies`, `get_movie_details`, `get_similar_movies`, `get_recommended_movies`, `get_trending_movies`,
`discover_movies`, `list_genres`.
User state: `add_favorite`, `list_favorites`, `remove_favorite`, `set_watching`, `list_watching`, `remove_watching`,
`save_review`, `get_review`, `list_reviews`, `get_user_profile`, `recommend_for_user`.

Original spec from Kaleem (being extended, see PLAN.md): `add_favorite(title)`, `list_favorites()`,
`search_shows(query)`, `get_similar(show_id)`, `set_watching(title)`, `list_watching()`,
`save_review(show_id, rating, comment)`.

## Conventions

- Work only on branch `ahad-working` (a branch name cannot contain a space). Never commit to `main`. Commit in small
  steps and push to `origin ahad-working` regularly.
- Commit messages: short, plain, lowercase style like the existing history ("threads api added"). No emoji, no
  hashtags, no AI attribution or Co-Authored-By lines.
- No emoji anywhere (code, docs, UI text). Use inline SVG icons in the UI.
- Never write secrets to logs, commits or chat. `.env` stays untracked.
- Tools return compact JSON. Do not use `indent=2` in tool output (it costs tokens and time).
- Do not print stdout from the MCP server process other than protocol traffic (stdio transport).

## Plan and status

The full plan is in `PLAN.md`. Phases: 0 setup and baseline, 1 repo hygiene and memory, 2 backend foundation
(DB, TMDB, tools), 3 speed, 4 threads and persistent memory, 5 professional UI redesign, 6 new features,
7 tests and wrap-up.

Status:
- Phase 0: done. Branch `ahad-working` pushed, `.venv` created, requirements installed, baseline measured.
- Phase 1: in progress (CLAUDE.md, PLAN.md, untrack DBs, README).
- Phases 2 to 7: not started.

Known problems being fixed: slow chat (model quota and latency, no streaming, new HTTP client per TMDB call,
oversized tool output), chat memory lost on refresh (frontend never stores the thread id or messages), no thread
list, movies-only TMDB layer, demo data in the UI, Groq leftovers in `api.py`.
