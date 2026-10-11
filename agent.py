"""
agent.py - Cinephile, a LangGraph agent for movie and TV recommendations.

The graph is agent <-> tools. Tools come from the MCP server (mcp_server/server.py)
over stdio. Chat memory is stored per thread_id by a SQLite checkpointer.

Speed and resilience choices:
  * A short chain of Gemini models. A model that returns 429 or 503 is skipped for a
    while and the next one is tried immediately (no long retry loops).
  * Thinking is switched off where the model allows it; it was the main latency cost.
  * Only a trimmed view of the history is sent to the model. The saved checkpoint
    keeps everything, so a thread can always be reloaded in full.
  * stream_turn() yields tokens and tool status as they happen.
"""

import asyncio
import logging
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

import aiosqlite
import certifi
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()

from langchain_core.messages import (  # noqa: E402
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    trim_messages,
)
from langchain_core.runnables import RunnableConfig  # noqa: E402
from langchain_google_genai import ChatGoogleGenerativeAI  # noqa: E402
from langchain_google_genai.chat_models import (  # noqa: E402
    GoogleAPIError,
    GoogleInvalidRequestError,
    GoogleModelNotFoundError,
    GoogleRateLimitError,
)
from langchain_mcp_adapters.client import MultiServerMCPClient  # noqa: E402
from langchain_mcp_adapters.tools import load_mcp_tools  # noqa: E402
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver  # noqa: E402
from langgraph.graph import START, MessagesState, StateGraph  # noqa: E402
from langgraph.prebuilt import ToolNode, tools_condition  # noqa: E402

from mcp_server import database  # noqa: E402

log = logging.getLogger("agent")


# --------------------------------------------------------------------------- #
# PATHS
# --------------------------------------------------------------------------- #

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

MCP_SERVER_PATH = BASE_DIR / "mcp_server" / "server.py"
CHECKPOINT_DB = DATA_DIR / "checkpoints.db"

if not MCP_SERVER_PATH.exists():
    raise FileNotFoundError(f"MCP server not found: {MCP_SERVER_PATH}")


# --------------------------------------------------------------------------- #
# CONFIGURATION
# --------------------------------------------------------------------------- #

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Primary model first, then the fallbacks, in order. Free-tier quotas are per model,
# so a second model keeps the app usable when the first one runs out.
DEFAULT_MODEL = "gemini-3.5-flash-lite"
DEFAULT_FALLBACKS = "gemini-3.1-flash-lite,gemini-3.5-flash"

GEMINI_MODEL = (os.getenv("GEMINI_MODEL") or DEFAULT_MODEL).strip()
GEMINI_FALLBACK_MODELS = [
    m.strip()
    for m in (os.getenv("GEMINI_FALLBACK_MODELS") or DEFAULT_FALLBACKS).split(",")
    if m.strip()
]

LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "40"))   # seconds for one model call
MAX_HISTORY_MESSAGES = 24                             # messages sent to the model
OLD_TOOL_RESULT_CHARS = 400                           # tool output kept from earlier turns
QUOTA_COOLDOWN = (60, 1800)                           # min, max seconds to skip a model after 429
UNAVAILABLE_COOLDOWN = 45                             # seconds to skip a model after 503 / timeout


# --------------------------------------------------------------------------- #
# SYSTEM PROMPT
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """You are Cinephile, a friendly, knowledgeable movie and TV recommendation assistant.

How to work:
- Recommend titles from the user's mood, genres, themes, pacing and past taste. Say briefly why each one fits.
- Use tools for facts. Never invent titles, ratings, cast or where something streams, and never say a save
  worked unless the tool result says so.
- To favorite, queue, review or remove a title, call that library tool with the title directly. Do not search
  first. If a tool returns needs_clarification, ask the user which candidate they mean.
- Call independent tools together in one step. For open requests prefer recommend_for_user, discover or
  get_trending over many searches.
- Write each recommended title exactly as it appears in the tool results so the app can show its poster card.
- Keep answers short: at most 5 recommendations with one or two lines each. Avoid spoilers unless asked.
- Movies and TV shows are both fine. If a request is unclear, ask one short question.
- Ratings are 0 to 10; convert if the user gives stars. Do not use emoji.
"""


def _profile_summary() -> str:
    """The user's saved titles as a few lines of text, so every thread knows their taste."""
    try:
        favorites = database.list_favorites()[:15]
        watching = database.list_watching()[:10]
        reviews = database.list_reviews()[:12]
    except Exception:
        return ""

    def label(row: dict) -> str:
        year = f" ({row['year']})" if row.get("year") else ""
        return f"{row['title']}{year}"

    lines = []
    if favorites:
        lines.append("Favorites: " + ", ".join(label(r) for r in favorites))
    if watching:
        lines.append("Watch list: " + ", ".join(f"{label(r)} [{r['status']}]" for r in watching))
    if reviews:
        lines.append("Reviews: " + ", ".join(f"{label(r)} {r['rating']:g}/10" for r in reviews))
    if not lines:
        return ""
    return "What the user has saved (do not recommend these again):\n" + "\n".join(lines)


def _system_message() -> SystemMessage:
    parts = [SYSTEM_PROMPT, f"Today is {date.today().isoformat()}."]
    profile = _profile_summary()
    if profile:
        parts.append(profile)
    return SystemMessage(content="\n\n".join(parts))


# --------------------------------------------------------------------------- #
# MESSAGE HELPERS
# --------------------------------------------------------------------------- #

def extract_text(content) -> str:
    """Extract readable text from a model response."""

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts = []

        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)

        return "".join(parts)

    return str(content)


def _repair_tool_calls(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Make every tool call have a matching result, and drop results with no call.

    A turn that was cancelled or crashed can leave an AI message asking for tools with
    no reply, which Gemini rejects for the rest of that thread.
    """
    fixed: list[BaseMessage] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if isinstance(msg, ToolMessage):
            i += 1  # orphan result: its call was not seen
            continue
        fixed.append(msg)
        i += 1
        if isinstance(msg, AIMessage) and msg.tool_calls:
            wanted = {tc["id"]: tc["name"] for tc in msg.tool_calls}
            answered = set()
            while i < len(messages) and isinstance(messages[i], ToolMessage):
                result = messages[i]
                if result.tool_call_id in wanted and result.tool_call_id not in answered:
                    fixed.append(result)
                    answered.add(result.tool_call_id)
                i += 1
            for call_id, name in wanted.items():
                if call_id not in answered:
                    fixed.append(ToolMessage(
                        content="This tool call was interrupted and returned no result.",
                        tool_call_id=call_id,
                        name=name,
                    ))
    return fixed


def _shrink_old_tool_results(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Shorten tool output from earlier turns; the assistant's own reply already summarises it."""
    last_human = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1)
    shrunk = []
    for i, msg in enumerate(messages):
        if isinstance(msg, ToolMessage) and i < last_human:
            text = extract_text(msg.content)
            if len(text) > OLD_TOOL_RESULT_CHARS:
                msg = msg.model_copy(update={"content": text[:OLD_TOOL_RESULT_CHARS] + " ...[older result shortened]"})
        shrunk.append(msg)
    return shrunk


def prepare_messages(history: list[BaseMessage]) -> list[BaseMessage]:
    """The view of a thread that is actually sent to the model."""
    messages = _shrink_old_tool_results(_repair_tool_calls(history))
    if len(messages) > MAX_HISTORY_MESSAGES:
        trimmed = trim_messages(
            messages,
            max_tokens=MAX_HISTORY_MESSAGES,
            token_counter=len,
            strategy="last",
            start_on="human",
            include_system=False,
            allow_partial=False,
        )
        messages = trimmed or messages
    return [_system_message(), *messages]


# --------------------------------------------------------------------------- #
# MODELS
# --------------------------------------------------------------------------- #

class LLMUnavailableError(Exception):
    """Every configured model failed in a way that waiting may fix (quota, overload, timeout)."""

    def __init__(self, kind: str, retry_in: int | None = None):
        self.kind = kind
        self.retry_in = retry_in
        if kind == "quota":
            message = "The Gemini models are out of free quota right now."
        elif kind == "auth":
            message = "The Gemini API key was rejected. Check GEMINI_API_KEY in .env."
        else:
            message = "The Gemini models are overloaded or not responding right now."
        if retry_in and kind != "auth":
            minutes = max(1, round(retry_in / 60))
            message += f" Try again in about {minutes} minute{'s' if minutes != 1 else ''}."
        super().__init__(message)


_llms: dict[str, object] = {}                 # model name -> chat model bound to the tools
_cooldown: dict[str, float] = {}              # model name -> monotonic time it may be tried again
_no_thinking_control: set[str] = set()        # models that reject thinking_budget=0
_no_thinking_control.add("gemini-3.5-flash-lite")


def model_chain(preferred: str | None = None) -> list[str]:
    chain = [preferred.strip()] if preferred and preferred.strip() else []
    chain += [GEMINI_MODEL, *GEMINI_FALLBACK_MODELS]
    return list(dict.fromkeys(chain))


def get_llm(model: str) -> ChatGoogleGenerativeAI:
    """A Gemini chat model that fails fast so the next model in the chain can take over."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing. Add it to your .env file.")
    kwargs = {"model": model, "google_api_key": GEMINI_API_KEY, "max_retries": 0, "timeout": LLM_TIMEOUT}
    if model not in _no_thinking_control:
        kwargs["thinking_budget"] = 0
    return ChatGoogleGenerativeAI(**kwargs)


def classify_error(exc: BaseException) -> str:
    """quota | unavailable | missing | auth | invalid | other"""
    name = type(exc).__name__
    if isinstance(exc, GoogleRateLimitError):
        return "quota"
    if isinstance(exc, GoogleModelNotFoundError):
        return "missing"
    if isinstance(exc, GoogleInvalidRequestError):
        return "invalid"
    if name in ("GoogleAuthenticationError", "GooglePermissionDeniedError"):
        return "auth"
    if isinstance(exc, (GoogleAPIError, TimeoutError, asyncio.TimeoutError)) or "Timeout" in name:
        return "unavailable"
    return "other"


def _retry_after_seconds(message: str) -> float | None:
    """Read 'retryDelay': '21746s' or 'retry in 6h2m26.1s' from a Gemini error."""
    m = re.search(r"retryDelay'?\"?:\s*'?\"?(\d+(?:\.\d+)?)s", message)
    if m:
        return float(m.group(1))
    m = re.search(r"retry in (?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", message)
    if m and any(m.groups()):
        h, mi, s = (float(g) if g else 0.0 for g in m.groups())
        return h * 3600 + mi * 60 + s
    return None


def _start_cooldown(model: str, kind: str, exc: BaseException) -> float:
    if kind == "quota":
        wait = _retry_after_seconds(str(exc)) or QUOTA_COOLDOWN[0]
        wait = min(max(wait, QUOTA_COOLDOWN[0]), QUOTA_COOLDOWN[1])
    else:
        wait = UNAVAILABLE_COOLDOWN
    _cooldown[model] = time.monotonic() + wait
    return wait


def _bound(model: str):
    if model not in _llms:
        _llms[model] = get_llm(model).bind_tools(_tools)
    return _llms[model]


def model_status() -> list[dict]:
    """Each configured model with whether it can be tried now, for the health endpoint."""
    now = time.monotonic()
    return [
        {"name": m, "available": _cooldown.get(m, 0) <= now,
         "retry_in": max(0, int(_cooldown.get(m, 0) - now))}
        for m in model_chain()
    ]


async def invoke_with_fallback(messages: list[BaseMessage], preferred: str | None = None) -> AIMessage:
    """Call the first usable model in the chain; move on quickly when one is out of quota."""
    now = time.monotonic()
    chain = model_chain(preferred)
    ready = [m for m in chain if _cooldown.get(m, 0) <= now]
    # If every model is cooling down, still try the one that recovers first.
    candidates = ready or sorted(chain, key=lambda m: _cooldown.get(m, 0))[:1]

    last_kind = "unavailable"
    for model in candidates:
        started = time.perf_counter()
        for attempt in (1, 2):
            try:
                response = await _bound(model).ainvoke(messages)
            except Exception as exc:  # noqa: BLE001 - classified below
                kind = classify_error(exc)
                if kind == "invalid" and model not in _no_thinking_control and attempt == 1:
                    log.info("%s rejected thinking_budget=0; retrying without it", model)
                    _no_thinking_control.add(model)
                    _llms.pop(model, None)
                    continue
                if kind in ("quota", "unavailable", "missing"):
                    wait = _start_cooldown(model, kind, exc)
                    log.warning("model %s %s after %.1fs; skipping it for %.0fs", model, kind,
                                time.perf_counter() - started, wait)
                    last_kind = kind
                    break
                if kind == "auth":
                    raise LLMUnavailableError("auth") from exc
                raise
            calls = [c["name"] for c in response.tool_calls] if response.tool_calls else []
            log.info("llm model=%s ms=%d tool_calls=%s", model, (time.perf_counter() - started) * 1000, calls)
            return response

    retry_in = None
    pending = [t - time.monotonic() for t in _cooldown.values() if t > time.monotonic()]
    if pending:
        retry_in = int(min(pending))
    raise LLMUnavailableError(last_kind, retry_in)


# --------------------------------------------------------------------------- #
# AGENT STATE
# --------------------------------------------------------------------------- #

_lock = asyncio.Lock()

_tools = None
_checkpointer: AsyncSqliteSaver | None = None
_agent = None

_mcp_task: asyncio.Task | None = None
_mcp_stop: asyncio.Event | None = None


# --------------------------------------------------------------------------- #
# MCP SESSION
# --------------------------------------------------------------------------- #

async def _mcp_session_runner(
    ready: asyncio.Future,
    stop: asyncio.Event,
) -> None:
    """
    Keep one MCP session open while the application is running.

    The MCP session stays in the same task for its entire lifetime.
    """

    client = MultiServerMCPClient(
        {
            "movies": {
                "command": sys.executable,
                "args": [str(MCP_SERVER_PATH)],
                "transport": "stdio",
            }
        }
    )

    try:
        async with client.session("movies") as session:
            tools = await load_mcp_tools(session)

            if not ready.done():
                ready.set_result(tools)

            await stop.wait()

    except Exception as exc:
        if not ready.done():
            ready.set_exception(exc)
        else:
            raise


async def _load_mcp_tools():
    """Start the MCP server once and load its tools."""

    global _mcp_task, _mcp_stop

    loop = asyncio.get_running_loop()

    ready = loop.create_future()
    _mcp_stop = asyncio.Event()

    _mcp_task = asyncio.create_task(
        _mcp_session_runner(ready, _mcp_stop)
    )

    try:
        return await ready
    except Exception:
        if _mcp_stop is not None:
            _mcp_stop.set()

        if _mcp_task is not None:
            try:
                await _mcp_task
            except Exception:
                pass

        _mcp_task = None
        _mcp_stop = None

        raise


# --------------------------------------------------------------------------- #
# SQLITE CHECKPOINTER
# --------------------------------------------------------------------------- #

async def get_checkpointer() -> AsyncSqliteSaver:
    """
    Create the asynchronous SQLite checkpointer once.

    It stores conversation state using LangGraph's thread_id.
    """

    global _checkpointer

    if _checkpointer is None:
        conn = await aiosqlite.connect(str(CHECKPOINT_DB))

        try:
            await conn.execute("PRAGMA journal_mode = WAL")
            checkpointer = AsyncSqliteSaver(conn)
            await checkpointer.setup()
        except Exception:
            await conn.close()
            raise

        _checkpointer = checkpointer

    return _checkpointer


# --------------------------------------------------------------------------- #
# BUILD AGENT
# --------------------------------------------------------------------------- #

async def build_agent():
    """Build (once) and return the Cinephile graph. The model is chosen per request."""

    global _tools, _agent

    if _agent is not None:
        return _agent

    async with _lock:
        if _agent is not None:
            return _agent

        # Start MCP and load tools only once.
        if _tools is None:
            _tools = await _load_mcp_tools()

        async def call_model(state: MessagesState, config: RunnableConfig):
            preferred = (config.get("configurable") or {}).get("model")
            response = await invoke_with_fallback(prepare_messages(state["messages"]), preferred)
            return {"messages": [response]}

        builder = StateGraph(MessagesState)

        builder.add_node("agent", call_model)
        builder.add_node("tools", ToolNode(_tools))

        builder.add_edge(START, "agent")
        builder.add_conditional_edges("agent", tools_condition)
        builder.add_edge("tools", "agent")

        _agent = builder.compile(checkpointer=await get_checkpointer())
        return _agent


# --------------------------------------------------------------------------- #
# THREAD ACCESS
# --------------------------------------------------------------------------- #

def _config(thread_id: str, model: str | None = None) -> dict:
    return {"configurable": {"thread_id": thread_id, "model": model}}


async def get_thread_messages(thread_id: str) -> list[BaseMessage]:
    """Every saved message of a thread, oldest first (empty if the thread is new)."""
    agent = await build_agent()
    state = await agent.aget_state(_config(thread_id))
    return list((state.values or {}).get("messages", []))


async def delete_thread(thread_id: str) -> None:
    """Remove a thread's saved checkpoints."""
    saver = await get_checkpointer()
    await saver.adelete_thread(thread_id)


# --------------------------------------------------------------------------- #
# RUN AGENT
# --------------------------------------------------------------------------- #

_TOOL_LABELS = {
    "search_shows": "Searching titles",
    "search_movies": "Searching titles",
    "get_details": "Reading details",
    "get_similar": "Finding similar titles",
    "get_recommended": "Finding recommendations",
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
    return _TOOL_LABELS.get(name, "Working")


async def stream_turn(message: str, thread_id: str, model: str | None = None):
    """
    Run one conversation turn and yield events as they happen:

        {"type": "token", "text": "..."}                  model text as it is written
        {"type": "tool_start", "tools": [{"name", "label"}]}
        {"type": "tool_end", "names": [...]}
        {"type": "done", "reply": "...", "turn": [messages produced this turn]}

    Raises LLMUnavailableError when every model is out of quota or overloaded.
    """
    agent = await build_agent()
    turn: list[BaseMessage] = []
    reply = ""

    async for mode, chunk in agent.astream(
        {"messages": [{"role": "user", "content": message}]},
        config=_config(thread_id, model),
        stream_mode=["messages", "updates"],
    ):
        if mode == "messages":
            msg, meta = chunk
            if meta.get("langgraph_node") == "agent" and isinstance(msg, AIMessageChunk):
                text = extract_text(msg.content)
                if text:
                    yield {"type": "token", "text": text}
            continue

        for node, update in chunk.items():
            if node not in ("agent", "tools") or not isinstance(update, dict):
                continue
            msgs = update.get("messages", [])
            turn.extend(msgs)
            if node == "agent":
                for m in msgs:
                    if getattr(m, "tool_calls", None):
                        yield {"type": "tool_start",
                               "tools": [{"name": c["name"], "label": tool_label(c["name"])} for c in m.tool_calls]}
                    else:
                        reply = extract_text(m.content)
            else:
                yield {"type": "tool_end", "names": [m.name for m in msgs if isinstance(m, ToolMessage)]}

    yield {"type": "done", "reply": reply, "turn": turn}


async def run_agent(message: str, thread_id: str = "default", model_name: str | None = None) -> str:
    """Run one turn without streaming and return the assistant's final text."""

    reply = ""
    async for event in stream_turn(message, thread_id, model_name):
        if event["type"] == "done":
            reply = event["reply"]
    return reply


# --------------------------------------------------------------------------- #
# SHUTDOWN
# --------------------------------------------------------------------------- #

async def shutdown() -> None:
    """Close the MCP session and SQLite connection."""

    global _checkpointer, _mcp_task, _mcp_stop, _tools, _agent

    if _mcp_stop is not None:
        _mcp_stop.set()

    if _mcp_task is not None:
        try:
            await _mcp_task
        except Exception as exc:
            print(f"MCP shutdown warning: {exc}")

        _mcp_task = None
        _mcp_stop = None

    if _checkpointer is not None:
        await _checkpointer.conn.close()
        _checkpointer = None

    _tools = None
    _agent = None
    _llms.clear()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

async def _cli() -> None:
    print("Cinephile - Google Gemini Edition")
    print(f"Models: {' -> '.join(model_chain())}")
    print("Type 'exit' to quit.\n")

    thread_id = "cli"

    try:
        while True:
            try:
                user = (
                    await asyncio.to_thread(
                        input,
                        "You: ",
                    )
                ).strip()

            except (EOFError, KeyboardInterrupt):
                print()
                break

            if user.lower() in {"exit", "quit"}:
                break

            if not user:
                continue

            try:
                reply = await run_agent(
                    message=user,
                    thread_id=thread_id,
                )

                print(f"\nCinephile: {reply}\n")

            except Exception as exc:
                print(f"\nError: {exc}\n")

    finally:
        await shutdown()


# --------------------------------------------------------------------------- #
# ENTRY POINT
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    asyncio.run(_cli())
