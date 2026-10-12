"""A minimal terminal chat for the local user (thread id "cli")."""

from __future__ import annotations

import asyncio

from cinephile.agent.models import LLMUnavailableError
from cinephile.agent.runtime import AgentRuntime, running
from cinephile.config import configure_logging
from cinephile.storage.db import LOCAL_USER, init_db

THREAD_ID = "cli"
EXIT_WORDS = frozenset({"exit", "quit"})


async def main() -> None:
    configure_logging("WARNING")
    init_db()
    runtime = AgentRuntime()
    print("Cinephile")
    print(f"Models: {' -> '.join(runtime.router.chain())}")
    print("Type 'exit' to quit.\n")

    async with running(runtime):
        while True:
            try:
                text = (await asyncio.to_thread(input, "You: ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if text.lower() in EXIT_WORDS:
                break
            if not text:
                continue
            try:
                reply = await runtime.run_turn(text, THREAD_ID, user_id=LOCAL_USER)
            except LLMUnavailableError as exc:
                reply = str(exc)
            print(f"\nCinephile: {reply}\n")
