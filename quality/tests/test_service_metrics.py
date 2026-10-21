"""Service timing examples use complete logical messages, never detail pages."""

from datetime import timedelta

import pytest

from quality.query.service_metrics import RoundServiceMetrics
from quality.tests.test_processing import START


def message(minute, role="contact", **overrides):
    row = {"sent_at": START + timedelta(minutes=minute) if minute is not None else None,
           "sender_role": role, "direction": "incoming" if role == "contact" else "outgoing",
           "is_private": False, "is_rating": False}
    return {**row, **overrides}


def metrics(messages, **overrides):
    round_item = {"started_at": START, "last_message_at": START + timedelta(minutes=8),
                  "status": "resolved", "resolved_at": START + timedelta(minutes=12),
                  "handoff_requested_at": (START + timedelta(minutes=1)).isoformat()}
    calculator = RoundServiceMetrics({**round_item, **overrides})
    for row in messages:
        calculator.observe(row)
    return calculator.result()


def test_first_response_average_customer_bursts_and_human_phase_counts():
    result = metrics([
        message(0), message(.5, "bot"), message(1.5), message(2),
        message(3, "agent"), message(3.5, "agent"), message(4), message(5),
        message(5.5, "bot"), message(7, "agent"), message(8),
        message(9, "agent", is_private=True), message(10, "bot", is_rating=True),
        message(11, "system", direction="activity"),
    ])
    assert result["first_response_seconds"] == 120
    assert result["average_response_seconds"] == 150
    assert result["response_sample_count"] == 2
    assert result["human_phase_customer_count"] == 5
    assert result["human_phase_agent_count"] == 3
    assert result["human_phase_message_count"] == 8
    assert result["last_message_by_agent"] is False
    assert result["last_message_sender_role"] == "contact"
    assert result["duration_seconds"] == 720


def test_bot_phase_is_not_included_in_agent_waits_or_counts():
    result = metrics([message(0), message(1, "bot"), message(4), message(5, "agent")],
                     handoff_requested_at=START + timedelta(minutes=4))
    assert result["first_response_seconds"] == result["average_response_seconds"] == 60
    assert (result["human_phase_customer_count"], result["human_phase_agent_count"]) == (1, 1)


def test_initial_wait_needs_no_additional_customer_message_after_transfer():
    result = metrics([message(0), message(3, "agent")])
    assert result["first_response_seconds"] == result["average_response_seconds"] == 120
    assert result["human_phase_message_count"] == 1


def test_missing_handoff_time_never_invents_zero_response_from_inferred_completion():
    result = metrics([message(0), message(1, "agent"), message(2), message(3, "agent")],
                     handoff_requested_at=None, handoff_completed_at=START + timedelta(minutes=1))
    assert result["first_response_seconds"] is None
    assert result["first_response_status"] == "missing_handoff_time"
    assert result["human_phase_basis"] == "first_agent_reply"
    assert result["average_response_seconds"] == 60
    assert (result["human_phase_customer_count"], result["human_phase_agent_count"]) == (1, 2)


def test_original_ics_assignment_supplies_an_observed_transfer_time():
    result = metrics([message(0), message(2, "agent")], handoff_requested_at=None,
                     responsibility_basis="ics_assignment", responsible_assigned_at=START + timedelta(minutes=.5))
    assert result["first_response_seconds"] == 90
    assert result["human_phase_basis"] == "ics_assignment"


def test_successful_assignment_replaces_an_earlier_request_clock():
    result = metrics([message(1, message_id=10), message(2, "agent")],
                     handoff_requested_at=START,
                     responsibility_basis="ics_assignment",
                     responsible_assigned_at=START + timedelta(minutes=1),
                     responsibility_trigger_message_id="10")
    assert result["first_response_seconds"] == 60
    assert result["average_response_seconds"] == 60
    assert result["human_phase_basis"] == "ics_assignment"


def test_legacy_assignment_and_current_assignee_do_not_supply_transfer_time():
    result = metrics([message(0), message(2, "agent")], handoff_requested_at=None,
                     responsibility_basis="last_public_agent_reply", responsible_assigned_at=START,
                     current_assignee_id="7")
    assert result["first_response_seconds"] is None


def test_inherited_human_round_starts_its_wait_at_the_new_round_start():
    result = metrics([message(0), message(2, "agent")], handoff_requested_at=None, takeover_inherited=True)
    assert result["first_response_seconds"] == 120
    assert result["human_phase_basis"] == "inherited_takeover"
    assert result["human_phase_customer_count"] == 1


def test_unanswered_transfer_is_pending_and_does_not_report_zero_average():
    result = metrics([message(0), message(2), message(3, "bot")])
    assert result["first_response_status"] == "awaiting_agent"
    assert result["first_response_seconds"] is None and result["average_response_seconds"] is None
    assert result["human_phase_message_count"] == 1
    assert result["last_message_by_agent"] is False


def test_ai_only_round_has_no_human_wait_or_human_messages():
    result = metrics([message(0), message(1, "bot")], handoff_requested_at=None)
    assert result["first_response_status"] == "not_handed_off"
    assert result["human_phase_message_count"] == 0
    assert result["response_sample_count"] == 0 and result["average_response_seconds"] is None


@pytest.mark.parametrize("role,expected", [("contact", False), ("bot", False), ("agent", True), ("unknown", None)])
def test_last_public_speaker_ignores_later_notes_system_activities_and_rating_cards(role, expected):
    result = metrics([message(2, role), message(3, "agent", is_private=True),
                      message(4, "system", direction="activity"), message(5, "bot", is_rating=True)])
    assert result["last_message_by_agent"] is expected
    assert result["last_message_sender_role"] == role


@pytest.mark.parametrize("role", ["contact", "agent"])
def test_missing_times_are_unknown_without_inventing_durations_or_counts(role):
    result = metrics([message(0), message(2, "agent"), message(None, role)])
    assert result["last_message_by_agent"] is None
    assert result["average_response_seconds"] is None
    assert result["human_phase_message_count"] is None
    if role == "agent":
        assert result["first_response_seconds"] is None and result["first_response_status"] == "unknown_time"


@pytest.mark.parametrize("status", ["active", "idle_boundary"])
def test_unresolved_round_duration_stops_at_the_last_observed_chat_message(status):
    assert metrics([], status=status)["duration_seconds"] == 480


def test_absent_invalid_and_negative_source_times_stay_unknown():
    result = metrics([message(2, "agent")], handoff_requested_at="invalid", started_at="invalid", last_message_at=None, resolved_at=None)
    assert result["duration_seconds"] is None and result["first_response_seconds"] is None
    result = metrics([], resolved_at=START - timedelta(minutes=1))
    assert result["duration_seconds"] is None


def test_zero_second_response_and_subsecond_average_remain_valid():
    result = metrics([message(1), message(1, "agent"), message(2), message(2 + .5/60, "agent")])
    assert result["first_response_status"] == "recorded" and result["first_response_seconds"] == 0
    assert result["average_response_seconds"] == .25
