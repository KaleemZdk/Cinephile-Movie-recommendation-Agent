"""
chat.py - Chat endpoints.

POST /chat/stream sends Server-Sent Events as the answer is written:
    thread   the thread this message belongs to (new threads are created here)
    status   a tool is running, with a short label
    token    a piece of the assistant's reply
    cards    poster cards for the titles the reply mentions
    done     the final reply
    error    the turn failed; message explains why

POST /chat returns the same result in one response, for simple clients.
"""

import asyncio
import json
import logging
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import agent
from backend import threads
from backend.cards import cards_from_turn

log = logging.getLogger("chat")
router = APIRouter(tags=["chat"])

MAX_MESSAGE_CHARS = 4000
EMPTY_REPLY = "I couldn't put together an answer. Try rephrasing."

_locks: dict[str, asyncio.Lock] = {}


class ChatIn(BaseModel):
    message: str
    thread_id: str | None = None
    conversation_id: str | None = None  # older name for thread_id
    model: str | None = None


def _check(body: ChatIn) -> tuple[str, str | None]:
    text = body.message.strip()
    if not text:
        raise HTTPException(400, "Message is empty.")
    if len(text) > MAX_MESSAGE_CHARS:
        raise HTTPException(400, f"Message is longer than {MAX_MESSAGE_CHARS} characters.")
    thread_id = body.thread_id or body.conversation_id
    if thread_id and not threads.valid_id(thread_id):
        raise HTTPException(400, "Invalid thread id.")
    if body.model and not body.model.startswith("gemini"):
        raise HTTPException(400, "Unknown model.")
    return text, thread_id


async def run_chat(text: str, thread_id: str | None, model: str | None):
    """Run one turn and yield UI events (dicts). Holds the thread's lock while it runs."""
    thread = threads.ensure(thread_id, text, model)
    thread_id = thread["id"]
    lock = _locks.setdefault(thread_id, asyncio.Lock())
    if lock.locked():
        yield {"type": "error", "kind": "busy", "message": "This chat is still answering the last message."}
        return

    async with lock:
        yield {"type": "thread", "thread": thread}
        started = time.perf_counter()
        first_token = None
        try:
            async for event in agent.stream_turn(text, thread_id, model):
                kind = event["type"]
                if kind == "token":
                    if first_token is None:
                        first_token = time.perf_counter() - started
                    yield event
                elif kind == "tool_start":
                    labels = list(dict.fromkeys(t["label"] for t in event["tools"]))
                    yield {"type": "status", "label": " and ".join(labels)}
                elif kind == "done":
                    reply = event["reply"].strip() or EMPTY_REPLY
                    cards = cards_from_turn(event["turn"], reply)
                    yield {"type": "cards", "recommendations": cards}
                    yield {"type": "done", "reply": reply, "thread": threads.touch(thread_id)}
                    log.info("turn thread=%s first_token=%.2fs total=%.2fs cards=%d",
                             thread_id[:8], first_token or 0, time.perf_counter() - started, len(cards))
        except agent.LLMUnavailableError as exc:
            log.warning("turn failed: %s", exc)
            yield {"type": "error", "kind": exc.kind, "message": str(exc)}
        except asyncio.CancelledError:
            log.info("turn cancelled by client thread=%s", thread_id[:8])
            raise
        except Exception:
            log.exception("Agent run failed")
            yield {"type": "error", "kind": "server", "message": "Something went wrong while answering. Please try again."}


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.post("/chat/stream")
async def chat_stream(body: ChatIn):
    text, thread_id = _check(body)

    async def events():
        async for event in run_chat(text, thread_id, body.model):
            yield _sse(event)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/chat")
async def chat(body: ChatIn):
    text, thread_id = _check(body)
    result = {"response": "", "recommendations": [], "conversation_id": thread_id, "thread": None}
    async for event in run_chat(text, thread_id, body.model):
        if event["type"] == "thread":
            result["thread"] = event["thread"]
            result["conversation_id"] = event["thread"]["id"]
        elif event["type"] == "cards":
            result["recommendations"] = event["recommendations"]
        elif event["type"] == "done":
            result["response"] = event["reply"]
        elif event["type"] == "error":
            if event["kind"] == "busy":
                raise HTTPException(409, event["message"])
            result["response"] = event["message"]
            result["error"] = event["kind"]
    return result
