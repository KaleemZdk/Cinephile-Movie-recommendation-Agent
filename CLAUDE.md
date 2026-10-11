# CLAUDE.md

Project memory for Cinephile (Movie_Recommendations_AI_Agent). Read this first. Keep the "Status" section current.

## What this is

A movie and TV recommendation chat app.
- `frontend/` single-page web UI (vanilla JS ES modules, no build step), served by FastAPI at `/` and `/assets`.
- `api.py` FastAPI app; routes live in `backend/` (`chat.py` streaming chat, `threads.py` chat threads, `library.py`
  favorites / watch list / reviews, `discover.py` TMDB browsing, `cards.py` UI shaping).
- `agent.py` LangGraph agent (Google Gemini) with a SQLite checkpointer for per-thread memory. Talks to the MCP server
  over stdio.
- `mcp_server/` MCP server (FastMCP): `server.py` tools, `tmdb.py` async TMDB client, `database.py` SQLite, `recommend.py`
  personalized picks (used by the tool and by the `/for-you` endpoint).

Request flow: browser -> `POST /chat/stream` (SSE) -> `agent.stream_turn()` (agent node <-> tools node) -> MCP stdio
subprocess -> TMDB / SQLite. Events: thread, status, token, cards, done, error. Cards come from tool results whose titles
the reply mentions (`backend/cards.py`).

## Run it (Windows, from the project root)

```
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m uvicorn api:app --reload --port 8000     # open http://127.0.0.1:8000
.venv\Scripts\python agent.py                                    # terminal chat (thread id "cli")
.venv\Scripts\python -m pytest                                   # backend tests (no model quota used)
node --test "tests/frontend/*.test.mjs"                          # markdown renderer tests
```

`requirements.txt` lists direct dependencies only. `requirements-lock.txt` is the old full freeze (not installable on
Windows because of `uvloop`; kept for reference).

## Configuration (.env, git-ignored, never commit or print values)

Keys: `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_FALLBACK_MODELS` (optional, comma list), `TMDB_API_KEY`,
`TMDB_API_Read_Access_Token`, `TMDB_REGION` (optional, default US), `GROQ_API_KEY` (legacy, unused).

Gemini facts (measured 2026-10-11, free tier):
- Quotas are per model per day. `gemini-3.8-flash` allowed only 20 requests per day, then 429 for hours.
- Default model is `gemini-3.5-flash-lite` (about 1 s per call); fallbacks `gemini-3.1-flash-lite`, `gemini-3.5-flash`.
- Thinking is the main latency cost on full flash models (17 s vs 2.7 s). `agent.py` sets `thinking_budget=0`, except
  `gemini-3.5-flash-lite` which rejects 0 (see `_no_thinking_control`).
- `agent.py` fails fast: no retries, a model that returns 429/503 is skipped for a while (`_cooldown`), the next model is
  tried at once. `/health` lists model availability. Keep test calls to the real API minimal.

## Data (git-ignored local files, do not commit)

- `data/movies.db`: favorites, watching, reviews (with media_type and posters), and the `threads` index.
  Titles are identified by (tmdb_id, media_type) because TMDB reuses ids between movies and TV.
- `data/checkpoints.db`: LangGraph chat checkpoints keyed by `thread_id`. The UI loads a thread through
  `GET /threads/{id}`; the agent sees a trimmed view of it (`agent.prepare_messages`), the checkpoint keeps everything.

## MCP tools (19)

Find: `search_shows`, `search_movies`, `get_details`, `get_similar`, `get_trending`, `discover`, `list_genres`.
Library: `add_favorite`, `list_favorites`, `remove_favorite`, `set_watching` (status plan/watching/watched),
`list_watching`, `remove_watching`, `save_review`, `get_review`, `list_reviews`, `get_user_profile`, `recommend_for_user`.
Library tools take a plain `title` (optional `media_type`, `year`, or `tmdb_id`) and resolve it themselves; ambiguous
titles return `needs_clarification` with candidates. Kaleem's original spec (add_favorite(title), search_shows(query),
get_similar(show_id), save_review(show_id, rating, comment), ...) is covered.

## Frontend

`frontend/index.html`, `css/` (tokens, base, layout, chat, pages), `js/` (app router, api, store, components, drawer,
threads sidebar, views for chat / discover / library / settings). Hash routes: `#/chat/<id>`, `#/discover`, `#/favorites`,
`#/watching`, `#/reviews`, `#/settings`. Light and dark themes via tokens. No inline JS or inline styles; icons are inline
SVG from `js/icons.js`; DOM is built with `h()` (never innerHTML with untrusted text; markdown is escaped first).

## Conventions

- Work only on branch `ahad-working` (a branch name cannot contain a space). Never commit to `main`. Commit in small
  steps and push to `origin ahad-working` regularly.
- Commit messages: short, plain, lowercase style like the existing history ("threads api added"). No emoji, no
  hashtags, no AI attribution or Co-Authored-By lines.
- No emoji anywhere (code, docs, UI text). Use inline SVG icons in the UI.
- Never write secrets to logs, commits or chat. `.env` stays untracked. Do not `git add` data files.
- Tools return compact JSON (no `indent=2`). Keep MCP stdout clean (stdio transport).
- Do not leave test data in the user's database; clean up what scripts create.

## Plan and status

Full plan and baseline measurements: `PLAN.md`.

Status:
- Phase 0 setup and baseline: done.
- Phase 1 repo hygiene and memory: done.
- Phase 2 backend foundation (DB migration, TV support, TMDB client, tools, tests): done.
- Phase 3 speed (model chain and fallback, no thinking, trimmed history, streaming, compact tool output): done.
  Measured with the lite model: plain reply about 1.2 s, library action about 2.3 s, recommendation turn 4 to 8 s with
  the first words streaming as soon as the last model call starts.
- Phase 4 threads and persistent memory: done (list, open, rename, pin, delete, restore after reload, last thread
  reopened on start).
- Phase 5 professional UI redesign: done (verified in headless Chrome at 1440 px and 390 px, dark and light).
- Phase 6 features: done for watch statuses, where to watch, trailer, personal picks rail, filters, stop generation.
  Not done: regenerate response, taste profile page, library export and import, document upload, per-thread model.
- Phase 7: tests added (63 Python, 7 Node). Open a pull request to `main` only when the user asks.

Known limits: the lite model sometimes makes extra tool calls on open-ended requests; a failed turn leaves the user
message in the thread and Retry re-sends it; chat history is trimmed to the last 24 messages for the model.
