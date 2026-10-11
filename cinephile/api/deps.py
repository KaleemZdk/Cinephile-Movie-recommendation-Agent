"""Shared request dependencies (the agent runtime and the registry of running turns)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import Depends, Request

from cinephile.agent import AgentRuntime


class ThreadBusyError(Exception):
    """The thread is already answering a message."""


class TurnRegistry:
    """Threads with a reply in progress. One turn per thread at a time."""

    def __init__(self) -> None:
        self._active: set[str] = set()

    def busy(self, thread_id: str) -> bool:
        return thread_id in self._active

    @contextmanager
    def hold(self, thread_id: str) -> Iterator[None]:
        if thread_id in self._active:
            raise ThreadBusyError(thread_id)
        self._active.add(thread_id)
        try:
            yield
        finally:
            self._active.discard(thread_id)


def get_runtime(request: Request) -> AgentRuntime:
    return request.app.state.runtime


def get_turns(request: Request) -> TurnRegistry:
    return request.app.state.turns


RuntimeDep = Annotated[AgentRuntime, Depends(get_runtime)]
TurnsDep = Annotated[TurnRegistry, Depends(get_turns)]
