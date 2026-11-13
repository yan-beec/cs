"""Window continuation must agree with the existing full-history semantics."""

from copy import deepcopy
from datetime import timedelta
import json

import pytest

from quality.processing.projector import project_conversation, timestamp
from quality.processing.windows import event_locator, message_locators, round_locators
from quality.tests.test_processing import START, event


def without_checkpoint(row):
    row = deepcopy(row)
    row["data"].pop("replay_seed", None)
    return row


def idle_history(count):
    events = []
    for index in range(count):
        events.append(event(index * 10 + 1, minute=index * 361))
        if index == 0:
            events.append(event(2, minute=1, sender="user", direction="outgoing"))
    return events


def test_saved_boundary_state_preserves_human_takeover_and_absolute_round_numbers():
    events = idle_history(62)
    full = project_conversation(events)
    boundary = full.rounds[50]["started_at"]
    first = project_conversation([item for item in events if timestamp(item["payload"]["created_at"]) < boundary],
                                 following_started_at=boundary)
    second = project_conversation([item for item in events if timestamp(item["payload"]["created_at"]) >= boundary],
                                  replay_seed=first.continuation, round_no_offset=50)
    assert [without_checkpoint(row) for row in first.rounds + second.rounds] == [without_checkpoint(row) for row in full.rounds]
    assert second.rounds[0]["round_no"] == 51
    assert second.rounds[0]["reception_type"] == "human"


def test_resolution_at_boundary_resets_previously_confirmed_human_takeover():
    first_events = [event(1), event(2, 1, sender="user", direction="outgoing"),
                    event(3, 2, event_type="conversation_status_changed", status="resolved")]
    next_events = [event(4, 3)]
    first = project_conversation(first_events, following_started_at=START + timedelta(minutes=3))
    continued = project_conversation(next_events, replay_seed=first.continuation, round_no_offset=1)
    full = project_conversation(first_events + next_events)
    assert without_checkpoint(continued.rounds[0]) == without_checkpoint(full.rounds[1])
    assert continued.rounds[0]["reception_type"] == "ai_only"


def test_embedded_old_message_can_update_state_without_becoming_a_new_message():
    old = event(1)
    current = event(2, 361)
    observation = event(3, 362, event_type="message_updated", message_id=1,
                        created_at=START.isoformat(), updated_at=(START + timedelta(minutes=362)).isoformat())
    observation["payload"]["conversation"].update(
        updated_at=(START + timedelta(minutes=362)).isoformat(), custom_attributes={"handoff_requested": True})
    observation.update(_message_in_window=False, _state_in_window=True, _rating_in_window=False)
    seed = project_conversation([old], following_started_at=START + timedelta(minutes=361)).continuation
    result = project_conversation([current, observation], replay_seed=seed, round_no_offset=1)
    assert [row["message_id"] for row in result.messages] == [2]
    assert result.rounds[0]["reception_type"] == "human"


@pytest.mark.parametrize("variant", ["idle", "resolved", "duplicate", "late_time", "partial", "out_of_order"])
def test_lightweight_round_locators_match_full_source_replay(variant):
    if variant == "idle":
        events = idle_history(70)
    elif variant == "resolved":
        events = [event(1), event(2, 1, event_type="conversation_status_changed", status="resolved"), event(3, 2)]
    elif variant == "duplicate":
        events = [event(1), event(2, message_id=1), event(3, 361)]
    elif variant == "late_time":
        original = event(1)
        original["payload"].pop("created_at")
        events = [original, event(2, 1, event_type="message_updated", message_id=1), event(3, 362)]
    elif variant == "partial":
        events = [event(1, event_type="message_updated"), event(2, 1, sender="user", direction="outgoing")]
    else:
        events = [event(9, 722), event(8, 361), event(7), event(10, 1, sender="user", direction="outgoing")]
    facts = [event_locator({**item, "payload_bytes": len(json.dumps(item["payload"]).encode())}) for item in events]
    facts.sort(key=lambda item: (item["message_id"] or 0, item["event_at"], item["event_id"]))
    messages = list(message_locators(row for row in facts if row["message_id"] is not None))
    actions = [{"kind": "customer", "at": row["creation_at"], "message_id": row["message_id"]}
               for row in messages if row["is_customer_creation"]]
    actions += [{"kind": "resolution", "at": row["state_at"], "message_id": row["event_id"]}
                for row in facts if row["is_resolution"]]
    actions.sort(key=lambda row: (row["at"], row["kind"] == "resolution", row["message_id"]))
    anchors = list(round_locators(actions, ("test-source", 1, 12), "customer", 21600))
    full = project_conversation(events)
    assert [(row["round_id"], row["round_no"], row["started_at"]) for row in anchors] == [
        (row["round_id"], row["round_no"], row["started_at"]) for row in full.rounds]
