"""排队决定的稳定身份和归档编码，不包含消息正文。"""
import hashlib
import json
from .repository import AssignmentEvent, _positive_id, _time

QUEUE_OUTBOX_TABLE = "ics_handoff_queue_event_outbox"

def queue_event_key(account, conversation, message):
    identity = [_positive_id(value) for value in (account, conversation, message)]
    return "ics_handoff_queue:" + hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()

def queue_event(settings, row):
    account, conversation, trigger, contact = [_positive_id(row[key]) for key in
        ("account_id", "conversation_id", "trigger_message_id", "contact_id")]
    if account not in settings.account_ids or type(row["payload_version"]) is not int or row["payload_version"] != 1 or row["outcome"] != "queued":
        raise ValueError("Invalid queue evidence scope or version")
    key = queue_event_key(account, conversation, trigger)
    if row["event_key"] != key:
        raise ValueError("Invalid queue event identity")
    trigger_at, decided = _time(row["trigger_at"]), _time(row["decided_at"])
    payload = {"event": "ics_handoff_queue_decided", "account": {"id": account},
        "conversation": {"id": conversation, "account_id": account}, "updated_at": decided.isoformat(),
        "queue_decision": {"schema_version": 1, "origin": "ics", "outcome": "queued",
            "trigger_message_id": str(trigger), "contact_id": str(contact),
            "trigger_at": trigger_at.isoformat(), "decided_at": decided.isoformat()}}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return AssignmentEvent(key, account, conversation, decided, raw, "ics_handoff_queue_decided")
