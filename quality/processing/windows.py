"""Small replay locators; no message bodies or raw payloads are retained here."""

from itertools import groupby

from . import projector
from .projector import (
    _assignment, _rating, contact_payload, conversation_payload, direction_of, event_time,
    is_rating, obj, positive_id, stable_id, state_observation, text, timestamp,
)


def event_locator(event):
    payload = obj(event["payload"])
    message = {**obj(payload.get("message")), **payload}
    at, state = state_observation(event)
    attributes = obj(message.get("content_attributes"))
    deleted = event["event_type"] == "message_deleted" or attributes.get("deleted") is True or message.get("deleted") is True
    kind = ("deleted" if deleted else "backfill" if event["ingestion_kind"] == "backfill"
            else "created" if event["event_type"] == "message_created" else "updated")
    rating = _rating(event, message, event_time(event), event.get("message_id")) if is_rating(message) else {}
    assignment = obj(payload.get("assignment")) if event["event_type"] == "ics_assignment_recorded" else {}
    valid_assignment = _assignment(event)
    contact = contact_payload(payload, conversation_payload(event))
    return {
        "event_id": event["id"], "source_key": event["source_key"], "account_id": event["account_id"],
        "conversation_id": event["conversation_id"], "event_type": event["event_type"],
        "event_at": event_time(event), "state_at": at, "message_id": event.get("message_id"),
        "sent_at": timestamp(message.get("created_at")) if event["event_type"].startswith("message_") else None,
        "anchor_message_id": positive_id(assignment.get("trigger_message_id")) or rating.get("invitation_message_id"),
        "closed_at": rating.get("closed_at"), "assignment_started_at": timestamp(assignment.get("session_started_at")),
        "contact_id": text(contact.get("id")) if positive_id(contact.get("id")) else None,
        "is_resolution": state["status"] == "resolved", "payload_bytes": event["payload_bytes"],
        "data": {"kind": kind, "role": projector.sender_role(message), "direction": direction_of(message.get("message_type")),
                 "sender_id": text(positive_id(obj(message.get("sender")).get("id"))),
                 "sender_name": text(obj(message.get("sender")).get("name")),
                 "private": message.get("private") is True if "private" in message else None,
                 "content_type": text(message.get("content_type")), "rating": is_rating(message),
                 "rating_external": rating.get("external_round_id"),
                 "assignment_key": valid_assignment["key"] if valid_assignment else None,
                 "assignment_at": valid_assignment["assigned_at"] if valid_assignment else None},
    }


def message_locators(rows):
    """Reproduce creation identity/time recovery using a constant-size group."""
    for message_id, observations in groupby(rows, key=lambda row: row["message_id"]):
        role, direction, private, content_type, sent_at, creation = "unknown", "unknown", False, "text", None, None
        for row in observations:
            data = row["data"]
            role = data["role"] if data["role"] != "unknown" else role
            direction = data["direction"] if data["direction"] != "unknown" else direction
            private = data["private"] if data["private"] is not None else private
            content_type = data["content_type"] or content_type
            sent_at = row["sent_at"] or sent_at
            if data["kind"] == "created" and creation is None:
                creation = {"event_id": row["event_id"], "sent_at": sent_at,
                            "public_customer": role == "contact" and direction == "incoming"
                            and not private and not data["rating"] and content_type != "input_csat"}
        creation_at = (creation["sent_at"] or sent_at) if creation else None
        yield {"message_id": message_id, "sent_at": sent_at,
               "creation_event_id": creation["event_id"] if creation else None,
               "is_customer_creation": bool(creation and creation["public_customer"] and creation_at),
               # Creation time may precede a malformed later observation. Both
               # source timestamps are kept for exact round boundary ordering.
               "creation_at": creation_at}


def round_locators(actions, scope, customer_id, idle_timeout_seconds):
    last_customer_at, resolved, count = None, False, 0
    for action in actions:
        if action["kind"] == "resolution":
            if count:
                resolved = True
            continue
        at, message_id = action["at"], action["message_id"]
        if not count or resolved or (at - last_customer_at).total_seconds() >= idle_timeout_seconds:
            count += 1
            yield {"source_key": scope[0], "account_id": scope[1], "conversation_id": scope[2],
                   "customer_id": customer_id, "round_id": stable_id("round:", *scope, message_id),
                   "round_no": count, "first_message_id": message_id, "started_at": at}
            resolved = False
        last_customer_at = at
