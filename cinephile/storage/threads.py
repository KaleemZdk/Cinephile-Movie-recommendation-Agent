"""Index of chat threads: owner, title, timestamps, pinned flag.

The messages of each thread live in the LangGraph checkpointer under the thread id;
this table holds what the chat list in the UI needs and who owns each thread. Every
function takes the owner's id, so one user can never read or change another's thread.
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
    data.pop("user_id", None)
    data["pinned"] = bool(data["pinned"])
    return data


def get(user_id: str, thread_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM threads WHERE id = ? AND user_id = ?", (thread_id, user_id)).fetchone()
    return _as_dict(row) if row else None


def create(user_id: str, title: str = DEFAULT_TITLE, model: str | None = None) -> dict:
    """A new thread with a random id (ids are never taken from the client)."""
    thread_id = uuid.uuid4().hex
    now = now_iso()
    with connect() as conn:
        conn.execute(
            "INSERT INTO threads (id, user_id, title, model, pinned, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 0, ?, ?)",
            (thread_id, user_id, make_title(title), model, now, now),
        )
    return get(user_id, thread_id)


def touch(user_id: str, thread_id: str) -> dict | None:
    with connect() as conn:
        conn.execute("UPDATE threads SET updated_at = ? WHERE id = ? AND user_id = ?", (now_iso(), thread_id, user_id))
    return get(user_id, thread_id)


def ensure(user_id: str, thread_id: str | None, first_message: str, model: str | None = None) -> dict:
    """Return the user's thread, or a new one (titled from the first message) if they have no such thread."""
    if thread_id and get(user_id, thread_id):
        return touch(user_id, thread_id)
    return create(user_id, first_message, model)


def list_all(user_id: str, query: str | None = None) -> list[dict]:
    sql = "SELECT * FROM threads WHERE user_id = ?"
    params: list[str] = [user_id]
    if query and query.strip():
        sql += " AND title LIKE ? ESCAPE '\\'"
        escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{escaped}%")
    sql += " ORDER BY pinned DESC, updated_at DESC"
    with connect() as conn:
        return [_as_dict(row) for row in conn.execute(sql, params).fetchall()]


def rename(user_id: str, thread_id: str, title: str) -> dict | None:
    with connect() as conn:
        conn.execute(
            "UPDATE threads SET title = ? WHERE id = ? AND user_id = ?", (make_title(title), thread_id, user_id)
        )
    return get(user_id, thread_id)


def set_pinned(user_id: str, thread_id: str, pinned: bool) -> dict | None:
    with connect() as conn:
        conn.execute("UPDATE threads SET pinned = ? WHERE id = ? AND user_id = ?", (int(pinned), thread_id, user_id))
    return get(user_id, thread_id)


def delete(user_id: str, thread_id: str) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM threads WHERE id = ? AND user_id = ?", (thread_id, user_id)).rowcount > 0
