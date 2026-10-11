"""Preparing a saved thread for the model.

The checkpoint keeps every message. Only a repaired, trimmed view is sent to the model:
earlier tool output is shortened and history is capped, which keeps calls fast.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage, trim_messages

MAX_HISTORY_MESSAGES = 24  # messages sent to the model
OLD_TOOL_RESULT_CHARS = 400  # tool output kept from earlier turns
INTERRUPTED_RESULT = "This tool call was interrupted and returned no result."


def extract_text(content: object) -> str:
    """Readable text from a message's content (a string or a list of content blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content)


def repair_tool_calls(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Give every tool call a matching result and drop results that have no call.

    A turn that was cancelled or crashed can leave an AI message asking for tools with no
    answer, which Gemini rejects for the rest of that thread.
    """
    fixed: list[BaseMessage] = []
    i = 0
    while i < len(messages):
        message = messages[i]
        i += 1
        if isinstance(message, ToolMessage):
            continue  # orphan result: its call was not seen
        fixed.append(message)
        if not (isinstance(message, AIMessage) and message.tool_calls):
            continue
        wanted = {call["id"]: call["name"] for call in message.tool_calls}
        answered: set[str] = set()
        while i < len(messages) and isinstance(messages[i], ToolMessage):
            result = messages[i]
            if result.tool_call_id in wanted and result.tool_call_id not in answered:
                fixed.append(result)
                answered.add(result.tool_call_id)
            i += 1
        fixed.extend(
            ToolMessage(content=INTERRUPTED_RESULT, tool_call_id=call_id, name=name)
            for call_id, name in wanted.items()
            if call_id not in answered
        )
    return fixed


def shrink_old_tool_results(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Shorten tool output from earlier turns; the assistant's reply already summarises it."""
    last_human = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1)
    shrunk = []
    for i, message in enumerate(messages):
        if isinstance(message, ToolMessage) and i < last_human:
            text = extract_text(message.content)
            if len(text) > OLD_TOOL_RESULT_CHARS:
                message = message.model_copy(  # noqa: PLW2901 - replacing the item is the point
                    update={"content": text[:OLD_TOOL_RESULT_CHARS] + " ...[older result shortened]"}
                )
        shrunk.append(message)
    return shrunk


def prepare_history(history: list[BaseMessage]) -> list[BaseMessage]:
    """The part of a thread that is sent to the model (without the system message)."""
    messages = shrink_old_tool_results(repair_tool_calls(history))
    if len(messages) <= MAX_HISTORY_MESSAGES:
        return messages
    trimmed = trim_messages(
        messages,
        max_tokens=MAX_HISTORY_MESSAGES,
        token_counter=len,
        strategy="last",
        start_on="human",
        include_system=False,
        allow_partial=False,
    )
    return trimmed or messages
