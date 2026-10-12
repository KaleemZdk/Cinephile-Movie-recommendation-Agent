"""System prompt, plus a short summary of the user's library so every thread knows their taste."""

from __future__ import annotations

import logging
import sqlite3
from datetime import date

from langchain_core.messages import SystemMessage

from cinephile.storage import library

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are Cinephile, a friendly, knowledgeable movie and TV recommendation assistant.

How to work:
- Recommend titles from the user's mood, genres, themes, pacing and past taste. Say briefly why each one fits.
- Use tools for facts. Never invent titles, ratings, cast or where something streams, and never say a save
  worked unless the tool result says so.
- To favorite, queue, review or remove a title, call that library tool with the title directly. Do not search
  first. If a tool returns needs_clarification, ask the user which candidate they mean.
- For "something like X" call get_similar with the title X directly; do not search first. Call independent tools
  together in one step. For open requests prefer recommend_for_user, discover or get_trending over many searches.
- When you save or review a title you already showed, pass its year and media_type so it resolves in one call.
- Write each recommended title exactly as it appears in the tool results so the app can show its poster card.
- Use as few tool calls as needed, usually one. As soon as you have enough suitable results, stop and answer.
- Keep answers short: at most 5 recommendations with one or two lines each. Avoid spoilers unless asked.
- Movies and TV shows are both fine. If a request is unclear, ask one short question.
- Ratings are 0 to 10; convert if the user gives stars. Do not use emoji.
"""

MAX_FAVORITES = 15
MAX_WATCHING = 10
MAX_REVIEWS = 12


def _label(row: dict) -> str:
    return f"{row['title']} ({row['year']})" if row.get("year") else row["title"]


def profile_summary(user_id: str) -> str:
    """The user's saved titles as a few lines of text (empty if nothing is saved)."""
    try:
        favorites = library.list_favorites(user_id)[:MAX_FAVORITES]
        watching = library.list_watching(user_id)[:MAX_WATCHING]
        reviews = library.list_reviews(user_id)[:MAX_REVIEWS]
    except sqlite3.Error:
        log.exception("Could not read the library for the profile summary")
        return ""

    lines = []
    if favorites:
        lines.append("Favorites: " + ", ".join(_label(r) for r in favorites))
    if watching:
        lines.append("Watch list: " + ", ".join(f"{_label(r)} [{r['status']}]" for r in watching))
    if reviews:
        lines.append("Reviews: " + ", ".join(f"{_label(r)} {r['rating']:g}/10" for r in reviews))
    if not lines:
        return ""
    return "What the user has saved (do not recommend these again):\n" + "\n".join(lines)


def system_message(user_id: str) -> SystemMessage:
    parts = [SYSTEM_PROMPT, f"Today is {date.today().isoformat()}."]
    if profile := profile_summary(user_id):
        parts.append(profile)
    return SystemMessage(content="\n\n".join(parts))
