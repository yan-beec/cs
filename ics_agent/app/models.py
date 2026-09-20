from typing import Any

from pydantic import BaseModel, Field


class ChatwootWebhookEvent(BaseModel):
    event: str = ""
    id: str | int | None = None
    account: dict[str, Any] | None = None
    conversation: dict[str, Any] | None = None
    message_type: str | None = None
    content: str | None = None
    sender: dict[str, Any] | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
