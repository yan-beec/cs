"""可信非消息证据的精确请求处理。"""
from datetime import timedelta
import pytest
from quality.tests.test_processing import event, project, ics_assignment, agent_reply, START
from quality.processing.projector import HANDOFF_BUSY_NOTICE, HANDOFF_QUEUE_NOTICE_PREFIX

def queued(identity=90, trigger=1, minute=2):
    item = event(identity, minute, event_type="ics_handoff_queue_decided")
    item["ingestion_kind"] = "backfill"
    item["payload"] = {"event": item["event_type"], "account": {"id": 1},
        "conversation": {"id": 12, "account_id": 1}, "queue_decision": {
            "schema_version": 1, "origin": "ics", "outcome": "queued", "contact_id": "42",
            "trigger_message_id": str(trigger), "trigger_at": START.isoformat(),
            "decided_at": (START + timedelta(minutes=minute)).isoformat()}}
    return item

def test_queue_without_notice_is_not_handoff_or_lost():
    row = project(event(1, content="转人工"), queued()).rounds[0]
    assert row["reception_type"] == "ai_only"
    assert "handoff_requested_at" not in row["data"]

def test_confirmed_assignment_without_notice_is_success():
    row = project(event(1, attachments=[{"id": 1, "file_type": "image"}]), ics_assignment(2)).rounds[0]
    assert row["reception_type"] == "human"
    assert row["data"]["handoff_result"] == "completed"
    assert not row["data"].get("has_agent_reply")

def test_queue_suppresses_only_its_request():
    row = project(event(1, content="转人工"), event(2, 1, content="转人工"), queued(trigger=2)).rounds[0]
    assert row["data"]["handoff_requested_at"] == START.isoformat()

def test_queue_notice_does_not_cancel_previous_unqueued_request():
    row = project(event(1, content="转人工"), event(2, 1, content="转人工"), queued(trigger=2),
        event(3, 2, sender="agent_bot", direction="outgoing", content=HANDOFF_QUEUE_NOTICE_PREFIX + "3人")).rounds[0]
    assert row["data"]["handoff_requested_at"] == START.isoformat()

@pytest.mark.parametrize("prior", ["busy", "reply", "success"])
def test_queue_keeps_prior_evidence(prior):
    earlier = {"busy": event(2, .1, sender="agent_bot", direction="outgoing", content=HANDOFF_BUSY_NOTICE),
        "reply": agent_reply(2, .1), "success": ics_assignment(2, .1)}[prior]
    row = project(event(1, content="转人工"), earlier, event(3, 1, content="转人工"), queued(trigger=3)).rounds[0]
    assert row["reception_type"] == "human"
    assert row["data"].get("handoff_failed_at") if prior == "busy" else row["data"]["handoff_result"] == "completed"

def test_success_wins_for_same_queued_request():
    row = project(event(1, content="转人工"), queued(), ics_assignment(91, 1)).rounds[0]
    assert row["data"]["handoff_result"] == "completed"

def test_success_refreshes_each_window_entry_seed():
    history = [event(1), ics_assignment(2), event(3, 400)]
    full = project(*history)
    seed = full.rounds[1]["data"]["replay_seed"]
    assert seed["human_taken_over"] is True
    assert project(history[-1], replay_seed=seed, round_no_offset=1).rounds[0]["reception_type"] == "human"

def test_reply_then_new_request_keeps_continuation_takeover():
    first = project(event(1), agent_reply(2, 1), event(3, 2, content="转人工"))
    assert first.continuation["human_taken_over"] is True

def test_unknown_assignment_time_does_not_manufacture_success():
    row = project(event(1), ics_assignment(2, evidence_kind="current_control_backfill")).rounds[0]
    assert row["data"]["responsible_assigned_at"] is None
    assert row["reception_type"] == "ai_only"
    assert "handoff_completed_at" not in row["data"]

def test_known_success_supersedes_unknown_snapshot_without_new_reply():
    row = project(event(1), ics_assignment(2, .1, evidence_kind="current_control_backfill"), ics_assignment(3, 1)).rounds[0]
    assert row["reception_type"] == "human"
    assert row["data"]["responsible_assigned_at"] == (START + timedelta(minutes=1)).isoformat()

def test_late_queue_anchor_does_not_change_newest_round():
    history = [event(1, content="转人工"), event(2, 1, event_type="conversation_status_changed", status="resolved"),
        event(3, 2, content="转人工"), queued(90, 1, 100)]
    rows = project(*history).rounds
    assert rows[0]["reception_type"] == "ai_only"
    assert rows[1]["data"]["handoff_requested_at"] == (START + timedelta(minutes=2)).isoformat()

@pytest.mark.parametrize("invalid", ["webhook", "version", "account", "private"])
def test_invalid_queue_cannot_suppress_request(invalid):
    evidence, request = queued(), event(1, content="转人工")
    if invalid == "webhook": evidence["ingestion_kind"] = "webhook"
    if invalid == "version": evidence["payload"]["queue_decision"]["schema_version"] = True
    if invalid == "account": evidence["payload"]["account"]["id"] = 2
    if invalid == "private": evidence["payload"]["queue_decision"]["trigger_message_id"] = "100"
    assert project(request, evidence).rounds[0]["reception_type"] == "human"
