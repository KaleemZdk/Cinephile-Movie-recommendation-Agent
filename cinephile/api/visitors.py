"""Anonymous visitors: each browser gets its own private library and chats.

A visitor is identified by a random id in an HttpOnly cookie, created on the first
visit. Nothing else is asked for. Clearing cookies or switching browsers starts a new,
empty visitor; the old data stays on the server but is no longer reachable.

The id must be long and random, so the short LOCAL_USER id that owns data from before
visitors existed can never be claimed through the cookie.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Awaitable, Callable

from fastapi import Request, Response

from cinephile.storage.db import LOCAL_USER

COOKIE_NAME = "cinephile_visitor"
COOKIE_MAX_AGE = 400 * 24 * 60 * 60  # the longest lifetime browsers allow
VISITOR_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,64}")
_SKIPPED_PREFIXES = ("/assets/",)  # static files need no visitor


def new_visitor_id() -> str:
    return secrets.token_urlsafe(32)


def valid_visitor_id(value: str | None) -> bool:
    return bool(value and VISITOR_ID_PATTERN.fullmatch(value))


def _is_https(request: Request) -> bool:
    # Hosts like Render end TLS at their proxy and forward plain HTTP with this header.
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return request.url.scheme == "https" or forwarded == "https"


def visitor_middleware(single_user: bool) -> Callable[[Request, Callable], Awaitable[Response]]:
    """Middleware that sets request.state.user_id, issuing the cookie when needed."""

    async def assign_visitor(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.url.path.startswith(_SKIPPED_PREFIXES):
            return await call_next(request)
        if single_user:
            request.state.user_id = LOCAL_USER
            return await call_next(request)

        visitor = request.cookies.get(COOKIE_NAME)
        known = valid_visitor_id(visitor)
        if not known:
            visitor = new_visitor_id()
        request.state.user_id = visitor
        response = await call_next(request)
        if not known or request.url.path == "/":  # renew on each page load so active visitors keep their data
            response.set_cookie(
                COOKIE_NAME,
                visitor,
                max_age=COOKIE_MAX_AGE,
                path="/",
                httponly=True,
                samesite="lax",
                secure=_is_https(request),
            )
        return response

    return assign_visitor


def get_user_id(request: Request) -> str:
    return request.state.user_id
