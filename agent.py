
import asyncio
import os
import sys
from pathlib import Path

import aiosqlite
import certifi
from dotenv import load_dotenv

load_dotenv()

os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()

from langchain_core.messages import SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition


# --------------------------------------------------------------------------- #
# PATHS
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

MCP_SERVER_PATH = BASE_DIR / "mcp_server" / "server.py"
CHECKPOINT_DB = DATA_DIR / "checkpoints.db"


# --------------------------------------------------------------------------- #
# CONFIGURATION
# --------------------------------------------------------------------------- #

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.8-flash",
)
if not GEMINI_API_KEY:
    raise ValueError(
        "GEMINI_API_KEY is missing. Add it to your .env file."
    )

if not MCP_SERVER_PATH.exists():
    raise FileNotFoundError(
        f"MCP server not found: {MCP_SERVER_PATH}"
    )


# --------------------------------------------------------------------------- #
# SYSTEM PROMPT
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """
You are Cinephile, a friendly movie and TV recommendation assistant.

Recommend titles based on the user's preferences, mood, genres, themes,
pacing, and reviews. Briefly explain why each recommendation fits.

Use available tools for searching titles, finding similar shows, managing
favorites and watchlists, and saving reviews. Never invent search results
or claim an action succeeded without confirmation.

Resolve ambiguous titles before saving them. Avoid spoilers unless asked.
Use conversation history when relevant. Be concise, honest, and conversational.
"""


# --------------------------------------------------------------------------- #
# MODEL HELPERS
# --------------------------------------------------------------------------- #

def get_llm(model_name: str | None = None):
    """
    Create a Google Gemini chat model.

    Examples:
        get_llm()
        get_llm("gemini-2.5-flash")
        get_llm("gemini-2.5-pro")
    """

    model = (model_name or GEMINI_MODEL).strip()

    if not model:
        model = GEMINI_MODEL

    return ChatGoogleGenerativeAI(
        model=model,
        google_api_key=GEMINI_API_KEY,
        temperature=0.3,
        max_retries=4,
    )


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


# --------------------------------------------------------------------------- #
# AGENT STATE
# --------------------------------------------------------------------------- #

_lock = asyncio.Lock()

_tools = None
_checkpointer: AsyncSqliteSaver | None = None

# Cache one graph per Gemini model.
_agents: dict[str, object] = {}

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

async def _get_checkpointer() -> AsyncSqliteSaver:
    """
    Create the asynchronous SQLite checkpointer once.

    It stores conversation state using LangGraph's thread_id.
    """

    global _checkpointer

    if _checkpointer is None:
        conn = await aiosqlite.connect(str(CHECKPOINT_DB))

        try:
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

async def build_agent(model_name: str | None = None):
    """
    Build or retrieve a cached Cinephile graph for a Gemini model.

    If no model is provided, GEMINI_MODEL from .env is used.
    """

    global _tools

    model = (model_name or GEMINI_MODEL).strip()

    if not model:
        model = GEMINI_MODEL

    if model in _agents:
        return _agents[model]

    async with _lock:
        if model in _agents:
            return _agents[model]

        # Start MCP and load tools only once.
        if _tools is None:
            _tools = await _load_mcp_tools()

        # Create the selected Gemini model and bind MCP tools.
        model_llm = get_llm(model).bind_tools(_tools)

        # ---------------- LLM NODE ---------------- #

        async def call_model(state: MessagesState):
            messages = [
                SystemMessage(content=SYSTEM_PROMPT)
            ] + state["messages"]

            response = await model_llm.ainvoke(messages)

            return {"messages": [response]}

        # ---------------- GRAPH ---------------- #

        builder = StateGraph(MessagesState)

        builder.add_node("agent", call_model)
        builder.add_node("tools", ToolNode(_tools))

        builder.add_edge(START, "agent")
        builder.add_conditional_edges(
            "agent",
            tools_condition,
        )
        builder.add_edge("tools", "agent")

        checkpointer = await _get_checkpointer()

        agent = builder.compile(
            checkpointer=checkpointer
        )

        _agents[model] = agent

        return agent


# --------------------------------------------------------------------------- #
# RUN AGENT
# --------------------------------------------------------------------------- #

async def run_agent(
    message: str,
    thread_id: str = "default",
    model_name: str | None = None,
) -> str:
    """
    Run one conversation turn.

    Args:
        message: User's message.
        thread_id: Conversation identifier for persistent history.
        model_name: Optional Gemini model ID.

    Returns:
        The assistant's final text response.
    """

    agent = await build_agent(model_name)

    config = {
        "configurable": {
            "thread_id": thread_id,
        }
    }

    result = await agent.ainvoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": message,
                }
            ]
        },
        config=config,
    )

    return extract_text(result["messages"][-1].content)


# --------------------------------------------------------------------------- #
# SHUTDOWN
# --------------------------------------------------------------------------- #

async def shutdown() -> None:
    """Close the MCP session and SQLite connection."""

    global _checkpointer, _mcp_task, _mcp_stop, _tools

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
    _agents.clear()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

async def _cli() -> None:
    print("Cinephile — Google Gemini Edition")
    print(f"Model: {GEMINI_MODEL}")
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
