"""Shaping data for the frontend.

Turns TMDB items and saved rows into the dict the UI renders, finds the titles an
assistant reply talks about so they can be shown as poster cards, and converts a saved
thread back into chat messages.
"""

from __future__ import annotations

import json
import re

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from cinephile import tmdb
from cinephile.agent.messages import extract_text

# Tools whose output lists titles worth showing as cards.
CARD_TOOLS = frozenset(
    {
        "search_shows",
        "search_movies",
        "get_details",
        "get_similar",
        "get_trending",
        "discover",
        "recommend_for_user",
    }
)
MAX_CARDS = 6


def ui_item(data: dict, size: str = "w342") -> dict:
    """One title as the frontend expects it (works for TMDB items and saved rows)."""
    path = data.get("poster_path")
    return {
        "id": data.get("tmdb_id"),
        "tmdb_id": data.get("tmdb_id"),
        "media_type": data.get("media_type") or "movie",
        "title": data.get("title") or "Untitled",
        "year": data.get("year"),
        "rating": data.get("rating"),
        "genres": data.get("genres") or [],
        "overview": data.get("overview") or "",
        "poster_url": tmdb.image_url(path, size),
        "poster_path": path,
        "because_you_liked": data.get("because_you_liked") or [],
    }


def saved_item(row: dict) -> dict:
    """A saved library row (favorite, watch-list entry or review) for the UI."""
    item = ui_item(row)
    for key in ("status", "added_at", "updated_at", "created_at", "comment", "rating"):
        if row.get(key) is not None:
            item[key] = row[key]
    item["saved_at"] = row.get("added_at") or row.get("created_at")
    return item


def _tool_items(message: ToolMessage) -> list[dict]:
    try:
        payload = json.loads(extract_text(message.content))
    except ValueError:
        return []
    if not isinstance(payload, dict):
        return []
    if "results" in payload:
        return payload["results"]
    if "title" in payload and "tmdb_id" in payload:
        return [payload]
    return []


def cards_from_turn(turn: list[BaseMessage], reply: str, limit: int = MAX_CARDS) -> list[dict]:
    """Titles returned by tools this turn that the reply mentions, in the order it mentions them."""
    found: dict[tuple, tuple[int, dict]] = {}
    titles_seen: set[str] = set()  # one card per title; the first (best-ranked) one wins
    for message in turn:
        if not isinstance(message, ToolMessage) or message.name not in CARD_TOOLS:
            continue
        for item in _tool_items(message):
            tmdb_id, title = item.get("tmdb_id"), item.get("title")
            if not tmdb_id or not title:
                continue
            key = (tmdb_id, item.get("media_type"))
            normalized = tmdb.normalize_title(title)
            if key in found or normalized in titles_seen:
                continue
            # Case-sensitive on purpose: the title "Her" must not match the word "her".
            if hit := re.search(rf"(?<!\w){re.escape(title)}(?!\w)", reply):
                found[key] = (hit.start(), item)
                titles_seen.add(normalized)
    ordered = sorted(found.values(), key=lambda pair: pair[0])
    return [ui_item(item) for _, item in ordered][:limit]


def _final_reply(turn: list[BaseMessage]) -> str:
    replies = [
        extract_text(m.content).strip()
        for m in turn
        if isinstance(m, AIMessage) and not m.tool_calls and extract_text(m.content).strip()
    ]
    return replies[-1] if replies else ""


def messages_to_ui(messages: list[BaseMessage]) -> list[dict]:
    """A saved thread as chat messages: user turns, final replies, and their cards."""
    turns: list[tuple[str, list[BaseMessage]]] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            turns.append((extract_text(message.content), []))
        elif turns:
            turns[-1][1].append(message)

    out: list[dict] = []
    for index, (question, turn) in enumerate(turns):
        reply = _final_reply(turn)
        retried = index + 1 < len(turns) and turns[index + 1][0] == question
        if not reply and retried:
            continue  # a failed attempt that the user retried: show the question once
        out.append({"role": "user", "content": question})
        if reply:
            out.append({"role": "assistant", "content": reply, "recommendations": cards_from_turn(turn, reply)})
    return out
