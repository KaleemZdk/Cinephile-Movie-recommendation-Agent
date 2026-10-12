"""Chat endpoints.

POST /chat/stream sends Server-Sent Events while the answer is written:
    thread   the thread this message belongs to (new threads are created here)
    status   a tool is running, with a short label
    token    a piece of the reply
    cards    poster cards for the titles the reply mentions
    done     the final reply
    error    the turn failed; `message` says why, `kind` says what kind of failure

POST /chat returns the same result as one JSON response, for simple clients.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse

from cinephile.agent import AgentRuntime, LLMUnavailableError
from cinephile.api.deps import RuntimeDep, TurnRegistry, TurnsDep, UserDep
from cinephile.api.presenters import cards_from_turn
from cinephile.api.schemas import ChatRequest
from cinephile.storage import threads

log = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])

EMPTY_REPLY = "I couldn't put together an answer. Try rephrasing."
BUSY_MESSAGE = "This chat is still answering the last message."
SERVER_ERROR_MESSAGE = "Something went wrong while answering. Please try again."


async def run_chat(runtime: AgentRuntime, turns: TurnRegistry, body: ChatRequest, user_id: str) -> AsyncIterator[dict]:
    """Run one turn for a user and yield UI events. Only one turn per thread runs at a time.

    A thread id the user does not own (or that no longer exists) starts a new thread.
    """
    thread = threads.ensure(user_id, body.thread, body.message, body.model)
    thread_id = thread["id"]
    if turns.busy(thread_id):
        yield {"type": "error", "kind": "busy", "message": BUSY_MESSAGE}
        return

    with turns.hold(thread_id):
        yield {"type": "thread", "thread": thread}
        started = time.perf_counter()
        first_token: float | None = None
        try:
            async for event in runtime.stream_turn(body.message, thread_id, body.model, user_id=user_id):
                kind = event["type"]
                if kind == "token":
                    first_token = first_token or time.perf_counter() - started
                    yield event
                elif kind == "tool_start":
                    labels = dict.fromkeys(tool["label"] for tool in event["tools"])
                    yield {"type": "status", "label": " and ".join(labels)}
                elif kind == "done":
                    reply = event["reply"].strip() or EMPTY_REPLY
                    cards = cards_from_turn(event["turn"], reply)
                    yield {"type": "cards", "recommendations": cards}
                    yield {"type": "done", "reply": reply, "thread": threads.touch(user_id, thread_id)}
                    log.info(
                        "turn thread=%s first_token=%.2fs total=%.2fs cards=%d",
                        thread_id[:8],
                        first_token or 0,
                        time.perf_counter() - started,
                        len(cards),
                    )
        except LLMUnavailableError as exc:
            log.warning("turn failed: %s", exc)
            yield {"type": "error", "kind": exc.kind, "message": str(exc)}
        except asyncio.CancelledError:
            log.info("turn cancelled by the client thread=%s", thread_id[:8])
            raise
        except Exception:
            log.exception("Agent run failed")
            yield {"type": "error", "kind": "server", "message": SERVER_ERROR_MESSAGE}


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.post("/chat/stream")
async def chat_stream(body: ChatRequest, runtime: RuntimeDep, turns: TurnsDep, user_id: UserDep) -> StreamingResponse:
    async def events() -> AsyncIterator[str]:
        async for event in run_chat(runtime, turns, body, user_id):
            yield _sse(event)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/chat")
async def chat(body: ChatRequest, runtime: RuntimeDep, turns: TurnsDep, user_id: UserDep) -> dict:
    result: dict = {"response": "", "recommendations": [], "conversation_id": body.thread, "thread": None}
    async for event in run_chat(runtime, turns, body, user_id):
        kind = event["type"]
        if kind == "thread":
            result["thread"] = event["thread"]
            result["conversation_id"] = event["thread"]["id"]
        elif kind == "cards":
            result["recommendations"] = event["recommendations"]
        elif kind == "done":
            result["response"] = event["reply"]
        elif kind == "error":
            if event["kind"] == "busy":
                raise HTTPException(status.HTTP_409_CONFLICT, event["message"])
            result["response"] = event["message"]
            result["error"] = event["kind"]
    return result
