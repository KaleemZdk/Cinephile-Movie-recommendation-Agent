"""Chat threads of the current visitor: list, create, open, rename, pin, delete.

Another visitor's thread is answered with 404, exactly like a thread that does not exist.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from cinephile.api.deps import RuntimeDep, TurnsDep, UserDep
from cinephile.api.presenters import messages_to_ui
from cinephile.api.schemas import ThreadId, ThreadPatch
from cinephile.storage import threads

router = APIRouter(prefix="/threads", tags=["threads"])


def _existing(user_id: str, thread_id: str) -> dict:
    thread = threads.get(user_id, thread_id) if threads.valid_id(thread_id) else None
    if not thread:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That chat does not exist.")
    return thread


@router.get("")
def list_threads(user_id: UserDep, q: str | None = None) -> dict:
    return {"threads": threads.list_all(user_id, q)}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_thread(user_id: UserDep) -> dict:
    return threads.create(user_id)


@router.get("/{thread_id}")
async def read_thread(thread_id: str, runtime: RuntimeDep, user_id: UserDep) -> dict:
    thread = _existing(user_id, thread_id)
    messages = await runtime.thread_messages(thread_id)
    return {"thread": thread, "messages": messages_to_ui(messages)}


@router.patch("/{thread_id}")
def update_thread(thread_id: ThreadId, body: ThreadPatch, user_id: UserDep) -> dict:
    thread = _existing(user_id, thread_id)
    if body.title is not None:
        thread = threads.rename(user_id, thread_id, body.title)
    if body.pinned is not None:
        thread = threads.set_pinned(user_id, thread_id, body.pinned)
    return thread


@router.delete("/{thread_id}")
async def delete_thread(thread_id: str, runtime: RuntimeDep, turns: TurnsDep, user_id: UserDep) -> dict:
    _existing(user_id, thread_id)
    if turns.busy(thread_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "This chat is still answering. Try again when it finishes.")
    threads.delete(user_id, thread_id)
    await runtime.delete_thread(thread_id)
    return {"success": True}
