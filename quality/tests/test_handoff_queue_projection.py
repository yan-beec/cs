"""排队提示只撤销当前尝试，不抹掉此前人工接待或拒接证据。"""

from datetime import timedelta

from quality.tests.test_processing import START, agent_reply, event, project
from quality.processing.projector import HANDOFF_BUSY_NOTICE


NOTICE = "当前咨询人数较多，您前面还有 3 人，预计等待 2 分钟。"


def notice(message_id, minute, **kwargs):
    return event(message_id, minute, sender="agent_bot", direction="outgoing", content=NOTICE, **kwargs)


def test_queue_keyword_returns_to_ai_without_transfer_clock():
    row = project(event(1, content="转人工"), notice(2, 1)).rounds[0]
    assert row["reception_type"] == "ai_only"
    assert row["data"]["handoff_result"] == "queued"
    assert "handoff_requested_at" not in row["data"]
    assert "handoff_failed_at" not in row["data"]


def test_media_queue_stays_ai_without_an_explicit_keyword():
    row = project(event(1, attachments=[{"id": 1, "file_type": "image"}]), notice(2, 1)).rounds[0]
    assert row["reception_type"] == "ai_only"
    assert row["data"]["handoff_result"] == "queued"
    assert "handoff_requested_at" not in row["data"]


def test_busy_refusal_before_queue_keeps_its_evidence():
    row = project(event(1, content="转人工"),
                  event(2, 1, sender="agent_bot", direction="outgoing", content=HANDOFF_BUSY_NOTICE),
                  event(3, 2, content="转人工"), notice(4, 3)).rounds[0]
    assert row["reception_type"] == "human"
    assert row["data"]["handoff_result"] == "queued"
    assert row["data"]["handoff_failed_at"] == (START + timedelta(minutes=1)).isoformat()
    assert "handoff_requested_at" not in row["data"]


def test_repeated_queue_requests_do_not_leak_human_classification():
    row = project(event(1, content="转人工"), event(2, .1, content="转人工"),
                  notice(3, .2), notice(4, .3), event(5, .4, content="转人工"), notice(6, .5)).rounds[0]
    assert row["reception_type"] == "ai_only"
    assert "handoff_requested_at" not in row["data"]


def test_real_request_after_queue_uses_new_transfer_clock():
    row = project(event(1, content="转人工"), notice(2, 1),
                  event(3, 2, content="转人工"), agent_reply(4, 3)).rounds[0]
    assert row["reception_type"] == "human"
    assert row["data"]["handoff_result"] == "completed"
    assert row["data"]["handoff_requested_at"] == (START + timedelta(minutes=2)).isoformat()


def test_late_queue_notice_does_not_undo_a_human_reply():
    row = project(event(1, content="转人工"), agent_reply(2, 1), notice(3, 2)).rounds[0]
    assert row["reception_type"] == "human"
    assert row["data"]["handoff_result"] == "completed"
    assert row["data"]["has_agent_reply"] is True
    assert row["data"]["handoff_requested_at"] == START.isoformat()


def test_queue_after_a_completed_attempt_restores_original_clock():
    row = project(event(1, content="转人工"), agent_reply(2, 1),
                  event(3, 2, content="转人工"), notice(4, 3)).rounds[0]
    assert row["reception_type"] == "human"
    assert row["data"]["handoff_result"] == "completed"
    assert row["data"]["handoff_requested_at"] == START.isoformat()


def test_private_or_customer_queue_text_is_not_queue_evidence():
    for extra in (notice(2, 1, private=True), event(2, 1, content=NOTICE)):
        row = project(event(1, content="转人工"), extra).rounds[0]
        assert row["reception_type"] == "human"
        assert row["data"]["handoff_result"] == "requested"


def test_new_round_does_not_inherit_queue_attempt_state():
    rows = project(event(1, content="转人工"), notice(2, 1),
                   event(3, 2, event_type="conversation_status_changed", status="resolved"),
                   event(4, 3)).rounds
    assert len(rows) == 2
    assert rows[1]["reception_type"] == "ai_only"
    assert "handoff_result" not in rows[1]["data"]
