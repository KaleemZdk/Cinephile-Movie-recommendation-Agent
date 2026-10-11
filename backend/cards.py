"""
cards.py - Shape data for the web UI.

Turns TMDB items and saved rows into the dict the frontend renders, finds the titles
an assistant reply is talking about so they can be shown as poster cards, and converts
a saved thread back into chat messages.
"""

import json
import re

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from agent import extract_text
from mcp_server import tmdb

# Tools whose output lists titles worth showing as cards.
CARD_TOOLS = {
    "search_shows", "search_movies", "get_details", "get_similar",
    "get_trending", "discover", "recommend_for_user",
}
MAX_CARDS = 6


def ui_item(d: dict, size: str = "w342") -> dict:
    """One title as the frontend expects it (works for TMDB list items and saved rows)."""
    path = d.get("poster_path")
    return {
        "id": d.get("tmdb_id"),
        "tmdb_id": d.get("tmdb_id"),
        "media_type": d.get("media_type") or "movie",
        "title": d.get("title") or "Untitled",
        "year": d.get("year"),
        "rating": d.get("rating"),
        "genres": d.get("genres") or [],
        "overview": d.get("overview") or "",
        "poster_url": tmdb.image_url(path, size),
        "poster_path": path,
        "because_you_liked": d.get("because_you_liked") or [],
    }


def _tool_payload(message: ToolMessage) -> dict | None:
    try:
        payload = json.loads(extract_text(message.content))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def cards_from_turn(turn: list[BaseMessage], reply: str, limit: int = MAX_CARDS) -> list[dict]:
    """Titles returned by tools this turn, keeping only those the reply actually mentions."""
    found: dict[tuple, tuple[int, dict]] = {}
    titles_seen: set[str] = set()  # one card per title, the first (best-ranked) one wins
    for message in turn:
        if not isinstance(message, ToolMessage) or message.name not in CARD_TOOLS:
            continue
        payload = _tool_payload(message)
        if not payload:
            continue
        if "results" in payload:
            items = payload["results"]
        elif "title" in payload and "tmdb_id" in payload:
            items = [payload]
        else:
            items = []
        for item in items:
            tid, title = item.get("tmdb_id"), item.get("title")
            key = (tid, item.get("media_type"))
            if not tid or not title or key in found or tmdb._norm_title(title) in titles_seen:
                continue
            # Case-sensitive on purpose: otherwise the title "Her" would match the word "her".
            hit = re.search(rf"(?<!\w){re.escape(title)}(?!\w)", reply)
            if hit:
                found[key] = (hit.start(), item)
                titles_seen.add(tmdb._norm_title(title))
    ordered = sorted(found.values(), key=lambda pair: pair[0])
    return [ui_item(item) for _, item in ordered][:limit]


def messages_to_ui(messages: list[BaseMessage]) -> list[dict]:
    """A saved thread as chat messages: user turns and final assistant replies with their cards."""
    out: list[dict] = []
    i = 0
    while i < len(messages):
        message = messages[i]
        if not isinstance(message, HumanMessage):
            i += 1
            continue
        out.append({"role": "user", "content": extract_text(message.content)})
        j = i + 1
        while j < len(messages) and not isinstance(messages[j], HumanMessage):
            j += 1
        turn = messages[i + 1:j]
        replies = [
            extract_text(m.content).strip()
            for m in turn
            if isinstance(m, AIMessage) and not m.tool_calls and extract_text(m.content).strip()
        ]
        if replies:
            out.append({
                "role": "assistant",
                "content": replies[-1],
                "recommendations": cards_from_turn(turn, replies[-1]),
            })
        i = j
    return out
