"""Versioned, transport-independent extension contracts."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=120)
    text: str = Field(default="", max_length=100000)
    deeplink: str = Field(default="", max_length=1000)
    published_at: datetime | None = None
    revision: str = Field(default="", max_length=120)


class SessionMessage(BaseModel):
    platform: str
    bot: str
    conversation: str
    event_id: str
    speaker: str
    role: Literal["user", "assistant", "tool"]
    content: str
    reply_to: str | None = None
    status: Literal["received", "pending", "sent", "failed", "unknown"]


class MediaAsset(BaseModel):
    source_id: str
    attachment_id: str
    kind: Literal["photo", "video", "document"]
    status: str
    sha256: str | None = None
    original_path: str | None = None
    preview_path: str | None = None
    error: str | None = None


class ContentVersion(BaseModel):
    message_id: int
    digest: str
    theme: str
    render_version: str


class DeliveryResult(BaseModel):
    status: Literal["sent", "failed", "unknown", "blocked", "expired"]
    bot: str
    target: str
    trigger: str
    remote_id: str | None = None
    error: str | None = None
