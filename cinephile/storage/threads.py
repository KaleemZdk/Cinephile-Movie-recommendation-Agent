"""Index of chat threads: title, timestamps, pinned flag.

The messages of each thread live in the LangGraph checkpointer; this table only holds
what the chat list in the UI needs.
"""

from __future__ import annotations

import re
import uuid

from cinephile.storage.db import connect, now_iso

THREAD_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
TITLE_MAX = 60
DEFAULT_TITLE = "New chat"


def valid_id(thread_id: str | None) -> bool:
    return bool(thread_id and THREAD_ID_PATTERN.fullmatch(thread_id))


def make_title(text: str) -> str:
    """A thread title from free text: one line, trimmed at a word boundary."""
    line = " ".join((text or "").split())
    if len(line) <= TITLE_MAX:
        return line or DEFAULT_TITLE
    return line[:TITLE_MAX].rsplit(" ", 1)[0].rstrip(",.;:") + "..."


def _as_dict(row) -> dict:
    data = dict(row)
    data["pinned"] = bool(data["pinned"])
    return data


def get(thread_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
    return _as_dict(row) if row else None


def create(title: str = DEFAULT_TITLE, model: str | None = None, thread_id: str | None = None) -> dict:
    thread_id = thread_id or uuid.uuid4().hex
    now = now_iso()
    with connect() as conn:
        conn.execute(
            "INSERT INTO threads (id, title, model, pinned, created_at, updated_at) VALUES (?, ?, ?, 0, ?, ?)",
            (thread_id, make_title(title), model, now, now),
        )
    return get(thread_id)


def touch(thread_id: str) -> dict | None:
    with connect() as conn:
        conn.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (now_iso(), thread_id))
    return get(thread_id)


def ensure(thread_id: str | None, first_message: str, model: str | None = None) -> dict:
    """Return the thread, creating it (titled from the first message) if it does not exist yet."""
    if thread_id and get(thread_id):
        return touch(thread_id)
    return create(first_message, model, thread_id)


def list_all(query: str | None = None) -> list[dict]:
    sql = "SELECT * FROM threads"
    params: list[str] = []
    if query and query.strip():
        sql += " WHERE title LIKE ? ESCAPE '\\'"
        escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{escaped}%")
    sql += " ORDER BY pinned DESC, updated_at DESC"
    with connect() as conn:
        return [_as_dict(row) for row in conn.execute(sql, params).fetchall()]


def rename(thread_id: str, title: str) -> dict | None:
    with connect() as conn:
        conn.execute("UPDATE threads SET title = ? WHERE id = ?", (make_title(title), thread_id))
    return get(thread_id)


def set_pinned(thread_id: str, pinned: bool) -> dict | None:
    with connect() as conn:
        conn.execute("UPDATE threads SET pinned = ? WHERE id = ?", (int(pinned), thread_id))
    return get(thread_id)


def delete(thread_id: str) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM threads WHERE id = ?", (thread_id,)).rowcount > 0
