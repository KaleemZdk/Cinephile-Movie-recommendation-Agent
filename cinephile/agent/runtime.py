"""The running agent: MCP tool session, checkpointer, LangGraph graph, and chat turns.

The graph is agent <-> tools. Tools come from the MCP server, started once as a
subprocess and kept open for the life of the app. Chat memory is stored per thread_id
by a SQLite checkpointer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import AsyncIterator

import aiosqlite
from langchain_core.messages import AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from cinephile.agent.messages import extract_text, prepare_history
from cinephile.agent.models import ModelRouter
from cinephile.agent.prompts import system_message
from cinephile.config import PROJECT_ROOT, Settings, get_settings

log = logging.getLogger(__name__)

TOOL_LABELS = {
    "search_shows": "Searching titles",
    "search_movies": "Searching titles",
    "get_details": "Reading details",
    "get_similar": "Finding similar titles",
    "get_trending": "Checking what is trending",
    "discover": "Browsing by genre",
    "list_genres": "Checking genres",
    "recommend_for_user": "Building picks from your taste",
    "get_user_profile": "Reading your library",
    "add_favorite": "Saving to favorites",
    "remove_favorite": "Removing from favorites",
    "list_favorites": "Reading your favorites",
    "set_watching": "Updating your watch list",
    "remove_watching": "Updating your watch list",
    "list_watching": "Reading your watch list",
    "save_review": "Saving your review",
    "get_review": "Reading your review",
    "list_reviews": "Reading your reviews",
}


def tool_label(name: str) -> str:
    return TOOL_LABELS.get(name, "Working")


def _config(thread_id: str, model: str | None = None) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id, "model": model}}


class AgentRuntime:
    """Owns the agent's long-lived resources. Start it once; use it from any request."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.router = ModelRouter(
            self.settings.gemini_model,
            self.settings.gemini_fallback_models,
            self.settings.gemini_api_key,
            self.settings.llm_timeout,
        )
        self._lock = asyncio.Lock()
        self._graph = None
        self._checkpointer: AsyncSqliteSaver | None = None
        self._mcp_task: asyncio.Task | None = None
        self._mcp_stop: asyncio.Event | None = None

    @property
    def ready(self) -> bool:
        return self._graph is not None

    def model_status(self) -> list[dict]:
        return self.router.status()

    # ------------------------------------------------------------------ startup

    async def _mcp_session(self, ready: asyncio.Future, stop: asyncio.Event) -> None:
        """Keep one MCP session open in a single task for its whole lifetime."""
        client = MultiServerMCPClient(
            {
                "cinephile": {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": ["-m", "cinephile.mcp_server"],
                    "cwd": str(PROJECT_ROOT),
                },
            }
        )
        try:
            async with client.session("cinephile") as session:
                tools = await load_mcp_tools(session)
                if not ready.done():
                    ready.set_result(tools)
                await stop.wait()
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)
            else:
                raise

    async def _start_mcp(self) -> list:
        ready = asyncio.get_running_loop().create_future()
        self._mcp_stop = asyncio.Event()
        self._mcp_task = asyncio.create_task(self._mcp_session(ready, self._mcp_stop))
        try:
            return await ready
        except Exception:
            await self._stop_mcp()
            raise

    async def _stop_mcp(self) -> None:
        if self._mcp_stop is not None:
            self._mcp_stop.set()
        if self._mcp_task is not None:
            try:
                await self._mcp_task
            except Exception as exc:  # noqa: BLE001 - shutdown must not fail
                log.warning("MCP shutdown: %s", exc)
        self._mcp_task = None
        self._mcp_stop = None

    async def _open_checkpointer(self) -> AsyncSqliteSaver:
        self.settings.checkpoint_db.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(self.settings.checkpoint_db))
        try:
            await conn.execute("PRAGMA journal_mode = WAL")
            saver = AsyncSqliteSaver(conn)
            await saver.setup()
        except Exception:
            await conn.close()
            raise
        return saver

    def _build_graph(self, tools: list, checkpointer: AsyncSqliteSaver):
        router = self.router

        async def call_model(state: MessagesState, config: RunnableConfig) -> dict:
            preferred = (config.get("configurable") or {}).get("model")
            messages = [system_message(), *prepare_history(state["messages"])]
            return {"messages": [await router.invoke(messages, preferred)]}

        builder = StateGraph(MessagesState)
        builder.add_node("agent", call_model)
        builder.add_node("tools", ToolNode(tools))
        builder.add_edge(START, "agent")
        builder.add_conditional_edges("agent", tools_condition)
        builder.add_edge("tools", "agent")
        return builder.compile(checkpointer=checkpointer)

    async def start(self):
        """Start the MCP server and build the graph (once). Returns the compiled graph."""
        if self._graph is not None:
            return self._graph
        async with self._lock:
            if self._graph is None:
                tools = await self._start_mcp()
                self.router.bind_tools(tools)
                self._checkpointer = await self._open_checkpointer()
                self._graph = self._build_graph(tools, self._checkpointer)
        return self._graph

    async def shutdown(self) -> None:
        await self._stop_mcp()
        if self._checkpointer is not None:
            await self._checkpointer.conn.close()
            self._checkpointer = None
        self._graph = None

    # ------------------------------------------------------------------ threads

    async def thread_messages(self, thread_id: str) -> list[BaseMessage]:
        """Every saved message of a thread, oldest first (empty for a new thread)."""
        graph = await self.start()
        state = await graph.aget_state(_config(thread_id))
        return list((state.values or {}).get("messages", []))

    async def delete_thread(self, thread_id: str) -> None:
        await self.start()
        await self._checkpointer.adelete_thread(thread_id)

    # ------------------------------------------------------------------ turns

    async def stream_turn(self, message: str, thread_id: str, model: str | None = None) -> AsyncIterator[dict]:
        """Run one turn and yield events as they happen:

            {"type": "token", "text": "..."}
            {"type": "tool_start", "tools": [{"name": ..., "label": ...}]}
            {"type": "tool_end", "names": [...]}
            {"type": "done", "reply": "...", "turn": [messages produced this turn]}

        Raises LLMUnavailableError when every model is out of quota or overloaded.
        """
        graph = await self.start()
        turn: list[BaseMessage] = []
        reply = ""
        async for mode, chunk in graph.astream(
            {"messages": [{"role": "user", "content": message}]},
            config=_config(thread_id, model),
            stream_mode=["messages", "updates"],
        ):
            if mode == "messages":
                piece, meta = chunk
                is_model_text = meta.get("langgraph_node") == "agent" and isinstance(piece, AIMessageChunk)
                if is_model_text and (text := extract_text(piece.content)):
                    yield {"type": "token", "text": text}
                continue

            for node, update in chunk.items():
                if node not in ("agent", "tools") or not isinstance(update, dict):
                    continue
                produced = update.get("messages", [])
                turn.extend(produced)
                if node == "tools":
                    yield {"type": "tool_end", "names": [m.name for m in produced if isinstance(m, ToolMessage)]}
                    continue
                for m in produced:
                    if m.tool_calls:
                        yield {
                            "type": "tool_start",
                            "tools": [{"name": c["name"], "label": tool_label(c["name"])} for c in m.tool_calls],
                        }
                    else:
                        reply = extract_text(m.content)
        yield {"type": "done", "reply": reply, "turn": turn}

    async def run_turn(self, message: str, thread_id: str, model: str | None = None) -> str:
        """Run one turn without streaming and return the final reply."""
        reply = ""
        async for event in self.stream_turn(message, thread_id, model):
            if event["type"] == "done":
                reply = event["reply"]
        return reply


@contextlib.asynccontextmanager
async def running(runtime: AgentRuntime) -> AsyncIterator[AgentRuntime]:
    """Start a runtime for the duration of a block (used by the CLI)."""
    await runtime.start()
    try:
        yield runtime
    finally:
        await runtime.shutdown()
