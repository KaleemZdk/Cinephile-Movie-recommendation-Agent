# CLAUDE.md

Project memory for Cinephile (Movie_Recommendations_AI_Agent). Read this first, then `CONTRIBUTING.md` for the coding
rules. Keep the "Status" section current.

## What this is

A movie and TV recommendation chat app, packaged as `cinephile/`:
- `config.py` settings (only place that reads env vars). `tmdb.py` TMDB client. `recommend.py` personal picks.
- `storage/` SQLite: `db.py` schema and migrations (schema version 2, plus the `threads` table), `library.py`
  favorites / watch list / reviews (`TitleRef` dataclass), `threads.py` chat index.
- `mcp_server/` 19 MCP tools (`server.py`), started by the agent as `python -m cinephile.mcp_server` over stdio.
- `agent/` `runtime.py` (`AgentRuntime`: MCP session, checkpointer, graph, `stream_turn`), `models.py`
  (`ModelRouter`: Gemini chain, cooldowns, no-thinking), `messages.py` (repair and trim history), `prompts.py`.
- `api/` `app.py` (`create_app(runtime)` factory, security headers, static UI), `schemas.py` (pydantic validation),
  `presenters.py` (cards and thread messages for the UI), `deps.py` (runtime and `TurnRegistry` dependencies),
  `routes/` chat, threads, library, discover, system.
- `frontend/` vanilla JS ES modules, self-hosted Inter font, served at `/` and `/assets`.

Request flow: browser -> `POST /chat/stream` (SSE) -> `AgentRuntime.stream_turn()` (agent node <-> tools node) -> MCP
subprocess -> TMDB / SQLite. Events: thread, status, token, cards, done, error.

## Run it (Windows, from the project root)

```
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m cinephile [--reload] [--port 8000]   # web app, http://127.0.0.1:8000
.venv\Scripts\python -m cinephile.agent                      # terminal chat (thread id "cli")
.venv\Scripts\python -m pytest                               # 83 tests, no network
.venv\Scripts\python -m ruff check . ; .venv\Scripts\python -m ruff format --check .
node --test "tests/frontend/*.test.mjs"                      # markdown renderer
```

The old entry points `uvicorn api:app` and `python agent.py` no longer exist.

## Configuration (.env, git-ignored, never commit or print values)

Keys: `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_FALLBACK_MODELS`, `TMDB_API_KEY`, `TMDB_API_Read_Access_Token`,
`TMDB_REGION`, `LLM_TIMEOUT`, `LOG_LEVEL`, `CINEPHILE_DATA_DIR`, `MOVIES_DB_PATH`. `GROQ_API_KEY` is legacy and unused.

Gemini facts (measured 2026-10-11, free tier):
- Quotas are per model per day; `gemini-3.8-flash` allowed only 20 requests a day.
- Default `gemini-3.5-flash-lite` (about 1 s per call); fallbacks `gemini-3.1-flash-lite`, `gemini-3.5-flash`.
- Thinking is the main latency cost (17 s vs 2.7 s). `thinking_budget=0` except for models that reject it
  (`REJECTS_THINKING_BUDGET` in `agent/models.py`; others are detected at runtime).
- No retries; a model returning 429/503 is skipped for a while. `/health` shows model availability. Keep real API
  calls in testing to a minimum.

## Data (local, git-ignored)

- `data/movies.db`: library (titles keyed by tmdb_id + media_type) and the `threads` index.
- `data/checkpoints.db`: LangGraph checkpoints by `thread_id`. The model sees a trimmed view; the checkpoint keeps all.
- `data/legacy-mcp_server-movies.db`: an old unused duplicate from the first version, kept only in case it is needed.

## API conventions

Library items are addressed as `/{media_type}/{tmdb_id}` (`/favorites/tv/70523`, `/watching/movie/27205`,
`/reviews/movie/27205`, `/titles/tv/70523`). Validation errors are 422, missing 404, busy thread 409, TMDB down 502.
The UI pages get a strict Content-Security-Policy (no inline scripts or styles, images only from TMDB).

## Conventions

- Work only on branch `ahad-working`. Never commit to `main`. Commit in small steps and push regularly.
- Commit messages: short, plain, lowercase. No emoji, no hashtags, no AI attribution or Co-Authored-By lines.
- No emoji anywhere (code, docs, UI text).
- Never write secrets to logs, commits or chat. Do not `git add` `.env` or `data/`.
- Do not leave test data in the user's database; clean up what scripts create.
- The user may be running their own server on port 8000; use another port (for example 8010) for test servers and do
  not stop processes you did not start.

## Plan and status

Full plan and measurements: `PLAN.md`.

- Phases 0 to 5 done (setup, hygiene, backend, speed, threads, UI redesign).
- Phase 6 mostly done (watch statuses, where to watch, trailer, personal picks, filters, stop generation).
  Not done: regenerate response, taste profile page, library export and import, document upload, per-thread model.
- Structure pass (2026-10-12): code moved into the `cinephile` package with layered modules, ruff lint and format
  clean, pydantic validation, dependency injection, security headers and CSP, self-hosted font, accessibility fixes,
  83 Python tests and 7 Node tests. Verified in headless Chrome and with live Gemini and TMDB.
- Open a pull request to `main` only when the user asks.

Known limits: the lite model sometimes makes extra tool calls on open-ended requests; chat history sent to the model is
capped at the last 24 messages.
