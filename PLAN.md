# Cinephile (Movie_Recommendations_AI_Agent): setup, fix and feature plan

## Context

Cinephile is a movie and TV recommendation app. A FastAPI layer (`api.py`) serves a single-page frontend
(`frontend/index.html`) and talks to a LangGraph agent (`agent.py`, Gemini) that calls tools exposed by an MCP
server (`mcp_server/server.py`, TMDB plus a SQLite database for favorites, watching and reviews).

Kaleem built the first working version. The goal now is to work in a separate branch (`ahad-working`) and:

1. Set up the dev environment (venv, requirements, branch) and write project memory (`CLAUDE.md`).
2. Fix three reported problems: chat is very slow, previous chats are not remembered, and there is no way to
   keep different chats (threads).
3. Review and harden Kaleem's seven tools (listed below).
4. Make the site look professional (current UI looks "vibe coded").
5. Add more features.

Decisions already made with the user: frontend stays vanilla JS but is split into proper files (no Node build);
TV shows are supported properly alongside movies.

## What I found (root causes)

### Chat is slow
- `/chat` in `api.py` waits for the whole agent loop (LLM call, tool call, LLM call, ...) and returns once. There
  is no streaming, so the UI shows "Thinking..." for the entire time.
- Gemini thinking is on by default for flash models, and `max_retries=4` can hide long backoffs on rate limits.
- Every TMDB request in `mcp_server/tmdb.py` builds a new `httpx.AsyncClient` (new TLS handshake each call).
- Tools return `json.dumps(indent=2)`, and the full tool JSON stays in the thread history and is re-sent to the
  model on every turn. Tokens grow with each message.
- `tmdb._normalize` omits poster and genres, so after each chat reply `api.fill_details` fires up to 6 extra TMDB
  detail calls (with credits and keywords appended) just to get posters.
- The agent must search first, then act. `add_favorite` needs `tmdb_id` and `title`, so saving a title costs
  extra LLM round trips.
- Not yet measured. Phase 0 adds timing logs so each fix is justified by numbers.

### Previous chats are not remembered
The backend checkpointer works: `data/checkpoints.db` holds 4 threads (`cli` has 68 checkpoints). The loss is in
the frontend and API:
- `state.currentConversationId` lives only in memory, so a page refresh starts a brand new thread.
- Chat messages are never persisted or reloaded. The "history" list stores only the user's first sentences in
  localStorage, and clicking one re-sends it as a new message into the current thread.
- "New Chat" does not reset the conversation. `GET /history` returns `[]`. There is no endpoint to list threads
  or load a thread's messages.
- A failed turn can leave a dangling tool call in the saved state, which then breaks that thread permanently
  with Gemini. Needs repair logic.

### Other bugs and risks found
- `api.py` imports `groq.RateLimitError` and says "Groq rate limit", but the agent uses Gemini. Real Gemini
  rate limits are never caught.
- `agent.py`: if `GEMINI_MODEL=` is set but empty, `os.getenv(..., default)` returns "" and the fallback does not
  apply. The default `gemini-3.8-flash` is unverified; the model must be checked against the Gemini API.
- Movies and TV: the system prompt and UI say "movies and TV", but tmdb.py only calls `/movie/*` endpoints and
  the DB has no `media_type`, so a TV id can collide with an unrelated movie id (api.py already has a workaround).
- The detail panel and Discover page use hard-coded demo data with fabricated TV ids and fake poster URLs.
  "Similar" in the detail panel is static; the real `/similar/{id}` endpoint is never called from the UI.
- Reviews store no title (api.py keeps a `known_titles` side table to compensate).
- Two processes (API and MCP subprocess) write the same SQLite file without WAL or a busy timeout, so
  "database is locked" can occur.
- Repo hygiene: `data/*.db`, `data/*.db-wal`, `data/*.db-shm` and a duplicate `mcp_server/movies.db` are tracked
  in git (user data and binaries). The README is one line.
- `requirements.txt` is a 166-line freeze with many unused packages (chromadb, kubernetes, onnxruntime, tavily,
  groq, pypdf, docx2txt, fastmcp, ...). It includes `uvloop`, which does not install on Windows, so
  `pip install -r requirements.txt` will fail here as-is.
- Frontend: assistant replies render as plain text (raw `**markdown**` shows), upload button does nothing,
  settings page says "LangGraph HTTP server", inline CSS and JS in one 510-line file.

## Kaleem's seven tools: review

The pasted list is the original spec. Current implementation versus spec:

| Spec | Implemented as | Verdict / fix |
|---|---|---|
| `add_favorite(title)` | `add_favorite(tmdb_id, title)` | Works, but forces a prior search. Accept `title` (and optional `media_type`, `year`) and resolve it inside the tool; return candidates if ambiguous. Keep `tmdb_id` as an optional exact override. |
| `list_favorites()` | `list_favorites()` | Returns bare rows. Add media_type, poster, year, saved date; compact JSON. |
| `search_shows(query)` | `search_movies(query, page)` | Movies only, name differs from spec. Add `search_shows` (TMDB `/search/multi`, movie and TV) and keep `search_movies` as an alias. |
| `get_similar(show_id)` | `get_similar_movies(tmdb_id)` | Movies only. Add `get_similar(show_id, media_type)` covering both, using recommendations first and similar as fallback. |
| `set_watching(title)` | `set_watching(tmdb_id, title)` | Same title-resolve fix as favorites. Add status (watching / plan to watch / watched). |
| `list_watching()` | `list_watching()` | Same enrichment as favorites; filter by status. |
| `save_review(show_id, rating, comment)` | `save_review(tmdb_id, rating, comment)` | Works (upsert, rating 0 to 10). Store title and media_type, keep `created_at`, add `updated_at`. Accept `show_id` as the argument name. |

Extras already present (`remove_*`, `get_review`, `list_reviews`, `get_user_profile`, `recommend_for_user`,
`discover_movies`, `list_genres`, `get_trending_movies`) are kept, extended to TV, and descriptions tightened so
the model picks the right tool.

## Plan of work (each phase ends with a commit and a push to `ahad-working`)

### Phase 0: Setup and baseline
1. `git checkout -b ahad-working` from `main`, then `git push -u origin ahad-working`. Note: a git branch name
   cannot contain a space, so "ahad working" becomes `ahad-working`. The remote is
   `KaleemZdk/Movie_Recommendations_AI_Agent`; the `ahad58041` account has push permission (verified).
2. Create the venv: `py -3.13 -m venv .venv` (already ignored by `.gitignore`).
3. `pip install -r requirements.txt` inside the venv. Expected failure on `uvloop`; fix by adding the marker
   `uvloop==0.23.0; sys_platform != "win32"` (smallest change), then re-run.
4. `.env` exists and is git-ignored (all 5 keys are filled; values are never printed or committed).
5. Baseline run: start `uvicorn api:app`, send a few chats, and record timings (LLM time, tool time, card
   enrichment time). Verify the Gemini model name by listing models through the Gemini API.

### Phase 1: Repo hygiene and project memory
- Create `CLAUDE.md` at the repo root: architecture and request flow, run commands (venv, uvicorn, MCP stdio),
  env keys (names only), DB and checkpoint locations, conventions (no emoji, plain commit messages with no AI
  attribution, work only on `ahad-working`), the tool list, and a "current status / next steps" section updated
  as phases complete. Also save a short project memory in the auto-memory folder.
- Save this plan to the repo as `PLAN.md` so it is tracked.
- `git rm --cached` the DB and WAL files and the duplicate `mcp_server/movies.db`; add `data/*.db*` and
  `mcp_server/*.db` to `.gitignore`. The files stay on disk.
- Write a real README (setup, run, env, features) and slim `requirements.txt` to direct dependencies, keeping the
  old freeze as `requirements-lock.txt`.

### Phase 2: Backend foundation (database, TMDB, tools)
- `mcp_server/database.py`: WAL mode and busy timeout; add `media_type` ('movie' or 'tv') plus `title`,
  `poster_path`, `year` to favorites, watching and reviews; UNIQUE on (tmdb_id, media_type); a small
  `schema_version` migration that preserves the 2 existing favorites; watching `status`; reviews `updated_at`.
  Remove the `known_titles` workaround in api.py once titles live in the tables.
- `mcp_server/tmdb.py`: one shared `httpx.AsyncClient` with connection pooling and short in-memory TTL cache;
  include `poster_url`, `genres`, `media_type` in `_normalize`; add TV functions (search/multi, `/tv/{id}`,
  similar and recommendations, trending, discover) and watch-providers and trailer lookup.
- `mcp_server/server.py`: implement the table above; compact JSON (no indent); `resolve_title()` helper that
  returns one confident match or a short candidate list; clear tool descriptions.
- Unit tests for database, title resolution and each tool (pytest, TMDB mocked).

### Phase 3: Speed
- Add per-step timing logs first, then optimize against numbers.
- Streaming: new `POST /chat/stream` (Server-Sent Events) built on `agent.astream(stream_mode=["messages",
  "updates"])`. Emits status events ("Searching TMDB..."), token events, a final `cards` event and `done`. The
  UI renders tokens as they arrive. Keep `/chat` as a non-streaming fallback.
- Gemini: set a low thinking budget, `max_retries=2`, a request timeout, and the verified fast model; make the
  empty-`GEMINI_MODEL` fallback correct; catch real Gemini quota errors (replace the Groq import).
- Context size: compact tool output, truncate long tool results, and trim history with
  `trim_messages`/a summarizing step so threads stay small. Repair dangling tool calls before a turn.
- Cards: poster and genres now come with search results, so remove most `fill_details` calls; keep the details
  cache. Title-based add tools remove a full LLM round trip when saving.
- Target: first visible token well under 2 seconds on a simple message; a tool-using answer streams visibly.

### Phase 4: Threads and persistent memory
- New app table `threads(id, title, created_at, updated_at, model, archived)` (in a separate `data/app.db` or the
  existing DB). The LangGraph checkpointer stays the source of truth for message content.
- Endpoints: `GET /threads`, `POST /threads`, `GET /threads/{id}/messages` (reads checkpointer state and returns
  user and assistant messages plus saved cards), `PATCH /threads/{id}` (rename), `DELETE /threads/{id}` (also
  deletes checkpoints). Auto-title a thread from its first message (cheap, no extra LLM call at first; optional
  short LLM title later).
- Frontend: sidebar thread list (new chat, switch, rename, delete, search), the active thread id persisted in
  localStorage and in the URL (`#/chat/<id>`), messages reloaded from the API on open and on refresh. The
  "Chat History" page is replaced by the sidebar list. The CLI uses the same thread functions.
- Cross-thread memory: the user profile (favorites, reviews) is already shared across threads through the DB;
  the system prompt injects a short taste summary at the start of each new thread.

### Phase 5: Professional UI redesign (vanilla JS, split files)
Structure: `frontend/index.html`, `frontend/css/` (tokens, layout, components), `frontend/js/` as ES modules
(`api.js`, `state.js`, `chat.js`, `threads.js`, `library.js`, `discover.js`, `components.js`), served by FastAPI
`StaticFiles`. API base becomes same-origin (no hard-coded URL).

Design direction: a clean cinematic product UI, not a themed mock-up.
- Design tokens (color, type scale, spacing, radius, elevation) with real dark and light themes and a toggle.
- Layout like a modern chat product: left sidebar with brand, New Chat, thread list and navigation; a centered
  readable chat column; a collapsible details drawer instead of the always-on right column.
- Proper icon set (inline SVG, no glyph characters), one type family with a consistent hierarchy, consistent
  buttons, inputs, modals, toasts and empty states, skeleton loaders, focus states, keyboard support, mobile
  layout, reduced-motion support.
- Chat: markdown rendering (sanitized), streaming caret, tool-status chips, stop and regenerate buttons, copy,
  recommendation cards with poster, year, rating, genres, save and watchlist actions.
- Real data everywhere: the demo movies and fake posters are removed; Discover opens on live trending with genre,
  year and media-type filters; the details drawer loads real details, similar titles, providers and trailer.
- Library pages (Favorites, Watching, Reviews) share one grid component with sort, filter, remove and a proper
  review editor (star rating plus text). Settings: theme, default model, clear data, API health.
- The existing dingbat glyphs are replaced; no emoji are introduced.
- I will use the installed `ui-ux-pro-max` skill for palette, type and component guidance while building this
  phase, and check results in the browser at desktop, tablet and phone widths.

### Phase 6: New features (Tier 1 now, Tier 2 if time allows)
Tier 1:
- Watchlist statuses (plan to watch, watching, watched) with move between states.
- "Where to watch" (TMDB providers by region) and trailer link in the details drawer.
- Mood and filter prompts on Discover and as chat quick actions (genre, era, runtime, rating).
- "Because you liked ..." home rail built from `recommend_for_user`, shown above Discover results.
- Stop generation and regenerate response in chat; edit and delete a thread.
Tier 2:
- Taste profile page (top genres, average rating, charts) and export and import of the library as JSON.
- Document upload to seed taste (the `pypdf` and `docx2txt` dependencies hint this was intended) or remove the
  upload button if dropped.
- Optional per-thread model choice (flash versus pro).

### Phase 7: Quality and wrap-up
- pytest for database, tmdb (mocked), tools and API endpoints; one end-to-end check of the agent against the
  real MCP server.
- Update `CLAUDE.md`, `README.md` and `PLAN.md` with final state; open a pull request from `ahad-working` to
  `main` only when the user asks.

## Git workflow
- All work on `ahad-working`; never commit to `main`.
- Small commits with short plain messages in the project's existing style (for example "threads api added").
  No emoji, no AI attribution or Co-Authored-By lines in commits (per the user's global instructions).
- Push after every phase and after any large step so the branch on GitHub stays current.
- `.env` is never staged; the DB and checkpoint files are untracked after Phase 1.

## Critical files
- `agent.py` (model config, graph, trimming, streaming helpers)
- `api.py` (chat streaming, threads endpoints, cleanup of groq and known_titles code)
- `mcp_server/server.py`, `mcp_server/database.py`, `mcp_server/tmdb.py` (tools, schema, TMDB client)
- `frontend/index.html` replaced by `frontend/` split structure
- New: `CLAUDE.md`, `PLAN.md`, `tests/`, `requirements-lock.txt`; edited: `.gitignore`, `requirements.txt`, `README.md`

Existing code to reuse: `agent.build_agent`/`extract_text`/`shutdown`, `api.movies_from_turn` (card extraction),
`api.ui_movie`, `database.*` CRUD helpers, `tmdb._get` retry logic, and the MCP session runner in `agent.py`.

## Verification
1. Environment: `.venv\Scripts\python -c "import fastapi, langgraph, langchain_google_genai, mcp"` succeeds;
   `uvicorn api:app` starts, `/health` reports `agent_ready: true`.
2. Branch: `git branch -vv` shows `ahad-working` tracking `origin/ahad-working`; GitHub shows the commits.
3. Memory: send a message, refresh the browser, reopen the same thread: all messages and cards return. Restart the
   server: still there. Two threads stay independent; deleting one removes its checkpoints.
4. Speed: compare the Phase 0 timings with the new ones; the first streamed token appears quickly and a tool
   call shows a status chip.
5. Tools: for each of the seven spec tools, call it through the agent in chat and directly through MCP and
   confirm the DB row and the UI update (favorite by title only, ambiguous title returns candidates, review
   rating out of range is rejected, TV and movie with the same id do not collide).
6. UI: check in the browser at 1440, 1024 and 390 px wide, light and dark, keyboard-only navigation, and no
   console errors.
7. `pytest` passes.

## Risks and open items
- Gemini model naming and quota can change; the model is verified at runtime in Phase 0 rather than assumed.
- SQLite schema migration touches Kaleem's existing data (2 favorites); migration is additive and tested.
- Pushing to a repo owned by KaleemZdk: push access is confirmed, but `main` is left untouched until a PR is
  approved.

## Addendum: Phase 0 baseline results (measured 2026-10-11)

Timings from `agent.py` driven directly (no frontend), MCP server started in 2.0 s.

| Model | Turn | Result |
|---|---|---|
| gemini-3.8-flash (configured default) | "Hi" (no tools) | 21.0 s, first token at 18 s |
| gemini-3.8-flash | next request | 429: free tier daily quota is 20 requests per model, retry in about 6 h |
| gemini-3.5-flash-lite | "Hi" (no tools) | 1.15 s |
| gemini-3.5-flash-lite | "Recommend something like Dark" | 7.7 s total, first visible token at 6.4 s |
| gemini-3.5-flash-lite | "What are my current favorites?" | 1.7 s |

Breakdown of the 7.7 s recommendation turn: 4 LLM calls (0.6 to 2.3 s each) and 3 sequential TMDB tool calls (about
1 s each: two `search_movies`, one `get_similar_movies`), all before the first token is shown.

Other model checks (3 calls each, trivial prompt): 3.7-flash 5 to 15 s, 3.6-flash 3 to 7 s, 3.5-flash 3 to 4 s,
3.1-flash-lite 1.6 to 2.9 s, 3.5-flash-lite 0.6 to 1.4 s, gemini-flash-latest 429, gemini-3-flash-preview 15 to 23 s
or 503. `gemini-2.5-flash` is no longer available to this key.

Changes to the plan because of this:
- Phase 3 gets a model strategy: default to `gemini-3.5-flash-lite`, keep an ordered fallback list
  (for example `gemini-3.1-flash-lite`, `gemini-3.5-flash`) used when a model returns 429 or 503, `max_retries=0` or 1
  with a short timeout so a failure falls over in about a second instead of hanging, and a clear in-app message when
  every model is out of quota.
- Drop the `temperature` argument (the lite model ignores it and warns).
- Cut tool round trips: title-based `add_favorite`/`set_watching` and a `search_shows` that returns posters and ids
  in one call, so the model searches once instead of twice.
- Reuse one HTTP client for TMDB and run independent tool calls in parallel.
- Stream tokens and tool status to the UI so the first visible output arrives in about 1 s instead of 6 s.
- `GEMINI_MODEL` in `.env` is currently `gemini-3.8-flash`; switching the default is a local `.env` change plus a
  code default, and does not touch secrets.

## Addendum: progress (2026-10-11)

Phases 0 to 5 are done, most of phase 6 and the test part of phase 7. See the Status section of CLAUDE.md for the
current list, including what is intentionally left (regenerate response, taste profile page, library export and import,
document upload, per-thread model choice).

Measured after the changes (gemini-3.5-flash-lite, real TMDB): plain reply 1.2 s, save or list action about 2.3 s with
one tool call, recommendation turn 4 to 8 s, first words visible as soon as the final model call starts. Before: a plain
"hi" took 21 s on gemini-3.8-flash and the next request failed with a daily quota error.
