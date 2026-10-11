"""Chat threads: list, create, open, rename, pin, delete."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from cinephile.api.deps import RuntimeDep, TurnsDep
from cinephile.api.presenters import messages_to_ui
from cinephile.api.schemas import ThreadId, ThreadPatch
from cinephile.storage import threads

router = APIRouter(prefix="/threads", tags=["threads"])


def _existing(thread_id: str) -> dict:
    thread = threads.get(thread_id) if threads.valid_id(thread_id) else None
    if not thread:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That chat does not exist.")
    return thread


@router.get("")
def list_threads(q: str | None = None) -> dict:
    return {"threads": threads.list_all(q)}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_thread() -> dict:
    return threads.create()


@router.get("/{thread_id}")
async def read_thread(thread_id: str, runtime: RuntimeDep) -> dict:
    thread = _existing(thread_id)
    messages = await runtime.thread_messages(thread_id)
    return {"thread": thread, "messages": messages_to_ui(messages)}


@router.patch("/{thread_id}")
def update_thread(thread_id: ThreadId, body: ThreadPatch) -> dict:
    thread = _existing(thread_id)
    if body.title is not None:
        thread = threads.rename(thread_id, body.title)
    if body.pinned is not None:
        thread = threads.set_pinned(thread_id, body.pinned)
    return thread


@router.delete("/{thread_id}")
async def delete_thread(thread_id: str, runtime: RuntimeDep, turns: TurnsDep) -> dict:
    _existing(thread_id)
    if turns.busy(thread_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "This chat is still answering. Try again when it finishes.")
    threads.delete(thread_id)
    await runtime.delete_thread(thread_id)
    return {"success": True}
