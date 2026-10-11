# Contributing

How the code is organized and the rules it follows. Keep changes consistent with these.

## Layout and layering

```
cinephile/config.py      the only place that reads environment variables
cinephile/storage/       SQLite: schema and migrations (db.py), data access (library.py, threads.py)
cinephile/tmdb.py        the only place that talks to TMDB
cinephile/recommend.py   domain logic built on storage and TMDB
cinephile/mcp_server/    MCP tools: thin wrappers that validate input and call the modules above
cinephile/agent/         LangGraph agent: prompts, history handling, model choice, runtime
cinephile/api/           HTTP layer: request schemas, presenters, routes; no SQL and no TMDB details here
frontend/                static UI, no build step
```

Dependencies point downward only: `api` and `mcp_server` use `agent`, `recommend`, `tmdb` and `storage`; `storage`
and `tmdb` depend on nothing in the project except `config`. Nothing imports from `api`.

## Python

- Python 3.12+, type hints on public functions, `from __future__ import annotations` in modules.
- PEP 8 via ruff: `python -m ruff check .` and `python -m ruff format .` must be clean (config in `pyproject.toml`,
  line length 120). A `noqa` needs a reason after it.
- No module-level mutable state managed with `global`; long-lived resources live in objects (`AgentRuntime`,
  `ModelRouter`, the TMDB transport) or are injected (FastAPI dependencies in `api/deps.py`).
- Validate input at the boundary: pydantic models in `api/schemas.py`, typed query and path parameters. Invalid input
  is a 422 response, missing things are 404, conflicts 409, upstream TMDB failures 502.
- Never put raw exception text from internals into a response; log it and return a plain message.
- Parameterized SQL only. Table names in SQL come from internal constants, never from input.
- Docstrings say why and what, not how. Comments explain non-obvious decisions.

## Frontend

- ES modules, no framework, no inline scripts or styles (the page is served with a strict Content-Security-Policy).
- Build DOM with `h()` from `js/dom.js`; never assign untrusted text to `innerHTML`. Markdown is escaped before it is
  rendered (`js/markdown.js`, covered by tests).
- Colors, spacing and type come from `css/tokens.css`; both themes must keep text contrast at WCAG AA.
- Every interactive element is a real button or link, reachable by keyboard, with a visible focus ring and an
  accessible name. Dialogs trap focus and close with Escape.
- Icons are inline SVG from `js/icons.js`. No emoji anywhere.

## Tests

- `python -m pytest` must pass. Tests never call Gemini or TMDB: use the fakes in the test files and
  `create_app(runtime=FakeRuntime())` for API tests.
- Add a test with every bug fix and every new endpoint or tool.
- `node --test "tests/frontend/*.test.mjs"` for frontend logic.

## Git

- Branch from `main`, work on a feature branch (currently `ahad-working`), open a pull request to merge.
- Small commits with short, plain, lowercase messages describing the change. No emoji.
- Never commit `.env`, anything in `data/`, or other local state.
