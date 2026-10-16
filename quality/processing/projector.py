"""Deterministic replay of observed Chatwoot evidence into disposable query views.

Only a public customer creation starts a round. Events are ordered by their
source timestamps, never by worker completion order. Missing evidence remains
unknown. Reception defaults to AI and changes on a human handoff request;
actual takeover still requires confirmation or a public human reply.
"""

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import math


UTC = timezone.utc


def obj(value):
    return value if isinstance(value, dict) else {}


def text(value):
    return str(value) if value is not None and not isinstance(value, (dict, list, bool)) else None


def positive_id(value):
    value = text(value)
    if value and value.isascii() and value.isdecimal() and len(value.lstrip("0")) <= 19:
        result = int(value)
        return result if 0 < result <= 2**63 - 1 else None
    return None


def timestamp(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, str) and not value.replace(".", "", 1).isdecimal():
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return timestamp(result)
        number = float(value)
        if not math.isfinite(number) or number < 0:
            return None
        return datetime.fromtimestamp(number / 1000 if number >= 100_000_000_000 else number, UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def stable_id(prefix, *parts):
    # Length prefixes avoid ambiguous concatenations and retain source/account scope.
    encoded = "".join(f"{len(str(p))}:{p}" for p in parts).encode()
    return prefix + hashlib.sha256(encoded).hexdigest()[:32]


def conversation_payload(event):
    payload = obj(event["payload"])
    return obj(payload.get("conversation")) or (payload if event["event_type"].startswith("conversation_") else {})


def source_event_time(event):
    payload = obj(event["payload"])
    conversation = conversation_payload(event)
    key = "created_at" if event["event_type"] == "message_created" else "updated_at"
    return (timestamp(event.get("source_time")) or timestamp(payload.get(key))
            or (timestamp(conversation.get("updated_at")) if event["event_type"].startswith("conversation_") else None))


def event_time(event):
    return source_event_time(event) or timestamp(event.get("received_at")) or datetime(1970, 1, 1, tzinfo=UTC)


def contact_payload(payload, conversation):
    for candidate in (payload.get("contact"), conversation.get("contact"),
                      obj(conversation.get("meta")).get("sender"), obj(payload.get("meta")).get("sender"),
                      payload.get("sender")):
        candidate = obj(candidate)
        kind = str(candidate.get("type") or "").lower().replace("_", "")
        if candidate and (kind == "contact" or candidate.get("id") and not kind and candidate is not payload.get("sender")):
            return candidate
    # Chatwoot's incoming message webhook serializes a Contact without a type
    # field. Its own ID remains usable when no conversation contact is included.
    # Never apply this fallback to an untyped outgoing user or bot payload.
    sender = obj(payload.get("sender"))
    kind = str(sender.get("type") or payload.get("sender_type") or "").lower().replace("_", "")
    if not kind and direction_of(payload.get("message_type")) == "incoming" and positive_id(sender.get("id")):
        return sender
    return {}


def sender_role(payload):
    sender = obj(payload.get("sender"))
    kind = str(sender.get("type") or payload.get("sender_type") or "").lower().replace("_", "")
    direction = direction_of(payload.get("message_type"))
    if kind == "contact":
        return "contact"
    if kind in {"agentbot", "bot"}:
        return "bot"
    if kind in {"user", "agent"}:
        return "agent"
    if kind == "system" or direction == "activity":
        return "system"
    if not kind and direction == "incoming":
        # A missing serializer type is not a missing role: an incoming Chatwoot
        # message is from its contact. Require an observed sender/contact ID,
        # and keep conflicting identities unknown instead of guessing.
        contact = contact_payload(payload, obj(payload.get("conversation")))
        sender_id = positive_id(sender.get("id") or payload.get("sender_id"))
        contact_id = positive_id(contact.get("id"))
        if contact_id is not None and (sender_id is None or sender_id == contact_id):
            return "contact"
    return "unknown"


def direction_of(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return "unknown"
    return {0: "incoming", 1: "outgoing", 2: "activity", "0": "incoming", "1": "outgoing", "2": "activity",
            "incoming": "incoming", "outgoing": "outgoing", "activity": "activity"}.get(value, "unknown")


def is_rating(payload):
    return (payload.get("content_type") == "input_csat" or bool(payload.get("csat_survey_response"))
            or str(payload.get("event", "")).startswith(("csat_", "csat_survey_")))


def attachment_metadata(payload):
    output = []
    for position, raw in enumerate(payload.get("attachments") or []):
        if not isinstance(raw, dict):
            continue
        output.append({"attachment_key": str(raw.get("id") or f"position:{position}"),
                       "source_attachment_id": text(raw.get("id")),
                       "filename": text(raw.get("file_name") or raw.get("filename")),
                       "media_type": text(raw.get("content_type") or raw.get("file_type")),
                       "source_url": text(raw.get("data_url") or raw.get("download_url") or raw.get("url")),
                       "expected_byte_count": raw.get("file_size") if isinstance(raw.get("file_size"), int) and not isinstance(raw["file_size"], bool) and raw["file_size"] >= 0 else None,
                       "metadata": raw})
    return output


@dataclass
class Projection:
    customer: dict = field(default_factory=dict)
    identities: list = field(default_factory=list)
    snapshots: list = field(default_factory=list)
    conversation_snapshots: list = field(default_factory=list)
    messages: list = field(default_factory=list)
    rounds: list = field(default_factory=list)
    agents: list = field(default_factory=list)
    ratings: list = field(default_factory=list)
    rating_snapshots: list = field(default_factory=list)
    conversation: dict = field(default_factory=dict)


def project_conversation(events: list[dict], idle_timeout_seconds=21600, channel_bindings=None) -> Projection:
    """Replay exactly one source/account/conversation without accessing any database."""
    output = Projection()
    if not events:
        return output
    channel_bindings = channel_bindings or {}
    events = sorted(events, key=lambda item: (event_time(item), item["id"]))
    first = events[0]
    source, account, conversation_id = first["source_key"], first["account_id"], first["conversation_id"]
    if not conversation_id or any((e["source_key"], e["account_id"], e["conversation_id"]) != (source, account, conversation_id) for e in events):
        raise ValueError("Projection requires exactly one identified conversation")
    contact, contact_event = {}, first
    inbox_id = None
    for event in events:
        payload, conversation = obj(event["payload"]), conversation_payload(event)
        found = contact_payload(payload, conversation)
        if positive_id(found.get("id")):
            contact, contact_event = found, event
        inbox_id = positive_id(payload.get("inbox_id")) or positive_id(conversation.get("inbox_id")) or inbox_id
    contact_id = text(contact.get("id"))
    # A contact ID is authoritative within an instance/account. Names and phone
    # numbers never participate in customer identity or cross-contact merging.
    customer_id = stable_id("customer:", source, account, "contact", contact_id) if contact_id else stable_id("customer:", source, account, "conversation", conversation_id)
    identity_attributes = {**obj(contact.get("additional_attributes")), **obj(contact.get("custom_attributes"))}
    business_id = text(contact.get("identifier") or identity_attributes.get("app_user_id"))
    verified = any(identity_attributes.get(key) is True or contact.get(key) is True
                   for key in ("hmac_verified", "identity_verified", "identifier_verified"))
    output.customer = {"customer_id": customer_id, "source_key": source, "account_id": account,
                       "contact_id": contact_id, "business_user_id": business_id if verified else None,
                       "display_name": text(contact.get("name")), "last_message_at": None,
                       "data": {"identity_status": "verified" if verified and business_id else "contact" if contact_id else "unknown",
                                "unverified_identifier": business_id if business_id and not verified else None,
                                "identity_observed_at": iso(event_time(contact_event)),
                                "first_message_at": None, "latest_event_id": contact_event["id"]}}
    if contact_id:
        output.identities.append({"source_key": source, "account_id": account, "identity_kind": "contact",
                                  "identity_value": contact_id, "customer_id": customer_id, "evidence_event_id": contact_event["id"]})
    channel_key = channel_bindings.get((str(account), str(inbox_id)))
    logical = {}
    creation_observations = {}
    creation_actions = set()
    timeline = []
    rating_observations = []
    for event in events:
        payload, conversation = obj(event["payload"]), conversation_payload(event)
        at = event_time(event)
        event_type = event["event_type"]
        if conversation:
            output.conversation_snapshots.append({"event_id": event["id"], "source_key": source,
                "account_id": account, "conversation_id": conversation_id, "inbox_id": inbox_id,
                "channel_key": channel_key, "event_type": event_type,
                "source_time": timestamp(conversation.get("updated_at")) or source_event_time(event), "data": conversation})
        # Only explicit status transitions close rounds; message-embedded status
        # is a snapshot and may describe a newer or unrelated source operation.
        status_change = event_type == "conversation_status_changed"
        for changed in payload.get("changed_attributes") or []:
            status_change = status_change or isinstance(changed, dict) and "status" in changed
        attrs = {**obj(conversation.get("custom_attributes")), **obj(payload.get("custom_attributes"))}
        assignee = obj(obj(conversation.get("meta")).get("assignee")) or obj(conversation.get("assignee"))
        if not assignee and event_type.startswith("conversation_"):
            assignee = obj(obj(payload.get("meta")).get("assignee")) or obj(payload.get("assignee"))
        assignment_present = any("assignee" in candidate or "assignee_id" in candidate for candidate in (
            conversation, obj(conversation.get("meta")), payload if event_type.startswith("conversation_") else {},
            obj(payload.get("meta")) if event_type.startswith("conversation_") else {}))
        if not assignee:
            raw_id = conversation.get("assignee_id", payload.get("assignee_id") if event_type.startswith("conversation_") else None)
            if positive_id(raw_id):
                assignee = {"id": raw_id}
        if sender_role({"sender": assignee}) == "bot":
            # Chatwoot includes AgentBot assignments in the same assignee slot.
            # This explicitly removes the current human assignment, without
            # erasing historical human participation or an observed takeover.
            assignee = {}
        state_at = at if status_change else timestamp(conversation.get("updated_at")) or at
        timeline.append((state_at, event["id"], "state", {
            "status": conversation.get("status") or payload.get("status") if status_change and source_event_time(event) else None,
            "assignee": assignee,
            "assignment_present": assignment_present,
            "confirmed": any(attrs.get(key) is True for key in ("ics_handoff_completed", "handoff_completed", "handoff_accepted")),
            "requested": attrs.get("handoff_requested") is True or attrs.get("handoff_required") is True,
            "reason": text(attrs.get("handoff_reason")),
        }))
        if event_type.startswith("message_"):
            message_payload = {**obj(payload.get("message")), **payload}
            message_id = positive_id(event.get("message_id")) or positive_id(message_payload.get("id"))
            if not message_id:
                continue
            attributes = obj(message_payload.get("content_attributes"))
            deleted = event_type == "message_deleted" or attributes.get("deleted") is True or message_payload.get("deleted") is True
            kind = "deleted" if deleted else "backfill" if event.get("ingestion_kind") == "backfill" else "created" if event_type == "message_created" else "updated"
            previous = logical.get(message_id)
            role = sender_role(message_payload)
            direction = direction_of(message_payload.get("message_type"))
            sender = obj(message_payload.get("sender"))
            sent_at = timestamp(message_payload.get("created_at")) or (previous or {}).get("sent_at")
            snapshot = {"event_id": event["id"], "source_key": source, "account_id": account,
                "conversation_id": conversation_id, "message_id": message_id, "inbox_id": inbox_id,
                "channel_key": channel_key, "business_user_id": output.customer["business_user_id"], "contact_id": contact_id,
                "snapshot_kind": kind, "direction": direction if direction != "unknown" else (previous or {}).get("direction", "unknown"),
                "sender_role": role if role != "unknown" else (previous or {}).get("sender_role", "unknown"),
                "sender_id": text(sender.get("id") or message_payload.get("sender_id")) or (previous or {}).get("sender_id"),
                "sender_name": text(sender.get("name")) or (previous or {}).get("sender_name"),
                "content": text(message_payload.get("content")) if "content" in message_payload else (previous or {}).get("content"),
                "content_type": text(message_payload.get("content_type")) or (previous or {}).get("data", {}).get("content_type") or "text",
                "content_attributes": attributes,
                "is_private": message_payload.get("private") is True if "private" in message_payload else (previous or {}).get("is_private", False),
                "sent_at": sent_at, "source_updated_at": timestamp(message_payload.get("updated_at")),
                "attachments": attachment_metadata(message_payload), "is_rating": is_rating(message_payload)}
            snapshot["is_rating"] = snapshot["is_rating"] or snapshot["content_type"] == "input_csat"
            output.snapshots.append(snapshot)
            if previous is None:
                previous = {"source_key": source, "account_id": account, "message_id": message_id,
                    "conversation_id": conversation_id, "customer_id": customer_id, "round_id": None,
                    "original_event_id": event["id"], "creation_event_id": None,
                    "data": {"has_creation_event": False, "channel_key": channel_key, "inbox_id": inbox_id}}
                logical[message_id] = previous
            if kind == "created" and previous["creation_event_id"] is None:
                previous["creation_event_id"] = event["id"]
                previous["original_event_id"] = event["id"]
                previous["data"]["has_creation_event"] = True
                creation_observations[message_id] = snapshot
                if sent_at is not None:
                    timeline.append((sent_at, event["id"], "message", (message_id, snapshot)))
                    creation_actions.add(message_id)
            # Sorted update time chooses the latest observed version; arrival
            # order does not let an old edit replace a newer message view.
            retained_content = previous.get("content") if deleted and previous.get("content") is not None else snapshot["content"]
            retained_attachments = previous["data"].get("attachments", []) if deleted and not snapshot["attachments"] else snapshot["attachments"]
            previous.update({key: snapshot[key] for key in ("sent_at", "sender_id", "sender_name", "sender_role", "direction", "is_private")})
            previous["content"] = retained_content
            previous.update(latest_event_id=event["id"], is_deleted=deleted)
            previous["data"].update(attachments=retained_attachments, content_type=snapshot["content_type"],
                                    is_rating=snapshot["is_rating"], version_observed_at=iso(at),
                                    timestamp_status="known" if sent_at else "unknown", content_is_pre_delete=deleted and retained_content is not None)
            if snapshot["is_rating"]:
                rating_observations.append(_rating(event, message_payload, at, message_id))
        elif is_rating(payload):
            rating_observations.append(_rating(event, payload, at, None))

    # An update can be our first observation of a real public agent reply.
    # Its stable message identity and creation timestamp prove one message,
    # although they do not prove that the observed body was the original body.
    # Count it once inside an already established round; never open a round
    # from an edit or allow the edit's update time to create a boundary.
    for message in logical.values():
        if message["creation_event_id"] is not None and message["message_id"] not in creation_actions and message["sent_at"]:
            # A later observation can supply a missing creation timestamp. Use
            # that time to recover the creation action, but keep the creation
            # observation's body and sender identity; an edit is not original.
            creation = {**creation_observations[message["message_id"]], "sent_at": message["sent_at"]}
            timeline.append((message["sent_at"], message["creation_event_id"], "message", (message["message_id"], creation)))
            creation_actions.add(message["message_id"])
        elif message["creation_event_id"] is None and message["sent_at"] and not message["data"]["is_rating"]:
            observed = {**message, "is_rating": False, "attachments": message["data"]["attachments"]}
            timeline.append((message["sent_at"], message["original_event_id"], "observed_message", (message["message_id"], observed)))

    current, last_customer_at = None, None
    human_taken_over = False
    current_assignee = {}
    agent_rows = {}
    # Chatwoot creation timestamps may have only second precision. Within a
    # timestamp, source message IDs retain message order even when webhooks
    # arrived backwards. At indistinguishable source times, messages precede
    # state observations so a round exists before its resolution/takeover.
    def timeline_order(item):
        at, event_id, action, value = item
        return at, 1 if action == "state" else 0, event_id if action == "state" else value[0], event_id

    for at, event_id, action, value in sorted(timeline, key=timeline_order):
        if action == "state":
            if value["assignment_present"]:
                current_assignee = value["assignee"]
                if current:
                    _agent(agent_rows, current, current_assignee, assigned=True)
                    current["data"]["current_assignee_id"] = text(current_assignee.get("id"))
            if value["confirmed"]:
                human_taken_over = True
                if current:
                    current["reception_type"] = "human"
                    current["data"].update(handoff_result="completed", handoff_completed_at=iso(at))
            if current and value["requested"]:
                current["reception_type"] = "human"
                current["data"].update(handoff_result=current["data"].get("handoff_result") or "requested",
                                       handoff_requested_at=current["data"].get("handoff_requested_at") or iso(at), handoff_reason=value["reason"])
            if current and value["status"] == "resolved" and at >= current["started_at"]:
                if current["status"] != "resolved":
                    current["status"] = "resolved"
                    current["data"].update(closed_at=iso(at), resolved_at=iso(at), end_reason="resolved")
            continue
        message_id, snapshot = value
        service = not snapshot["is_private"] and not snapshot["is_rating"] and snapshot["direction"] != "activity" and snapshot["sender_role"] != "system"
        customer_message = service and snapshot["sender_role"] == "contact" and snapshot["direction"] == "incoming"
        if customer_message and action == "message":
            boundary = "first_customer_message" if current is None else "resolved" if current["status"] == "resolved" else "idle" if last_customer_at and (at - last_customer_at).total_seconds() >= idle_timeout_seconds else None
            if boundary:
                inherited = human_taken_over and boundary == "idle"
                if current and boundary == "idle":
                    current["status"] = "idle_boundary"
                    current["data"].update(boundary_at=iso(at), end_reason="idle_timeout")
                if boundary == "resolved":
                    human_taken_over = False
                current = {"round_id": stable_id("round:", source, account, conversation_id, message_id),
                    "source_key": source, "account_id": account, "conversation_id": conversation_id,
                    "customer_id": customer_id, "round_no": len(output.rounds) + 1, "inbox_id": inbox_id,
                    "channel_key": channel_key, "started_at": at, "last_message_at": at,
                    "reception_type": "human" if inherited else "ai_only", "reply_composition": "none",
                    "status": "active", "rating_status": "not_invited", "rating": None,
                    "data": {"boundary_rule": boundary, "start_reason": boundary, "round_rule_version": "1",
                             "idle_timeout_seconds": idle_timeout_seconds,
                             "takeover_inherited": inherited, "has_agent_reply": False, "counts": {"contact": 0, "bot": 0, "agent": 0, "private": 0, "attachments": 0},
                             "responsible_agent_id": None, "responsible_agent_name": None,
                             "responsible_assigned_at": None, "responsibility_event_id": None,
                             "responsibility_external_round_id": None, "responsibility_trigger_message_id": None,
                             "responsibility_basis": "assignment_unobserved",
                             "last_public_agent_id": None, "last_public_agent_name": None,
                             "last_public_reply_at": None, "last_public_message_id": None,
                             "customer_name": output.customer["display_name"], "first_message_id": message_id,
                             "latest_event_id": event_id, "current_assignee_id": text(current_assignee.get("id"))}}
                output.rounds.append(current)
                if current_assignee:
                    _agent(agent_rows, current, current_assignee, assigned=True)
            last_customer_at = at
            current["data"]["last_customer_at"] = iso(at)
            from ics_agent.app.chatwoot.payload import is_explicit_handoff_request
            if is_explicit_handoff_request(snapshot["content"]):
                current["reception_type"] = "human"
                current["data"].setdefault("handoff_requested_at", iso(at))
                current["data"].setdefault("handoff_reason", "explicit_request")
                current["data"].setdefault("handoff_result", "requested")
        if current is None:
            continue
        message = logical[message_id]
        if action == "observed_message":
            current["data"]["observed_only_message_count"] = current["data"].get("observed_only_message_count", 0) + 1
        if not snapshot["is_rating"]:
            message["round_id"] = current["round_id"]
        current["data"]["latest_event_id"] = max(current["data"]["latest_event_id"], event_id)
        current["data"]["current_assignee_id"] = text(current_assignee.get("id"))
        counts = current["data"]["counts"]
        if snapshot["is_private"]:
            counts["private"] += 1
            if snapshot["sender_role"] == "agent":
                _agent(agent_rows, current, {"id": snapshot["sender_id"], "name": snapshot["sender_name"]}, noted=True)
        if service:
            current["last_message_at"] = max(current["last_message_at"], at)
            counts["attachments"] += len(snapshot["attachments"])
            role = snapshot["sender_role"]
            if role in ("contact", "bot", "agent"):
                counts[role] += 1
            if role == "agent" and snapshot["direction"] == "outgoing":
                human_taken_over = True
                current["reception_type"] = "human"
                current["data"].update(has_agent_reply=True, handoff_result="completed")
                current["data"].setdefault("handoff_completed_at", iso(at))
                # Actual replies remain source facts, independently of the
                # original ICS assignment that determines responsibility.
                sender_id = snapshot["sender_id"] if positive_id(snapshot["sender_id"]) else None
                current["data"].update(last_public_agent_id=sender_id,
                    last_public_agent_name=snapshot["sender_name"], last_public_reply_at=iso(at),
                    last_public_message_id=message_id)
                _agent(agent_rows, current, {"id": snapshot["sender_id"], "name": snapshot["sender_name"]}, reply_at=at)
            elif role == "bot" and current["reception_type"] != "human":
                current["reception_type"] = "ai_only"
    for round_row in output.rounds:
        # Compatibility indicator of a round ending, independent of reception.
        round_row["data"]["classification_finalized"] = round_row["status"] in ("resolved", "idle_boundary")
        counts = round_row["data"]["counts"]
        round_row["reply_composition"] = "mixed" if counts["bot"] and counts["agent"] else "agent_only" if counts["agent"] else "ai_only" if counts["bot"] else "none"
    # Updates and partial history never start a round; assign their existing
    # logical message by its own creation time where that time is known.
    for message in logical.values():
        if message["round_id"] is None and not message["data"]["is_rating"] and message["sent_at"]:
            candidates = [r for r in output.rounds if r["started_at"] <= message["sent_at"]]
            if candidates:
                message["round_id"] = candidates[-1]["round_id"]
    output.messages = list(logical.values())
    output.agents = list(agent_rows.values())
    output.rating_snapshots = deepcopy(rating_observations)
    _link_assignments(output, events)
    _link_ratings(output, rating_observations)
    service_times = [m["sent_at"] for m in output.messages if m["sent_at"] and not m["data"]["is_rating"] and m["direction"] != "activity" and not m["is_private"]]
    if service_times:
        output.customer["last_message_at"] = max(service_times)
        output.customer["data"]["first_message_at"] = iso(min(service_times))
    output.conversation = {"source_key": source, "account_id": account, "conversation_id": conversation_id,
        "inbox_id": inbox_id, "channel_key": channel_key, "contact_id": contact_id,
        "business_user_id": output.customer["business_user_id"], "contact_name": output.customer["display_name"],
        "current_assignee_id": text(current_assignee.get("id")),
        "current_status": output.rounds[-1]["status"] if output.rounds else "unknown",
        "last_message_at": output.customer["last_message_at"], "last_event_id": events[-1]["id"]}
    return output


def _assignment_time(value):
    """Assignment contracts require an ISO timestamp with an explicit timezone."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def _assignment(event):
    """Reject malformed/untrusted records without changing their archived payload."""
    if event["event_type"] != "ics_assignment_recorded" or event.get("ingestion_kind") != "backfill":
        return None
    payload = obj(event["payload"])
    assignment = obj(payload.get("assignment"))
    conversation = obj(payload.get("conversation"))
    account_id, conversation_id = positive_id(event["account_id"]), positive_id(event["conversation_id"])
    if (payload.get("event") != "ics_assignment_recorded" or account_id is None or conversation_id is None
            or positive_id(obj(payload.get("account")).get("id")) != account_id
            or positive_id(conversation.get("account_id")) != account_id
            or positive_id(conversation.get("id")) != conversation_id
            or type(assignment.get("schema_version")) is not int or assignment["schema_version"] != 1
            or assignment.get("origin") != "ics"):
        return None
    key = assignment.get("assignment_key")
    if (not isinstance(key, str) or not key or key != key.strip()
            or assignment.get("business_round_id") != key):
        return None
    agent, trigger = assignment.get("agent_id"), assignment.get("trigger_message_id")
    if (not isinstance(agent, str) or not positive_id(agent)
            or not isinstance(trigger, str) or not positive_id(trigger)
            or "assigned_at" not in assignment):
        return None
    session = _assignment_time(assignment.get("session_started_at"))
    assigned = _assignment_time(assignment.get("assigned_at"))
    kind = assignment.get("evidence_kind", "assignment_applied")
    recorded = _assignment_time(assignment.get("recorded_at"))
    if session is None or not isinstance(kind, str) or kind not in {"assignment_applied", "current_control_backfill"}:
        return None
    if kind == "assignment_applied" and assigned is None:
        return None
    if kind == "current_control_backfill" and (assignment.get("assigned_at") is not None or recorded is None):
        return None
    return {"event_id": event["id"], "key": key, "agent_id": str(positive_id(agent)),
            "trigger_message_id": positive_id(trigger), "session_started_at": session,
            "assigned_at": assigned, "order_at": assigned or recorded}


def _link_assignments(output, events):
    """Freeze responsibility from scoped ICS evidence after real rounds exist.

    Arrival/recording time never identifies a round. The trigger message and
    session/assignment timestamps must agree; contradictory or unresolved
    evidence remains a conflict instead of falling back to a Chatwoot assignee.
    """
    if not output.rounds:
        return
    tolerance = timedelta(milliseconds=1)
    rounds = {row["round_id"]: row for row in output.rounds}
    messages = {row["message_id"]: row for row in output.messages}
    snapshots = {row["event_id"]: row for row in output.snapshots}
    limits = {}
    for index, row in enumerate(output.rounds):
        closed = timestamp(row["data"].get("closed_at"))
        following = output.rounds[index + 1]["started_at"] if index + 1 < len(output.rounds) else None
        limits[row["round_id"]] = closed, following

    def inside(row, at):
        closed, following = limits[row["round_id"]]
        return (row["started_at"] - tolerance <= at
                and (closed is None or at <= closed + tolerance)
                and (following is None or at < following))

    groups = {}
    for event in events:
        evidence = _assignment(event)
        if evidence is None:
            continue
        trigger = messages.get(evidence["trigger_message_id"])
        anchor_round = rounds.get(trigger["round_id"]) if trigger else None
        session = evidence["session_started_at"]
        assigned = evidence["assigned_at"]
        candidates = {row["round_id"] for row in output.rounds if inside(row, session)}
        if assigned is not None:
            candidates.update(row["round_id"] for row in output.rounds if inside(row, assigned))
        if anchor_round is not None:
            candidates.add(anchor_round["round_id"])
        original = snapshots.get(trigger["creation_event_id"], trigger) if trigger else {}
        trigger_at = original.get("sent_at") or (trigger or {}).get("sent_at")
        public_customer = (original.get("sender_role") == "contact" and original.get("direction") == "incoming"
                           and not original.get("is_private")
                           and not original.get("is_rating", obj(original.get("data")).get("is_rating")))
        valid = (anchor_round is not None and trigger_at is not None and public_customer
                 and inside(anchor_round, trigger_at) and inside(anchor_round, session)
                 and session <= trigger_at + tolerance
                 and (assigned is None or (inside(anchor_round, assigned)
                      and assigned + tolerance >= max(session, trigger_at))))
        evidence.update(round_id=anchor_round["round_id"] if valid else None, candidates=candidates)
        groups.setdefault(evidence["key"], []).append(evidence)

    conflicts, confirmed = set(), {}
    for records in groups.values():
        bound = {item["round_id"] for item in records}
        identities = {(item["agent_id"], item["trigger_message_id"]) for item in records}
        starts = [item["session_started_at"] for item in records]
        if (None in bound or len(bound) != 1 or len(identities) != 1 or max(starts) - min(starts) > tolerance):
            for item in records:
                conflicts.update(item["candidates"])
            continue
        selected = min(records, key=lambda item: (item["order_at"], item["event_id"]))
        confirmed.setdefault(selected["round_id"], []).append(selected)

    # Names come only from messages by that same observed human sender, never
    # from assignee snapshots or the historical CSAT assignment attribute.
    names = {}
    for snapshot in output.snapshots:
        agent_id = positive_id(snapshot["sender_id"])
        if snapshot["sender_role"] == "agent" and agent_id and snapshot["sender_name"]:
            names[str(agent_id)] = snapshot["sender_name"]
    for round_id, row in rounds.items():
        if round_id in conflicts:
            row["data"]["responsibility_basis"] = "assignment_conflict"
        elif round_id in confirmed:
            selected = min(confirmed[round_id], key=lambda item: (item["order_at"], item["event_id"]))
            row["data"].update(responsible_agent_id=selected["agent_id"],
                responsible_agent_name=names.get(selected["agent_id"]),
                responsible_assigned_at=iso(selected["assigned_at"]), responsibility_event_id=selected["event_id"],
                responsibility_external_round_id=selected["key"],
                responsibility_trigger_message_id=str(selected["trigger_message_id"]), responsibility_basis="ics_assignment")


def _agent(rows, current, sender, assigned=False, noted=False, reply_at=None):
    if sender_role({"sender": sender}) == "bot":
        return
    agent_id = text(sender.get("id"))
    if not agent_id:
        return
    key = current["round_id"], agent_id
    row = rows.setdefault(key, {"round_id": key[0], "agent_id": agent_id, "agent_name": text(sender.get("name")),
        "was_assigned": False, "replied": False, "noted": False, "reply_count": 0,
        "first_reply_at": None, "last_reply_at": None})
    row["agent_name"] = text(sender.get("name")) or row["agent_name"]
    row["was_assigned"] |= assigned
    row["noted"] |= noted
    if reply_at:
        row["replied"] = True
        row["reply_count"] += 1
        row["first_reply_at"] = min(row["first_reply_at"] or reply_at, reply_at)
        row["last_reply_at"] = max(row["last_reply_at"] or reply_at, reply_at)


def _rating(event, payload, at, message_id):
    attrs = obj(payload.get("content_attributes"))
    response = (obj(payload.get("csat_survey_response")) or obj(attrs.get("csat_survey_response"))
                or obj(obj(attrs.get("submitted_values")).get("csat_survey_response")))
    raw_rating = response.get("rating", payload.get("rating", attrs.get("rating")))
    rating = positive_id(raw_rating)
    rating = rating if rating and rating <= 5 else None
    feedback_observed = "feedback_message" in response or "feedback_message" in payload
    feedback = response.get("feedback_message") if "feedback_message" in response else payload.get("feedback_message")
    invitation_id = positive_id(response.get("message_id") or payload.get("message_id")) or message_id
    return {"event_id": event["id"], "source_key": event["source_key"], "account_id": event["account_id"],
            "conversation_id": event["conversation_id"], "invitation_message_id": invitation_id,
            "external_round_id": text(attrs.get("ics_round_id") or payload.get("ics_round_id")),
            "closed_at": timestamp(attrs.get("ics_closed_at") or payload.get("ics_closed_at")),
            "rating": rating, "feedback": text(feedback),
            "source_time": source_event_time(event), "data": {"content_attributes": attrs, "response": response,
                "observed_at": iso(at), "timestamp_status": "known" if source_event_time(event) else "unknown",
                "feedback_observed": feedback_observed,
                "rating_observed_at": iso(at) if rating is not None or feedback_observed else None}}


def _link_ratings(output, observations):
    latest = {}
    candidates = {}
    invitations = {}
    aliases = {}
    for item in observations:
        matches = {r["round_id"] for r in output.rounds
                   if item["external_round_id"] == r["round_id"] and r["status"] == "resolved"}
        if not matches and item["closed_at"]:
            # Chatwoot/ICS may round the same source instant a microsecond apart.
            # A narrow window still requires one resolved round, never the latest.
            matches = {r["round_id"] for r in output.rounds if r["status"] == "resolved"
                       and timestamp(r["data"].get("closed_at")) is not None
                       and abs(timestamp(r["data"]["closed_at"]) - item["closed_at"]) <= timedelta(milliseconds=1)}
        candidates[item["event_id"]] = matches
        if len(matches) == 1:
            if item["invitation_message_id"]:
                invitations.setdefault(item["invitation_message_id"], set()).update(matches)
            if item["external_round_id"]:
                aliases.setdefault(item["external_round_id"], set()).update(matches)
        item["round_id"] = None
        key = f"message:{item['invitation_message_id']}" if item["invitation_message_id"] else f"round:{item['external_round_id']}" if item["external_round_id"] else f"event:{item['event_id']}"
        item["rating_key"] = key
        prior = latest.get(key)
        if prior:
            for field_name in ("external_round_id", "closed_at", "rating"):
                if item.get(field_name) is None:
                    item[field_name] = prior.get(field_name)
            if not item["data"]["feedback_observed"]:
                item["feedback"] = prior["feedback"]
            if item["data"]["rating_observed_at"] is None:
                item["source_time"] = prior["source_time"]
                item["data"]["rating_observed_at"] = prior["data"]["rating_observed_at"]
                item["data"]["timestamp_status"] = prior["data"]["timestamp_status"]
        latest[key] = item
    for item in sorted(latest.values(), key=lambda row: (
            timestamp(row["data"]["rating_observed_at"] or row["data"]["observed_at"]), row["event_id"])):
        invitation_matches = invitations.get(item["invitation_message_id"], set())
        alias_matches = aliases.get(item["external_round_id"], set())
        matches = candidates[item["event_id"]]
        proven = invitation_matches | alias_matches | (matches if len(matches) == 1 else set())
        # Conflicting card/alias evidence stays unlinked. An established card
        # may disambiguate a coarse timestamp but cannot point to another round.
        if (len(invitation_matches) <= 1 and len(alias_matches) <= 1 and len(proven) == 1
                and (not matches or proven.issubset(matches))):
            item["round_id"] = next(iter(proven))
        item["status"] = "unlinked" if not item["round_id"] else "rated" if item["rating"] else "pending"
        item["data"]["linkage"] = "closed_at_or_invitation" if item["round_id"] else "unknown"
        output.ratings.append(item)
        if item["round_id"]:
            round_row = next(r for r in output.rounds if r["round_id"] == item["round_id"])
            round_row["rating_status"] = item["status"]
            round_row["rating"] = item["rating"]
            round_row["data"].update(rating_at=iso(item["source_time"]) if item["rating"] else None,
                                     rating_feedback=item["feedback"],
                                     external_round_id=item["external_round_id"], invitation_message_id=item["invitation_message_id"])
            for message in output.messages:
                if message["message_id"] == item["invitation_message_id"]:
                    message["round_id"] = item["round_id"]
