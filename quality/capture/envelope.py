"""Extract routing metadata without changing or filtering the captured body."""

from dataclasses import dataclass
import hashlib
import json
import re


def positive_id(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value)
    if not text.isascii() or not text.isdecimal() or len(text.lstrip("0")) > 19:
        return None
    number = int(text)
    return number if 0 < number <= 2**63 - 1 else None


def object_or_empty(value) -> dict:
    return value if isinstance(value, dict) else {}


def reject_non_json_number(value):
    raise ValueError("Non-JSON numeric constant")


@dataclass(frozen=True)
class CaptureEnvelope:
    account_id: int
    event_type: str
    event_key: str
    conversation_id: int | None
    message_id: int | None
    raw_body: bytes

    @classmethod
    def parse(cls, raw_body: bytes):
        payload = json.loads(raw_body.decode("utf-8"), parse_constant=reject_non_json_number)
        if not isinstance(payload, dict):
            raise ValueError("Webhook body must be a JSON object")
        event_type = payload.get("event")
        if not isinstance(event_type, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", event_type):
            raise ValueError("Missing or invalid event type")
        conversation = object_or_empty(payload.get("conversation"))
        account = object_or_empty(payload.get("account"))
        account_id = positive_id(account.get("id"))
        conversation_account_id = positive_id(conversation.get("account_id"))
        if account_id is None or (conversation_account_id is not None and account_id != conversation_account_id):
            raise ValueError("Missing or inconsistent account ID")
        is_message = event_type.startswith("message_")
        message = object_or_empty(payload.get("message"))
        message_id = (positive_id(payload.get("id")) or positive_id(message.get("id"))) if is_message else None
        conversation_id = positive_id(conversation.get("id"))
        if event_type.startswith("conversation_"):
            conversation_id = conversation_id or positive_id(payload.get("id"))
        return cls(
            account_id=account_id, event_type=event_type,
            event_key=f"{event_type}:{hashlib.sha256(raw_body).hexdigest()}",
            conversation_id=conversation_id, message_id=message_id, raw_body=raw_body,
        )
