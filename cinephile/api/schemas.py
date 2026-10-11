"""Request bodies. Validation happens here, so routes receive clean values."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

MediaType = Literal["movie", "tv"]
MediaFilter = Literal["all", "movie", "tv"]
WatchStatus = Literal["plan", "watching", "watched"]
ThreadId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
ModelName = Annotated[str, Field(pattern=r"^gemini[A-Za-z0-9.-]{0,60}$")]

MAX_MESSAGE_CHARS = 4000


class ChatRequest(BaseModel):
    message: str = Field(max_length=MAX_MESSAGE_CHARS)
    thread_id: ThreadId | None = None
    conversation_id: ThreadId | None = None  # older name for thread_id
    model: ModelName | None = None

    @field_validator("message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Message is empty.")
        return value

    @property
    def thread(self) -> str | None:
        return self.thread_id or self.conversation_id


class TitleIn(BaseModel):
    """A title as the UI identifies it. Missing title, year or poster are filled from TMDB."""

    tmdb_id: int | None = Field(default=None, gt=0)
    show_id: int | None = Field(default=None, gt=0)  # older name for tmdb_id
    media_type: MediaType = "movie"
    title: str = Field(default="", max_length=300)
    year: str | None = Field(default=None, pattern=r"^\d{4}$")
    poster_path: str | None = Field(default=None, max_length=200, pattern=r"^/[\w.-]+$")

    @model_validator(mode="after")
    def _needs_id(self) -> TitleIn:
        if not (self.tmdb_id or self.show_id):
            raise ValueError("tmdb_id is required.")
        return self

    @property
    def id(self) -> int:
        return self.tmdb_id or self.show_id


class WatchIn(TitleIn):
    status: WatchStatus = "watching"


class StatusIn(BaseModel):
    status: WatchStatus


class ReviewIn(TitleIn):
    rating: float = Field(ge=0, le=10)
    comment: str = Field(default="", max_length=2000)


class ThreadPatch(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    pinned: bool | None = None
