"""
threads.py - Chat threads: a list of conversations the user can switch between.

The LangGraph checkpointer (data/checkpoints.db) holds the messages of each thread.
This module keeps the small index the UI needs: title, timestamps, pinned flag.
"""

import re
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import agent
from backend.cards import messages_to_ui
from mcp_server import database

router = APIRouter(prefix="/threads", tags=["threads"])

THREAD_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
TITLE_MAX = 60
DEFAULT_TITLE = "New chat"


def init_threads() -> None:
    with database.connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS threads (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                model TEXT,
                pinned INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)


def valid_id(thread_id: str) -> bool:
    return bool(THREAD_ID_PATTERN.fullmatch(thread_id or ""))


def make_title(text: str) -> str:
    """A thread title from the first message: one line, trimmed at a word boundary."""
    line = " ".join((text or "").split())
    if len(line) <= TITLE_MAX:
        return line or DEFAULT_TITLE
    return line[:TITLE_MAX].rsplit(" ", 1)[0].rstrip(",.;:") + "..."


def _row(row) -> dict:
    data = dict(row)
    data["pinned"] = bool(data["pinned"])
    return data


def get(thread_id: str) -> dict | None:
    with database.connect() as conn:
        row = conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
    return _row(row) if row else None


def ensure(thread_id: str | None, first_message: str, model: str | None = None) -> dict:
    """Return the thread, creating it (titled from the first message) if it is new."""
    thread_id = thread_id or uuid.uuid4().hex
    existing = get(thread_id)
    if existing:
        return touch(thread_id)
    now = database.current_time()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO threads (id, title, model, pinned, created_at, updated_at) VALUES (?, ?, ?, 0, ?, ?)",
            (thread_id, make_title(first_message), model, now, now),
        )
    return get(thread_id)


def touch(thread_id: str) -> dict | None:
    with database.connect() as conn:
        conn.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (database.current_time(), thread_id))
    return get(thread_id)


def list_all(query: str | None = None) -> list[dict]:
    sql = "SELECT * FROM threads"
    params: list = []
    if query and query.strip():
        sql += " WHERE title LIKE ?"
        params.append(f"%{query.strip()}%")
    sql += " ORDER BY pinned DESC, updated_at DESC"
    with database.connect() as conn:
        return [_row(r) for r in conn.execute(sql, params).fetchall()]


class ThreadPatch(BaseModel):
    title: str | None = None
    pinned: bool | None = None


@router.get("")
def list_threads(q: str | None = None):
    return {"threads": list_all(q)}


@router.post("")
def create_thread():
    now = database.current_time()
    thread_id = uuid.uuid4().hex
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO threads (id, title, model, pinned, created_at, updated_at) VALUES (?, ?, NULL, 0, ?, ?)",
            (thread_id, DEFAULT_TITLE, now, now),
        )
    return get(thread_id)


@router.get("/{thread_id}")
async def read_thread(thread_id: str):
    thread = get(thread_id) if valid_id(thread_id) else None
    if not thread:
        raise HTTPException(404, "That chat does not exist.")
    messages = await agent.get_thread_messages(thread_id)
    return {"thread": thread, "messages": messages_to_ui(messages)}


@router.patch("/{thread_id}")
def update_thread(thread_id: str, body: ThreadPatch):
    if not valid_id(thread_id) or not get(thread_id):
        raise HTTPException(404, "That chat does not exist.")
    if body.title is not None:
        title = make_title(body.title)
        with database.connect() as conn:
            conn.execute("UPDATE threads SET title = ? WHERE id = ?", (title, thread_id))
    if body.pinned is not None:
        with database.connect() as conn:
            conn.execute("UPDATE threads SET pinned = ? WHERE id = ?", (int(body.pinned), thread_id))
    return get(thread_id)


@router.delete("/{thread_id}")
async def delete_thread(thread_id: str):
    if not valid_id(thread_id) or not get(thread_id):
        raise HTTPException(404, "That chat does not exist.")
    with database.connect() as conn:
        conn.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
    await agent.delete_thread(thread_id)
    return {"success": True}
