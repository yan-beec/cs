"""Conversation semantics without database or customer-service resources."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from quality.processing.config import ProcessingSettings
from quality.processing.projector import project_conversation


START = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)


def event(event_id, minute=0, *, event_type="message_created", message_id=None,
          sender="contact", direction="incoming", conversation_id=12, source="test-source", **payload):
    at = START + timedelta(minutes=minute)
    data = {"event": event_type, "account": {"id": 1}}
    if event_type.startswith("message_"):
        data.update(id=message_id or event_id, created_at=at.isoformat(), content=f"message {event_id}",
                    message_type=direction, sender={"id": 42 if sender == "contact" else 7, "type": sender, "name": sender},
                    conversation={"id": conversation_id, "account_id": 1, "inbox_id": 3,
                                  "meta": {"sender": {"id": 42, "name": "客户甲", "type": "contact"}}})
    else:
        data.update(id=conversation_id, updated_at=at.isoformat(), inbox_id=3,
                    meta={"sender": {"id": 42, "type": "contact", "name": "客户甲"}})
    data.update(payload)
    return {"id": event_id, "source_key": source, "account_id": 1, "conversation_id": conversation_id,
            "message_id": message_id or event_id if event_type.startswith("message_") else None,
            "event_type": event_type, "ingestion_kind": "webhook", "received_at": at + timedelta(seconds=2),
            "source_time": None, "payload": data}


def project(*events, **kwargs):
    return project_conversation(list(events), channel_bindings={("1", "3"): "xingliao"}, **kwargs)


def agent_reply(event_id, minute=0, *, agent_id=7, agent_name="实际回复客服", **payload):
    item = event(event_id, minute, sender="user", direction="outgoing", **payload)
    item["payload"]["sender"] = {"id": agent_id, "name": agent_name, "type": "user"}
    return item


def ics_assignment(event_id, minute=1, *, agent_id=8, trigger_message_id=1,
                   business_round_id="ics-session-1", session_minute=0, evidence_kind="assignment_applied", **values):
    at = START + timedelta(minutes=minute)
    item = event(event_id, minute, event_type="ics_assignment_recorded")
    item["ingestion_kind"] = "backfill"
    item["payload"] = {"event": "ics_assignment_recorded", "account": {"id": 1},
        "conversation": {"id": item["conversation_id"], "account_id": 1}, "updated_at": at.isoformat(),
        "assignment": {"schema_version": 1, "origin": "ics", "assignment_key": business_round_id,
            "business_round_id": business_round_id, "agent_id": str(agent_id),
            "trigger_message_id": str(trigger_message_id),
            "session_started_at": (START + timedelta(minutes=session_minute)).isoformat(),
            "assigned_at": at.isoformat() if evidence_kind == "assignment_applied" else None,
            "recorded_at": at.isoformat(), "evidence_kind": evidence_kind}}
    item["payload"]["assignment"].update(values)
    return item


class ProcessingProjectionTest(unittest.TestCase):
    def test_contact_profile_keeps_chatwoot_user_phone_and_note(self):
        incoming = event(1)
        incoming["payload"]["sender"].update(
            identifier="h5-user-42",
            phone_number="+86 13800000000",
            additional_attributes={"description": "VIP 联系人备注"},
            custom_attributes={"app_user_id": "visitor-42"},
        )
        result = project(incoming)
        self.assertEqual(result.customer["contact_id"], "42")
        self.assertEqual(result.customer["data"]["h5_user_id"], "h5-user-42")
        self.assertEqual(result.customer["data"]["chatwoot_user_id"], "42")
        self.assertEqual(result.customer["data"]["phone_number"], "+86 13800000000")
        self.assertEqual(result.customer["data"]["contact_note"], "VIP 联系人备注")

    def test_real_chatwoot_untyped_incoming_contact_starts_mapped_round(self):
        incoming = event(1, message_id=100, created_at="2026-10-01T08:00:00.000Z")
        incoming["payload"]["sender"] = {
            "id": 42, "name": "Synthetic contact", "email": None, "avatar": "", "account": {"id": 1},
            "blocked": False, "thumbnail": "", "identifier": None, "phone_number": None,
            "custom_attributes": {}, "additional_attributes": {},
        }
        incoming["payload"]["inbox"] = {"id": 3, "name": "Test inbox"}
        result = project(incoming, event(2, 1, sender="agent_bot", direction="outgoing"),
                         event(3, 2, sender="user", direction="outgoing"))
        self.assertEqual(len(result.rounds), 1)
        self.assertEqual(result.rounds[0]["channel_key"], "xingliao")
        self.assertEqual(result.rounds[0]["data"]["counts"], {"contact": 1, "bot": 1, "agent": 1, "private": 0, "attachments": 0})
        self.assertEqual(result.customer["contact_id"], "42")
        self.assertEqual(result.snapshots[0]["sender_role"], "contact")
        self.assertEqual(result.messages[0]["sender_role"], "contact")
        self.assertEqual(result.rounds[0]["reception_type"], "human")
        self.assertEqual(result.agents[0]["agent_id"], "7")
        self.assertTrue(result.agents[0]["replied"])

    def test_explicit_unknown_incoming_type_does_not_get_missing_type_fallback(self):
        incoming = event(1, sender="mystery")
        result = project(incoming)
        self.assertEqual(result.messages[0]["sender_role"], "unknown")
        self.assertEqual(result.rounds, [])

    def test_untyped_incoming_sender_can_supply_contact_without_nested_metadata(self):
        incoming = event(1)
        incoming["payload"]["sender"].pop("type")
        incoming["payload"]["conversation"].pop("meta")
        result = project(incoming)
        self.assertEqual(result.customer["contact_id"], "42")
        self.assertEqual(result.customer["display_name"], "contact")
        self.assertEqual(len(result.rounds), 1)
        self.assertEqual(result.messages[0]["sender_role"], "contact")

    def test_untyped_incoming_can_use_conversation_contact_but_rejects_conflicting_sender(self):
        missing_sender = event(1)
        missing_sender["payload"].pop("sender")
        result = project(missing_sender)
        self.assertEqual(result.messages[0]["sender_role"], "contact")
        self.assertEqual(len(result.rounds), 1)
        conflicting = event(2)
        conflicting["payload"]["sender"].pop("type")
        conflicting["payload"]["sender"]["id"] = 999
        result = project(conflicting)
        self.assertEqual(result.messages[0]["sender_role"], "unknown")
        self.assertEqual(result.rounds, [])

    def test_untyped_outgoing_activity_and_unidentified_incoming_do_not_guess_roles(self):
        for direction, expected in (("outgoing", "unknown"), ("activity", "system")):
            with self.subTest(direction=direction):
                outgoing = event(2, 1, direction=direction)
                outgoing["payload"]["sender"].pop("type")
                result = project(event(1), outgoing)
                self.assertEqual(result.messages[-1]["sender_role"], expected)
                self.assertEqual(result.rounds[0]["data"]["counts"]["agent"], 0)
                self.assertFalse(result.rounds[0]["data"]["has_agent_reply"])
                self.assertEqual(result.agents, [])
        incoming = event(1)
        incoming["payload"].pop("sender")
        incoming["payload"]["conversation"].pop("meta")
        result = project(incoming)
        self.assertEqual(result.messages[0]["sender_role"], "unknown")
        self.assertEqual(result.rounds, [])

    def test_untyped_contact_same_timestamp_create_update_counts_once_and_resolution_splits(self):
        created = event(2, message_id=100)
        updated = event(1, message_id=100, event_type="message_updated", updated_at=START.isoformat(), content="edited")
        for item in (created, updated):
            item["payload"]["sender"].pop("type")
            item["payload"]["conversation"]["meta"]["sender"].pop("type")
        reopened = event(4, 2, message_id=101)
        reopened["payload"]["sender"].pop("type")
        result = project(updated, created, event(3, 1, event_type="conversation_status_changed", status="resolved"), reopened)
        self.assertEqual(len(result.rounds), 2)
        self.assertEqual([row["status"] for row in result.rounds], ["resolved", "active"])
        self.assertEqual([row["data"]["counts"]["contact"] for row in result.rounds], [1, 1])
        self.assertEqual(len(result.messages), 2)
        self.assertTrue(all(row["data"]["has_creation_event"] for row in result.messages))
        self.assertEqual(result.messages[0]["round_id"], result.rounds[0]["round_id"])
        self.assertEqual(result.messages[1]["round_id"], result.rounds[1]["round_id"])

    def test_ai_and_human_reply_composition_is_independent_from_reception(self):
        result = project(event(1), event(2, 1, sender="agent_bot", direction="outgoing"),
                         event(3, 2, sender="user", direction="outgoing"))
        round_row = result.rounds[0]
        self.assertEqual((round_row["reception_type"], round_row["reply_composition"]), ("human", "mixed"))
        self.assertTrue(round_row["data"]["has_agent_reply"])
        self.assertEqual(round_row["data"]["counts"], {"contact": 1, "bot": 1, "agent": 1, "private": 0, "attachments": 0})
        self.assertEqual(result.agents[0]["reply_count"], 1)

    def test_ics_responsibility_survives_reassignment_other_replies_and_csat(self):
        events = [event(1), event(2, 1, event_type="conversation_updated", assignee={"id": 8, "name": "分配客服A"}),
            agent_reply(3, 2, agent_id=9, agent_name="实际客服B"),
            event(4, 3, content_type="input_csat", sender="agent_bot", direction="outgoing",
                content_attributes={"ics_assigned_agent_id": "10"}),
            ics_assignment(5, 1, agent_id=8),
            event(6, 2, event_type="conversation_updated", assignee={"id": 9}),
            agent_reply(7, 4, agent_id=10, agent_name="实际客服C")]
        original = deepcopy(events)
        result = project(*events)
        data = result.rounds[0]["data"]
        self.assertEqual((data["responsible_agent_id"], data["responsible_agent_name"]), ("8", None))
        self.assertEqual(data["responsible_assigned_at"], (START + timedelta(minutes=1)).isoformat())
        self.assertEqual(data["responsibility_event_id"], 5)
        self.assertEqual(data["responsibility_external_round_id"], "ics-session-1")
        self.assertEqual(data["responsibility_trigger_message_id"], "1")
        self.assertEqual(data["responsibility_basis"], "ics_assignment")
        self.assertEqual((data["last_public_agent_id"], data["last_public_agent_name"], data["last_public_message_id"]), ("10", "实际客服C", 7))
        self.assertEqual(data["last_public_reply_at"], (START + timedelta(minutes=4)).isoformat())
        self.assertEqual(data["current_assignee_id"], "9")
        self.assertNotIn("responsible_reply_at", data)
        self.assertNotIn("responsible_message_id", data)
        self.assertEqual(events, original)

    def test_last_public_reply_can_change_a_to_b_and_back_without_changing_ics_responsibility(self):
        result = project(event(1), agent_reply(2, 1, agent_id=8, agent_name="客服A"),
            agent_reply(3, 2, agent_id=9, agent_name="客服B"), agent_reply(4, 3, agent_id=8, agent_name="客服A"),
            ics_assignment(5, 0, agent_id=9))
        data = result.rounds[0]["data"]
        self.assertEqual((data["last_public_agent_id"], data["last_public_agent_name"], data["last_public_message_id"]), ("8", "客服A", 4))
        self.assertEqual(data["last_public_reply_at"], (START + timedelta(minutes=3)).isoformat())
        self.assertEqual((data["responsible_agent_id"], data["responsible_agent_name"]), ("9", "客服B"))
        self.assertEqual({row["agent_id"]: row["reply_count"] for row in result.agents}, {"8": 2, "9": 1})

    def test_private_bot_customer_system_csat_and_incoming_agent_do_not_replace_last_public_reply(self):
        incoming_agent = agent_reply(9, 8, agent_id=99)
        incoming_agent["payload"]["message_type"] = "incoming"
        result = project(event(1), agent_reply(2, 1, agent_id=8, agent_name="公开回复A"),
            agent_reply(3, 2, agent_id=9, private=True), event(4, 3, sender="agent_bot", direction="outgoing"),
            event(5, 4), event(6, 5, sender="system", direction="outgoing"),
            agent_reply(7, 6, agent_id=99, content_type="input_csat"),
            event(8, 7, sender="user", direction="activity"), incoming_agent,
            event(10, 9, sender="mystery", direction="outgoing"))
        data = result.rounds[0]["data"]
        self.assertEqual((data["last_public_agent_id"], data["last_public_agent_name"], data["last_public_message_id"]), ("8", "公开回复A", 2))
        self.assertEqual(data["last_public_reply_at"], (START + timedelta(minutes=1)).isoformat())
        self.assertEqual(data["responsibility_basis"], "assignment_unobserved")
        self.assertEqual(result.rounds[0]["reception_type"], "human")

    def test_assignment_confirmed_takeover_and_csat_without_public_reply_have_no_responsible_agent(self):
        result = project(event(1),
            event(2, 1, event_type="conversation_updated", assignee={"id": 8}, custom_attributes={"handoff_accepted": True}),
            agent_reply(3, 2, agent_id=8, private=True),
            event(4, 3, content_type="input_csat", sender="agent_bot", direction="outgoing", content_attributes={"ics_assigned_agent_id": "8"}))
        data = result.rounds[0]["data"]
        self.assertEqual(result.rounds[0]["reception_type"], "human")
        self.assertEqual(data["responsibility_basis"], "assignment_unobserved")
        self.assertTrue(all(data[key] is None for key in ("responsible_agent_id", "responsible_agent_name", "responsible_assigned_at", "responsibility_event_id", "last_public_reply_at", "last_public_message_id")))

    def test_responsibility_is_not_inherited_across_idle_or_resolved_rounds(self):
        for boundary in ("idle", "resolved"):
            with self.subTest(boundary=boundary):
                events = [event(1), event(2, 0, event_type="conversation_updated", assignee={"id": 8}),
                    agent_reply(3, 1, agent_id=9, agent_name="首轮客服B"), ics_assignment(10, 0, agent_id=8)]
                if boundary == "resolved":
                    events.append(event(4, 2, event_type="conversation_status_changed", status="resolved"))
                events.append(event(5, 360 if boundary == "idle" else 3))
                result = project(*events)
                first, second = result.rounds
                self.assertEqual(first["data"]["responsible_agent_id"], "8")
                self.assertEqual(first["data"]["last_public_agent_id"], "9")
                self.assertEqual(second["data"]["responsibility_basis"], "assignment_unobserved")
                self.assertTrue(all(second["data"][key] is None for key in ("responsible_agent_id", "responsible_agent_name", "responsible_assigned_at", "responsibility_event_id", "last_public_reply_at", "last_public_message_id")))
                self.assertEqual(second["reception_type"], "human" if boundary == "idle" else "ai_only")
                self.assertEqual(second["data"]["takeover_inherited"], boundary == "idle")
                next_result = project(*events, agent_reply(6, 361 if boundary == "idle" else 4, agent_id=10, agent_name="新轮客服C"))
                self.assertEqual([row["data"]["responsible_agent_id"] for row in next_result.rounds], ["8", None])
                self.assertEqual([row["data"]["last_public_agent_id"] for row in next_result.rounds], ["9", "10"])

    def test_last_public_human_with_unknown_identity_does_not_fall_back_to_assignment_or_earlier_reply(self):
        for missing_id in (None, "", 0, "invalid", True):
            with self.subTest(sender_id=missing_id):
                result = project(event(1), event(2, 0, event_type="conversation_updated", assignee={"id": 8}),
                    agent_reply(3, 1, agent_id=9, agent_name="较早客服B"),
                    agent_reply(4, 2, agent_id=missing_id, agent_name="只观察到姓名"),
                    event(5, 3, sender="agent_bot", direction="outgoing"))
                data = result.rounds[0]["data"]
                self.assertIsNone(data["last_public_agent_id"])
                self.assertEqual(data["last_public_agent_name"], "只观察到姓名")
                self.assertEqual(data["last_public_reply_at"], (START + timedelta(minutes=2)).isoformat())
                self.assertEqual(data["last_public_message_id"], 4)
                self.assertIsNone(data["responsible_agent_id"])
                self.assertEqual(data["responsibility_basis"], "assignment_unobserved")
                self.assertEqual(data["current_assignee_id"], "8")
                self.assertTrue(data["has_agent_reply"])

    def test_edited_old_reply_and_update_only_observation_do_not_outrank_later_sent_reply(self):
        for has_creation in (True, False):
            with self.subTest(has_creation=has_creation):
                events = [event(1), agent_reply(3, 3, message_id=300, agent_id=9, agent_name="后发客服B"),
                    agent_reply(4, 1, message_id=200, event_type="message_updated", agent_id=8, agent_name="早发客服A",
                        updated_at=(START + timedelta(minutes=10)).isoformat())]
                if has_creation:
                    events.append(agent_reply(2, 1, message_id=200, agent_id=8, agent_name="早发客服A"))
                result = project(*reversed(events))
                data = result.rounds[0]["data"]
                self.assertEqual((data["last_public_agent_id"], data["last_public_message_id"]), ("9", 300))
                self.assertEqual(data["last_public_reply_at"], (START + timedelta(minutes=3)).isoformat())
                self.assertEqual(data["counts"]["agent"], 2)
                self.assertEqual(result, project(*events))

    def test_update_only_final_public_reply_records_original_creation_time_once(self):
        result = project(event(1), agent_reply(2, 1, message_id=100, agent_id=8),
            agent_reply(3, 2, message_id=200, event_type="message_updated", agent_id=9, agent_name="补观察客服B",
                updated_at=(START + timedelta(minutes=4)).isoformat()),
            agent_reply(4, 2, message_id=200, event_type="message_updated", agent_id=9, agent_name="补观察客服B",
                updated_at=(START + timedelta(minutes=5)).isoformat()))
        data = result.rounds[0]["data"]
        self.assertEqual((data["last_public_agent_id"], data["last_public_agent_name"], data["last_public_message_id"]), ("9", "补观察客服B", 200))
        self.assertEqual(data["last_public_reply_at"], (START + timedelta(minutes=2)).isoformat())
        self.assertEqual(data["observed_only_message_count"], 1)
        self.assertEqual(data["counts"]["agent"], 2)

    def test_same_timestamp_last_public_reply_uses_source_message_id_despite_reversed_delivery(self):
        result = project(agent_reply(2, 1, message_id=300, agent_id=9, agent_name="高消息ID客服B"),
            event(1, message_id=100), agent_reply(10, 1, message_id=200, agent_id=8, agent_name="低消息ID客服A"))
        data = result.rounds[0]["data"]
        self.assertEqual((data["last_public_agent_id"], data["last_public_message_id"]), ("9", 300))
        self.assertEqual(data["last_public_reply_at"], (START + timedelta(minutes=1)).isoformat())

    def test_later_creation_time_recovery_does_not_move_old_reply_past_newer_reply(self):
        original = agent_reply(2, 1, message_id=200, agent_id=8, agent_name="原始早发客服A")
        original["payload"].pop("created_at")
        result = project(event(1), original,
            agent_reply(3, 3, message_id=300, agent_id=9, agent_name="后发客服B"),
            agent_reply(4, 1, message_id=200, event_type="message_updated", agent_id=10, agent_name="编辑观察客服C",
                updated_at=(START + timedelta(minutes=10)).isoformat()))
        data = result.rounds[0]["data"]
        self.assertEqual((data["last_public_agent_id"], data["last_public_message_id"]), ("9", 300))
        self.assertEqual(data["last_public_reply_at"], (START + timedelta(minutes=3)).isoformat())
        self.assertEqual({row["agent_id"] for row in result.agents}, {"8", "9"})
        self.assertIsNone(result.snapshots[1]["sent_at"])
        self.assertEqual(result.snapshots[1]["sender_id"], "8")

    def test_public_reply_without_source_creation_time_does_not_invent_last_public_reply(self):
        missing_time = agent_reply(2, 1, agent_id=8)
        missing_time["payload"].pop("created_at")
        result = project(event(1), missing_time)
        data = result.rounds[0]["data"]
        self.assertEqual(data["responsibility_basis"], "assignment_unobserved")
        self.assertIsNone(data["responsible_agent_id"])
        self.assertIsNone(data["last_public_reply_at"])

    def test_ics_assignment_without_public_reply_confirms_responsibility_only(self):
        result = project(event(1), ics_assignment(2))
        data = result.rounds[0]["data"]
        self.assertEqual(data["responsible_agent_id"], "8")
        self.assertEqual(data["responsibility_basis"], "ics_assignment")
        self.assertIsNone(data["responsible_agent_name"])
        self.assertIsNone(data["last_public_agent_id"])
        self.assertIsNone(data["last_public_message_id"])
        self.assertEqual(result.rounds[0]["reception_type"], "ai_only")
        self.assertFalse(data["has_agent_reply"])
        self.assertEqual(data["counts"]["agent"], 0)

    def test_no_ics_assignment_cannot_infer_responsibility_from_any_reply_or_assignee(self):
        result = project(event(1),
            event(2, 1, event_type="conversation_updated", assignee={"id": 8, "name": "分配人"}),
            agent_reply(3, 2, agent_id=9),
            event(4, 3, content_type="input_csat", sender="agent_bot", direction="outgoing",
                  content_attributes={"ics_assigned_agent_id": "8", "ics_round_id": "session-old"}))
        data = result.rounds[0]["data"]
        self.assertEqual(data["responsibility_basis"], "assignment_unobserved")
        self.assertIsNone(data["responsible_agent_id"])
        self.assertEqual(data["last_public_agent_id"], "9")
        self.assertEqual(data["current_assignee_id"], "8")

    def test_first_ics_assignment_uses_actual_assignment_time_then_event_id(self):
        events = [event(1), ics_assignment(20, 5, agent_id=9, business_round_id="later"),
                  ics_assignment(30, 2, agent_id=8, business_round_id="first")]
        events[1]["received_at"] = START + timedelta(minutes=6)
        events[2]["received_at"] = START + timedelta(days=1)
        result = project(*reversed(events))
        self.assertEqual(result.rounds[0]["data"]["responsible_agent_id"], "8")
        self.assertEqual(result.rounds[0]["data"]["responsibility_event_id"], 30)
        self.assertEqual(result, project(*events))
        tied = project(event(1), ics_assignment(30, 2, agent_id=8, business_round_id="tie-high"),
                       ics_assignment(20, 2, agent_id=9, business_round_id="tie-low"))
        self.assertEqual(tied.rounds[0]["data"]["responsible_agent_id"], "9")
        self.assertEqual(tied.rounds[0]["data"]["responsibility_event_id"], 20)

    def test_delayed_ics_evidence_backfills_only_anchored_original_round(self):
        events = [event(1), event(2, 2, event_type="conversation_status_changed", status="resolved"),
                  event(3, 3), agent_reply(4, 4, agent_id=9),
                  ics_assignment(30, 1, agent_id=8)]
        events[-1]["received_at"] = START + timedelta(days=1)
        result = project(*events)
        self.assertEqual([row["data"]["responsible_agent_id"] for row in result.rounds], ["8", None])
        self.assertEqual([row["data"]["last_public_agent_id"] for row in result.rounds], [None, "9"])
        self.assertEqual(result, project(*reversed(events)))

    def test_independent_ics_assignments_bind_each_resolved_or_idle_round(self):
        for boundary in ("idle", "resolved"):
            with self.subTest(boundary=boundary):
                second_start = 360 if boundary == "idle" else 3
                events = [event(1), ics_assignment(10, 1, agent_id=8)]
                if boundary == "resolved":
                    events.append(event(2, 2, event_type="conversation_status_changed", status="resolved"))
                events.extend([event(3, second_start),
                    ics_assignment(20, second_start + 1, agent_id=9, trigger_message_id=3,
                                   session_minute=second_start, business_round_id="ics-session-2")])
                result = project(*events)
                self.assertEqual([row["data"]["responsible_agent_id"] for row in result.rounds], ["8", "9"])
                self.assertEqual([row["data"]["responsibility_external_round_id"] for row in result.rounds], ["ics-session-1", "ics-session-2"])

    def test_current_control_backfill_preserves_unknown_assignment_time_and_original_round(self):
        observed = ics_assignment(30, 700, agent_id=8, evidence_kind="current_control_backfill")
        result = project(event(1), event(2, 360), observed)
        data = result.rounds[0]["data"]
        self.assertEqual(data["responsible_agent_id"], "8")
        self.assertEqual(data["responsibility_basis"], "ics_assignment")
        self.assertIsNone(data["responsible_assigned_at"])
        self.assertEqual(data["responsibility_event_id"], 30)
        self.assertEqual(result.rounds[1]["data"]["responsibility_basis"], "assignment_unobserved")
        self.assertIsNone(result.rounds[1]["data"]["responsible_agent_id"])

    def test_same_key_duplicate_evidence_is_stable_and_does_not_duplicate_messages(self):
        events = [event(1), ics_assignment(20), ics_assignment(10)]
        result = project(*events)
        self.assertEqual(result.rounds[0]["data"]["responsibility_event_id"], 10)
        self.assertEqual(result.rounds[0]["data"]["responsible_agent_id"], "8")
        self.assertEqual(result.rounds[0]["data"]["counts"]["contact"], 1)
        self.assertEqual(len(result.messages), 1)
        self.assertEqual(result, project(*reversed(events)))

    def test_same_key_conflicting_agents_leave_responsibility_unset(self):
        result = project(event(1), ics_assignment(2, agent_id=8), ics_assignment(3, agent_id=9),
                         agent_reply(4, 2, agent_id=10))
        data = result.rounds[0]["data"]
        self.assertEqual(data["responsibility_basis"], "assignment_conflict")
        self.assertTrue(all(data[key] is None for key in ("responsible_agent_id", "responsible_agent_name",
            "responsible_assigned_at", "responsibility_event_id", "responsibility_external_round_id",
            "responsibility_trigger_message_id")))
        self.assertEqual(data["last_public_agent_id"], "10")

    def test_same_key_conflicting_trigger_or_session_cannot_choose_one_record(self):
        for changes in ({"trigger_message_id": 1}, {"session_minute": 0.25}):
            with self.subTest(changes=changes):
                result = project(event(1), event(2, 0.5), ics_assignment(10, trigger_message_id=2),
                    ics_assignment(20, **changes))
                self.assertEqual(result.rounds[0]["data"]["responsibility_basis"], "assignment_conflict")
                self.assertIsNone(result.rounds[0]["data"]["responsible_agent_id"])

    def test_same_key_cannot_claim_multiple_rounds(self):
        result = project(event(1), event(2, 2, event_type="conversation_status_changed", status="resolved"),
            event(3, 3), ics_assignment(10, 1),
            ics_assignment(20, 4, trigger_message_id=3, session_minute=3))
        self.assertEqual([row["data"]["responsibility_basis"] for row in result.rounds], ["assignment_conflict", "assignment_conflict"])
        self.assertTrue(all(row["data"]["responsible_agent_id"] is None for row in result.rounds))

    def test_cross_round_trigger_session_or_assignment_time_cannot_leak_responsibility(self):
        for changes in ({"session_minute": 3, "minute": 4}, {"minute": 4}):
            with self.subTest(changes=changes):
                result = project(event(1), event(2, 2, event_type="conversation_status_changed", status="resolved"),
                    event(3, 3), ics_assignment(10, **changes))
                self.assertTrue(all(row["data"]["responsible_agent_id"] is None for row in result.rounds))
                self.assertTrue(all(row["data"]["responsibility_basis"] == "assignment_conflict" for row in result.rounds))

    def test_assignment_in_closed_gap_or_before_trigger_is_conflicting(self):
        for assigned_minute in (0.5, 2.5):
            with self.subTest(assigned_minute=assigned_minute):
                result = project(event(1), event(2, 1),
                    event(3, 2, event_type="conversation_status_changed", status="resolved"),
                    event(4, 3), ics_assignment(10, assigned_minute, trigger_message_id=2))
                self.assertEqual(result.rounds[0]["data"]["responsibility_basis"], "assignment_conflict")
                self.assertIsNone(result.rounds[0]["data"]["responsible_agent_id"])
                self.assertIsNone(result.rounds[1]["data"]["responsible_agent_id"])

    def test_missing_or_noncustomer_trigger_is_conflict_until_real_anchor_is_observed(self):
        assigned = ics_assignment(10, 2, trigger_message_id=2)
        missing = project(event(1), assigned)
        self.assertEqual(missing.rounds[0]["data"]["responsibility_basis"], "assignment_conflict")
        for anchor in (agent_reply(2, 1), event(2, 1, private=True),
                       event(2, 1, content_type="input_csat")):
            with self.subTest(anchor=anchor["payload"]):
                result = project(event(1), assigned, anchor)
                self.assertEqual(result.rounds[0]["data"]["responsibility_basis"], "assignment_conflict")
                self.assertIsNone(result.rounds[0]["data"]["responsible_agent_id"])
        recovered = project(event(1), assigned, event(2, 1))
        self.assertEqual(recovered.rounds[0]["data"]["responsible_agent_id"], "8")

    def test_invalid_assignment_contract_fields_are_ignored_without_poisoning_valid_evidence(self):
        changes = [{"origin": "chatwoot"}, {"schema_version": True}, {"schema_version": 2},
            {"assignment_key": ""}, {"assignment_key": " another "}, {"business_round_id": "different"},
            {"agent_id": 8}, {"agent_id": "0"}, {"agent_id": "invalid"}, {"trigger_message_id": None},
            {"session_started_at": None}, {"session_started_at": START.replace(tzinfo=None).isoformat()},
            {"assigned_at": None}, {"assigned_at": 123}, {"assigned_at": "2026-10-01T08:01:00"},
            {"evidence_kind": "ordinary_assignment"}, {"evidence_kind": {}}, {"evidence_kind": []},
            {"evidence_kind": "current_control_backfill", "assigned_at": None, "recorded_at": None},
            {"evidence_kind": "current_control_backfill", "assigned_at": None, "recorded_at": "2026-10-01T08:01:00"},
            {"evidence_kind": "current_control_backfill"}]
        for fields in changes:
            with self.subTest(fields=fields):
                malformed = ics_assignment(3, agent_id=9)
                malformed["payload"]["assignment"].update(fields)
                untouched = deepcopy(malformed)
                absent = project(event(1), malformed)
                self.assertEqual(absent.rounds[0]["data"]["responsibility_basis"], "assignment_unobserved")
                valid = project(event(1), ics_assignment(2), malformed)
                self.assertEqual(valid.rounds[0]["data"]["responsible_agent_id"], "8")
                self.assertEqual(valid.rounds[0]["data"]["responsibility_basis"], "ics_assignment")
                self.assertEqual(malformed, untouched)
        for field in ("schema_version", "origin", "assignment_key", "business_round_id", "agent_id",
                      "trigger_message_id", "session_started_at", "assigned_at"):
            with self.subTest(missing=field):
                malformed = ics_assignment(3)
                malformed["payload"]["assignment"].pop(field)
                self.assertEqual(project(event(1), malformed).rounds[0]["data"]["responsibility_basis"], "assignment_unobserved")

    def test_assignment_scope_and_ingestion_provenance_must_match_archive(self):
        for path, value in (("account", 2), ("conversation", 13), ("conversation_account", 2),
                            ("event", "conversation_updated"), ("ingestion_kind", "webhook")):
            with self.subTest(path=path):
                malformed = ics_assignment(3)
                if path == "account":
                    malformed["payload"]["account"]["id"] = value
                elif path == "conversation":
                    malformed["payload"]["conversation"]["id"] = value
                elif path == "conversation_account":
                    malformed["payload"]["conversation"]["account_id"] = value
                elif path == "event":
                    malformed["payload"]["event"] = value
                else:
                    malformed[path] = value
                result = project(event(1), malformed)
                self.assertEqual(result.rounds[0]["data"]["responsibility_basis"], "assignment_unobserved")
                self.assertIsNone(result.rounds[0]["data"]["responsible_agent_id"])
        mixed_source = ics_assignment(3)
        mixed_source["source_key"] = "another-source"
        with self.assertRaises(ValueError):
            project(event(1), mixed_source)

    def test_initial_v1_assignment_contract_remains_compatible_without_evidence_kind(self):
        assigned = ics_assignment(2)
        assigned["payload"]["assignment"].pop("evidence_kind")
        assigned["payload"]["assignment"].pop("recorded_at")
        data = project(event(1), assigned).rounds[0]["data"]
        self.assertEqual(data["responsibility_basis"], "ics_assignment")
        self.assertEqual(data["responsible_agent_id"], "8")

    def test_trigger_uses_original_customer_identity_after_edit_and_names_use_same_agent_messages(self):
        assigned = ics_assignment(10)
        events = [event(1, content="原始问题"), assigned,
            event(2, event_type="message_updated", message_id=1, sender="user", direction="outgoing",
                  updated_at=(START + timedelta(minutes=2)).isoformat(), content="编辑后内容"),
            agent_reply(3, 3, agent_id=8, agent_name="责任人实名", private=True),
            agent_reply(4, 4, agent_id=9, agent_name="代答人")]
        original = deepcopy(events)
        result = project(*events)
        data = result.rounds[0]["data"]
        self.assertEqual((data["responsible_agent_id"], data["responsible_agent_name"]), ("8", "责任人实名"))
        self.assertEqual((data["last_public_agent_id"], data["last_public_agent_name"]), ("9", "代答人"))
        self.assertEqual(events, original)
        creation = next(row for row in result.snapshots if row["event_id"] == 1)
        self.assertEqual((creation["sender_role"], creation["content"]), ("contact", "原始问题"))

    def test_request_classifies_human_without_proving_takeover_or_splitting_round(self):
        result = project(event(1, content="我要转人工"), event(2, 1, sender="agent_bot", direction="outgoing"),
            event(3, 2, event_type="conversation_updated", assignee={"id": 8, "name": "坐席"}),
            event(4, 3, sender="user", direction="outgoing", private=True))
        self.assertEqual(len(result.rounds), 1)
        self.assertEqual(result.rounds[0]["reception_type"], "human")
        self.assertFalse(result.rounds[0]["data"]["has_agent_reply"])
        self.assertEqual(result.rounds[0]["data"]["handoff_result"], "requested")
        self.assertNotIn("handoff_completed_at", result.rounds[0]["data"])
        self.assertEqual(result.rounds[0]["data"]["current_assignee_id"], "8")
        self.assertEqual(result.rounds[0]["data"]["counts"]["private"], 1)
        self.assertTrue(any(row["was_assigned"] for row in result.agents))
        self.assertTrue(any(row["noted"] for row in result.agents))

    def test_new_active_customer_only_round_defaults_to_ai_before_any_reply(self):
        result = project(event(1))
        round_row = result.rounds[0]
        self.assertEqual(round_row["reception_type"], "ai_only")
        self.assertEqual(round_row["reply_composition"], "none")
        self.assertEqual(round_row["status"], "active")
        self.assertFalse(round_row["data"]["classification_finalized"])
        self.assertFalse(round_row["data"]["has_agent_reply"])
        self.assertNotIn("handoff_result", round_row["data"])

    def test_later_explicit_request_changes_same_active_round_without_ai_reply(self):
        result = project(event(1), event(2, 1, content="我要转人工"))
        self.assertEqual(len(result.rounds), 1)
        round_row = result.rounds[0]
        self.assertEqual(round_row["reception_type"], "human")
        self.assertEqual(round_row["reply_composition"], "none")
        self.assertEqual(round_row["data"]["handoff_result"], "requested")
        self.assertEqual(round_row["data"]["handoff_requested_at"], (START + timedelta(minutes=1)).isoformat())
        self.assertFalse(round_row["data"]["classification_finalized"])
        self.assertFalse(round_row["data"]["has_agent_reply"])
        self.assertNotIn("handoff_completed_at", round_row["data"])

    def test_handoff_request_attributes_classify_human_and_ai_does_not_revert(self):
        for key in ("handoff_requested", "handoff_required"):
            for initial_message in (True, False):
                with self.subTest(key=key, initial_message=initial_message):
                    incoming = event(1)
                    events = [incoming]
                    if initial_message:
                        incoming["payload"]["conversation"]["custom_attributes"] = {key: True}
                    else:
                        events.append(event(2, 1, event_type="conversation_updated", custom_attributes={key: True}))
                    events.append(event(3, 2, sender="agent_bot", direction="outgoing"))
                    result = project(*events)
                    round_row = result.rounds[0]
                    self.assertEqual(round_row["reception_type"], "human")
                    self.assertEqual(round_row["reply_composition"], "ai_only")
                    self.assertEqual(round_row["data"]["handoff_result"], "requested")
                    self.assertFalse(round_row["data"]["has_agent_reply"])
                    self.assertNotIn("handoff_completed_at", round_row["data"])

    def test_requested_handoff_becomes_completed_only_on_actual_takeover(self):
        for evidence in (event(2, 1, event_type="conversation_updated", custom_attributes={"handoff_accepted": True}),
                         event(2, 1, sender="user", direction="outgoing")):
            with self.subTest(event_type=evidence["event_type"]):
                result = project(event(1, content="我要转人工"), evidence,
                    event(3, 2, sender="agent_bot", direction="outgoing"),
                    event(4, 3, event_type="conversation_updated", custom_attributes={"handoff_requested": True}))
                round_row = result.rounds[0]
                self.assertEqual(round_row["reception_type"], "human")
                self.assertEqual(round_row["data"]["handoff_result"], "completed")
                self.assertEqual(round_row["data"]["handoff_completed_at"], (START + timedelta(minutes=1)).isoformat())
                self.assertEqual(round_row["data"]["has_agent_reply"], evidence["event_type"] == "message_created")

    def test_assignment_and_private_note_without_request_keep_ai_classification(self):
        result = project(event(1),
            event(2, 1, event_type="conversation_updated", assignee={"id": 8}),
            event(3, 2, sender="user", direction="outgoing", private=True))
        round_row = result.rounds[0]
        self.assertEqual(round_row["reception_type"], "ai_only")
        self.assertEqual(round_row["reply_composition"], "none")
        self.assertFalse(round_row["data"]["has_agent_reply"])
        self.assertNotIn("handoff_result", round_row["data"])

    def test_request_alone_does_not_inherit_actual_takeover_at_idle_boundary(self):
        result = project(event(1, content="我要转人工"), event(2, 360))
        self.assertEqual([row["reception_type"] for row in result.rounds], ["human", "ai_only"])
        self.assertEqual([row["status"] for row in result.rounds], ["idle_boundary", "active"])
        self.assertTrue(result.rounds[0]["data"]["classification_finalized"])
        self.assertFalse(result.rounds[1]["data"]["classification_finalized"])
        self.assertFalse(result.rounds[1]["data"]["takeover_inherited"])
        self.assertNotIn("handoff_result", result.rounds[1]["data"])

    def test_resolution_after_request_resets_next_round_to_ai_before_reply(self):
        result = project(event(1, content="我要转人工"),
            event(2, 1, event_type="conversation_status_changed", status="resolved"), event(3, 2))
        self.assertEqual([row["reception_type"] for row in result.rounds], ["human", "ai_only"])
        self.assertEqual([row["status"] for row in result.rounds], ["resolved", "active"])
        self.assertEqual(result.rounds[0]["data"]["handoff_result"], "requested")
        self.assertFalse(result.rounds[1]["data"]["takeover_inherited"])
        self.assertNotIn("handoff_result", result.rounds[1]["data"])

    def test_bot_assignee_is_not_a_human_seat_or_current_human_assignment(self):
        for kind in ("agent_bot", "AgentBot", "bot"):
            with self.subTest(kind=kind):
                assigned = event(2, 1, event_type="conversation_updated", assignee_id=6,
                    meta={"sender": {"id": 42, "type": "contact"},
                          "assignee": {"id": 6, "name": "Synthetic bot", "type": kind}})
                result = project(event(1), assigned, event(3, 2, sender="agent_bot", direction="outgoing"))
                self.assertEqual(result.agents, [])
                self.assertIsNone(result.rounds[0]["data"]["current_assignee_id"])
                self.assertIsNone(result.conversation["current_assignee_id"])
                self.assertEqual(result.rounds[0]["reception_type"], "ai_only")
                self.assertFalse(result.rounds[0]["data"]["has_agent_reply"])

    def test_bot_reassignment_clears_current_human_but_preserves_history_and_takeover(self):
        result = project(event(1),
            event(2, 1, event_type="conversation_updated", assignee={"id": 8, "name": "Assigned human"}),
            event(3, 2, sender="user", direction="outgoing"),
            event(4, 3, event_type="conversation_updated", assignee={"id": 6, "type": "agent_bot"}),
            event(5, 4))
        self.assertEqual(len(result.rounds), 1)
        round_row = result.rounds[0]
        self.assertIsNone(round_row["data"]["current_assignee_id"])
        self.assertIsNone(result.conversation["current_assignee_id"])
        self.assertEqual(round_row["reception_type"], "human")
        self.assertTrue(round_row["data"]["has_agent_reply"])
        self.assertEqual(round_row["data"]["handoff_result"], "completed")
        agents = {row["agent_id"]: row for row in result.agents}
        self.assertEqual(set(agents), {"7", "8"})
        self.assertTrue(agents["8"]["was_assigned"])
        self.assertTrue(agents["7"]["replied"])
        self.assertEqual(agents["7"]["reply_count"], 1)

    def test_bot_reassignment_preserves_confirmed_takeover_without_public_reply(self):
        result = project(event(1),
            event(2, 1, event_type="conversation_updated", assignee={"id": 8}, custom_attributes={"handoff_accepted": True}),
            event(3, 2, event_type="conversation_updated", assignee={"id": 6, "type": "bot"}))
        self.assertIsNone(result.rounds[0]["data"]["current_assignee_id"])
        self.assertEqual(result.rounds[0]["reception_type"], "human")
        self.assertEqual(result.rounds[0]["data"]["handoff_result"], "completed")
        self.assertFalse(result.rounds[0]["data"]["has_agent_reply"])
        self.assertEqual([row["agent_id"] for row in result.agents], ["8"])

    def test_human_assignment_after_bot_retains_untyped_id_compatibility(self):
        result = project(event(1),
            event(2, 1, event_type="conversation_updated", assignee={"id": 6, "type": "agent_bot"}),
            event(3, 2, event_type="conversation_updated", assignee={"id": 8}))
        self.assertEqual(result.rounds[0]["data"]["current_assignee_id"], "8")
        self.assertEqual(result.conversation["current_assignee_id"], "8")
        self.assertEqual([row["agent_id"] for row in result.agents], ["8"])
        self.assertTrue(result.agents[0]["was_assigned"])
        self.assertNotEqual(result.rounds[0]["reception_type"], "human")

    def test_confirmed_takeover_without_reply_is_human(self):
        result = project(event(1), event(2, 1, event_type="conversation_updated", custom_attributes={"handoff_accepted": True}))
        self.assertEqual(result.rounds[0]["reception_type"], "human")
        self.assertFalse(result.rounds[0]["data"]["has_agent_reply"])
        self.assertEqual(result.rounds[0]["reply_composition"], "none")

    def test_idle_boundary_uses_last_customer_and_inherits_takeover(self):
        result = project(event(1), event(2, 1, sender="user", direction="outgoing"),
                         event(3, 359, sender="user", direction="outgoing"), event(4, 360))
        self.assertEqual(len(result.rounds), 2)
        self.assertEqual(result.rounds[0]["status"], "idle_boundary")
        self.assertEqual(result.rounds[1]["reception_type"], "human")
        self.assertTrue(result.rounds[1]["data"]["takeover_inherited"])

    def test_customer_activity_extends_idle_and_configurable_timeout_is_used(self):
        result = project(event(1), event(2, 5), event(3, 14), idle_timeout_seconds=600)
        self.assertEqual(len(result.rounds), 1)
        result = project(event(1), event(2, 10), idle_timeout_seconds=600)
        self.assertEqual(len(result.rounds), 2)

    def test_explicit_resolution_starts_new_round_and_resets_takeover(self):
        result = project(event(1), event(2, 1, sender="user", direction="outgoing"),
            event(3, 2, event_type="conversation_status_changed", status="resolved"), event(4, 3),
            event(5, 4, sender="agent_bot", direction="outgoing"))
        self.assertEqual([row["reception_type"] for row in result.rounds], ["human", "ai_only"])
        self.assertEqual([row["status"] for row in result.rounds], ["resolved", "active"])
        self.assertFalse(result.rounds[1]["data"]["takeover_inherited"])

    def test_embedded_message_status_and_unrelated_conversation_update_do_not_close(self):
        initial = event(1)
        initial["payload"]["conversation"]["status"] = "resolved"
        result = project(initial, event(2, 1, event_type="conversation_updated", status="resolved",
                                      changed_attributes=[{"last_activity_at": {"current_value": 1}}]), event(3, 2))
        self.assertEqual(len(result.rounds), 1)
        self.assertEqual(result.rounds[0]["status"], "active")

    def test_changed_attribute_status_is_a_real_resolution(self):
        result = project(event(1), event(2, 1, event_type="conversation_updated", status="resolved",
                         changed_attributes=[{"status": {"current_value": "resolved"}}]), event(3, 2))
        self.assertEqual(len(result.rounds), 2)

    def test_duplicate_created_events_and_edits_do_not_inflate_counts(self):
        result = project(event(1, message_id=100, content="original"), event(2, message_id=100, content="original"),
            event(3, 1, event_type="message_updated", message_id=100, content="edited", updated_at=(START+timedelta(minutes=1)).isoformat()),
            event(4, 2, event_type="message_updated", message_id=100, content=None,
                  content_attributes={"deleted": True}, updated_at=(START+timedelta(minutes=2)).isoformat()))
        self.assertEqual(len(result.messages), 1)
        self.assertEqual(result.rounds[0]["data"]["counts"]["contact"], 1)
        self.assertEqual(len(result.snapshots), 4)
        self.assertEqual(result.snapshots[0]["content"], "original")
        self.assertTrue(result.messages[0]["is_deleted"])
        self.assertEqual(result.messages[0]["content"], "edited")
        self.assertIsNone(result.snapshots[-1]["content"])
        self.assertEqual(result.messages[0]["original_event_id"], 1)

    def test_out_of_order_old_edit_does_not_replace_newer_content(self):
        original = event(1, message_id=100)
        recent = event(2, 10, event_type="message_updated", message_id=100, content="new", updated_at=(START+timedelta(minutes=10)).isoformat())
        old = event(3, 2, event_type="message_updated", message_id=100, content="old", updated_at=(START+timedelta(minutes=2)).isoformat())
        old["received_at"] = START+timedelta(days=1)
        result = project(recent, old, original)
        self.assertEqual(result.messages[0]["content"], "new")
        self.assertEqual(result.messages[0]["latest_event_id"], 2)
        self.assertEqual(result.rounds[0]["last_message_at"], START)

    def test_partial_history_and_outgoing_messages_never_start_a_round(self):
        result = project(event(1, event_type="message_updated"), event(2, 1, sender="user", direction="outgoing"))
        self.assertEqual(result.rounds, [])
        self.assertFalse(result.messages[0]["data"]["has_creation_event"])
        self.assertIsNone(result.messages[0]["round_id"])

    def test_activity_private_and_csat_are_excluded_from_service_counts(self):
        result = project(event(1), event(2, 1, sender="user", direction="activity"),
            event(3, 2, sender="user", direction="outgoing", private=True),
            event(4, 3, sender="agent_bot", direction="outgoing", content_type="input_csat"))
        self.assertEqual(result.rounds[0]["data"]["counts"], {"contact": 1, "bot": 0, "agent": 0, "private": 1, "attachments": 0})
        self.assertEqual(result.rounds[0]["last_message_at"], START)

    def test_late_rating_links_closed_round_and_never_latest_round(self):
        closed = START + timedelta(minutes=2)
        result = project(event(1), event(2, 2, event_type="conversation_status_changed", status="resolved"),
            event(3, 3), event(4, 4, message_id=500, sender="agent_bot", direction="outgoing", content_type="input_csat",
                  content_attributes={"ics_round_id": "external-random-id", "ics_closed_at": closed.isoformat()}),
            event(5, 5, event_type="message_updated", message_id=500, content_type="input_csat",
                  csat_survey_response={"message_id": 500, "rating": 4, "feedback_message": "很好"}))
        self.assertEqual([row["rating_status"] for row in result.rounds], ["rated", "not_invited"])
        self.assertEqual(result.rounds[0]["rating"], 4)
        self.assertEqual(result.ratings[0]["round_id"], result.rounds[0]["round_id"])
        self.assertEqual(len(result.rating_snapshots), 2)
        self.assertEqual(next(m for m in result.messages if m["message_id"] == 500)["round_id"], result.rounds[0]["round_id"])

    def test_real_nested_csat_response_and_microsecond_close_difference_are_projected(self):
        closed = START + timedelta(minutes=2, microseconds=9632)
        external = "chatwoot:1:12:round:36cb5011-59ec-450a-9da4-e901824abf29"
        attributes = {"ics_round_id": external,
            "ics_closed_at": (closed + timedelta(microseconds=1)).astimezone(timezone(timedelta(hours=8))).isoformat()}
        events = [event(1), event(2, 2, event_type="conversation_status_changed", status="resolved", updated_at=closed.isoformat()),
            event(3, 3, message_id=500, sender="agent_bot", direction="outgoing", content_type="input_csat", content_attributes=attributes),
            event(4, 4, event_type="message_updated", message_id=500, sender="agent_bot", direction="outgoing",
                created_at=(START + timedelta(minutes=3)).isoformat(), content_type="input_csat",
                content_attributes={**attributes, "submitted_values": {"csat_survey_response": {"rating": 5, "feedback_message": "ok"}}})]
        original = deepcopy(events)
        result = project(*events)
        round_row = result.rounds[0]
        self.assertNotEqual(external, round_row["round_id"])
        self.assertEqual(round_row["rating_status"], "rated")
        self.assertEqual(round_row["rating"], 5)
        self.assertEqual(round_row["data"]["rating_feedback"], "ok")
        self.assertIsNone(round_row["data"]["rating_at"])
        self.assertEqual(result.ratings[0]["feedback"], "ok")
        self.assertEqual(result.rating_snapshots[-1]["rating"], 5)
        self.assertEqual(result.snapshots[-1]["content_attributes"], events[-1]["payload"]["content_attributes"])
        self.assertEqual(round_row["data"]["closed_at"], closed.isoformat())
        self.assertEqual(round_row["data"]["counts"], {"contact": 1, "bot": 0, "agent": 0, "private": 0, "attachments": 0})
        self.assertEqual(round_row["last_message_at"], START)
        self.assertEqual(events, original)
        self.assertEqual(result, project(*reversed(events)))

    def test_rating_close_time_tolerance_is_limited_to_one_millisecond(self):
        closed = START + timedelta(minutes=2)
        for microseconds in (-1001, -1000, -1, 0, 1, 1000, 1001):
            with self.subTest(microseconds=microseconds):
                result = project(event(1), event(2, 2, event_type="conversation_status_changed", status="resolved"),
                    event(3, 3, message_id=500, sender="agent_bot", direction="outgoing", content_type="input_csat",
                        content_attributes={"ics_round_id": "business-round", "ics_closed_at": (closed + timedelta(microseconds=microseconds)).isoformat(),
                            "submitted_values": {"csat_survey_response": {"rating": 5}}}))
                linked = abs(microseconds) <= 1000
                self.assertEqual(result.ratings[0]["status"], "rated" if linked else "unlinked")
                self.assertEqual(result.rounds[0]["rating"], 5 if linked else None)

    def test_rating_with_two_resolved_rounds_in_tolerance_window_stays_unlinked(self):
        closed = START + timedelta(minutes=2)
        result = project(event(1), event(2, 2, event_type="conversation_status_changed", status="resolved"),
            event(3, 2, created_at=(closed + timedelta(microseconds=200)).isoformat()),
            event(4, 2, event_type="conversation_status_changed", status="resolved", updated_at=(closed + timedelta(microseconds=500)).isoformat()),
            event(5, 3, message_id=500, content_type="input_csat", sender="agent_bot", direction="outgoing",
                content_attributes={"ics_round_id": "ambiguous-business-round", "ics_closed_at": (closed + timedelta(microseconds=250)).isoformat(),
                    "submitted_values": {"csat_survey_response": {"rating": 4}}}))
        self.assertEqual(len(result.rounds), 2)
        self.assertEqual(result.ratings[0]["status"], "unlinked")
        self.assertIsNone(result.ratings[0]["round_id"])
        self.assertEqual([row["rating_status"] for row in result.rounds], ["not_invited", "not_invited"])

    def test_nested_late_old_card_rating_uses_invitation_identity_after_new_round_starts(self):
        closed = START + timedelta(minutes=2)
        result = project(event(1), event(2, 2, event_type="conversation_status_changed", status="resolved"),
            event(3, 3, message_id=500, sender="agent_bot", direction="outgoing", content_type="input_csat",
                content_attributes={"ics_round_id": "business-old-round", "ics_closed_at": (closed + timedelta(microseconds=-1)).isoformat()}),
            event(4, 4),
            event(5, 5, event_type="message_updated", message_id=500, sender="agent_bot", direction="outgoing",
                content_type="input_csat", updated_at=(START + timedelta(minutes=5)).isoformat(),
                content_attributes={"submitted_values": {"csat_survey_response": {"rating": 2, "feedback_message": "旧轮评价"}}}))
        self.assertEqual([row["rating_status"] for row in result.rounds], ["rated", "not_invited"])
        self.assertEqual(result.rounds[0]["data"]["rating_feedback"], "旧轮评价")
        self.assertEqual(result.rounds[0]["data"]["rating_at"], (START + timedelta(minutes=5)).isoformat())
        self.assertEqual(result.ratings[0]["round_id"], result.rounds[0]["round_id"])
        self.assertEqual(next(m for m in result.messages if m["message_id"] == 500)["round_id"], result.rounds[0]["round_id"])
        self.assertEqual([row["data"]["counts"]["contact"] for row in result.rounds], [1, 1])

    def test_different_cards_keep_different_round_scores_when_old_card_is_rated_last(self):
        first_closed, second_closed = START + timedelta(minutes=1), START + timedelta(minutes=4)
        result = project(event(1), event(2, 1, event_type="conversation_status_changed", status="resolved"),
            event(3, 2, message_id=500, sender="agent_bot", direction="outgoing", content_type="input_csat",
                content_attributes={"ics_round_id": "business-first", "ics_closed_at": (first_closed + timedelta(microseconds=1)).isoformat()}),
            event(4, 3), event(5, 4, event_type="conversation_status_changed", status="resolved"),
            event(6, 5, message_id=600, sender="agent_bot", direction="outgoing", content_type="input_csat",
                content_attributes={"ics_round_id": "business-second", "ics_closed_at": (second_closed + timedelta(microseconds=-1)).isoformat()}),
            event(7, 6, event_type="message_updated", message_id=600, content_type="input_csat", updated_at=(START + timedelta(minutes=6)).isoformat(),
                content_attributes={"ics_round_id": "business-second", "submitted_values": {"csat_survey_response": {"rating": 5, "feedback_message": "第二轮"}}}),
            event(8, 7, event_type="message_updated", message_id=500, content_type="input_csat", updated_at=(START + timedelta(minutes=7)).isoformat(),
                content_attributes={"ics_round_id": "business-first", "submitted_values": {"csat_survey_response": {"rating": 1, "feedback_message": "第一轮"}}}))
        self.assertEqual([row["rating"] for row in result.rounds], [1, 5])
        self.assertEqual([row["data"]["rating_feedback"] for row in result.rounds], ["第一轮", "第二轮"])
        self.assertEqual([row["data"]["invitation_message_id"] for row in result.rounds], [500, 600])
        self.assertEqual(len(result.ratings), 2)

    def test_csat_rating_and_feedback_edits_win_but_lifecycle_update_does_not_change_rating_time(self):
        closed = START + timedelta(minutes=1)
        invitation = event(3, 2, message_id=500, sender="agent_bot", direction="outgoing", content_type="input_csat",
            content_attributes={"ics_round_id": "business-round", "ics_closed_at": closed.isoformat()})
        early = event(4, 3, event_type="message_updated", message_id=500, content_type="input_csat", updated_at=(START + timedelta(minutes=3)).isoformat(),
            content_attributes={"submitted_values": {"csat_survey_response": {"rating": 2, "feedback_message": "最初反馈"}}})
        later = event(5, 4, event_type="message_updated", message_id=500, content_type="input_csat", updated_at=(START + timedelta(minutes=4)).isoformat(),
            content_attributes={"submitted_values": {"csat_survey_response": {"rating": 5, "feedback_message": "修改反馈"}}})
        lifecycle = event(6, 5, event_type="message_updated", message_id=500, updated_at=(START + timedelta(minutes=5)).isoformat(), content="updated card")
        events = [event(1), event(2, 1, event_type="conversation_status_changed", status="resolved"), invitation, later, early, lifecycle]
        result = project(*events)
        self.assertEqual(result.rounds[0]["rating"], 5)
        self.assertEqual(result.rounds[0]["data"]["rating_feedback"], "修改反馈")
        self.assertEqual(result.rounds[0]["data"]["rating_at"], (START + timedelta(minutes=4)).isoformat())
        self.assertEqual(result.ratings[0]["feedback"], "修改反馈")
        self.assertEqual([row["rating"] for row in result.rating_snapshots], [None, 2, 5, None])
        self.assertEqual(result.rounds[0]["data"]["counts"], {"contact": 1, "bot": 0, "agent": 0, "private": 0, "attachments": 0})
        self.assertEqual(result.rounds[0]["last_message_at"], START)
        self.assertEqual(result, project(*reversed(events)))

    def test_latest_explicit_feedback_clear_does_not_restore_older_feedback(self):
        for cleared in ("", None):
            with self.subTest(feedback=cleared):
                result = project(event(1), event(2, 1, event_type="conversation_status_changed", status="resolved"),
                    event(3, 2, message_id=500, content_type="input_csat", content_attributes={"ics_round_id": "business-round", "ics_closed_at": (START + timedelta(minutes=1)).isoformat(),
                        "submitted_values": {"csat_survey_response": {"rating": 3, "feedback_message": "旧反馈"}}}),
                    event(4, 3, event_type="message_updated", message_id=500, content_type="input_csat", updated_at=(START + timedelta(minutes=3)).isoformat(),
                        content_attributes={"submitted_values": {"csat_survey_response": {"rating": 4, "feedback_message": cleared}}}))
                self.assertEqual(result.rounds[0]["rating"], 4)
                self.assertEqual(result.rounds[0]["data"]["rating_feedback"], cleared)
                self.assertEqual(result.ratings[0]["feedback"], cleared)

    def test_proven_business_round_alias_links_response_without_invitation_id(self):
        result = project(event(1), event(2, 1, event_type="conversation_status_changed", status="resolved"),
            event(3, 2, message_id=500, content_type="input_csat", content_attributes={"ics_round_id": "business-old", "ics_closed_at": (START + timedelta(minutes=1)).isoformat()}),
            event(4, 3),
            event(5, 4, event_type="csat_survey_response_updated", ics_round_id="business-old", csat_survey_response={"rating": 4, "feedback_message": "别名确认"}))
        self.assertEqual([row["rating_status"] for row in result.rounds], ["rated", "not_invited"])
        self.assertEqual(result.rounds[0]["data"]["rating_feedback"], "别名确认")

    def test_conflicting_invitation_or_business_alias_evidence_stays_unlinked(self):
        for conflict in ("invitation", "alias"):
            with self.subTest(conflict=conflict):
                result = project(event(1), event(2, 1, event_type="conversation_status_changed", status="resolved"),
                    event(3, 2, message_id=500, content_type="input_csat", content_attributes={"ics_round_id": "business-first", "ics_closed_at": (START + timedelta(minutes=1)).isoformat()}),
                    event(4, 3), event(5, 4, event_type="conversation_status_changed", status="resolved"),
                    event(6, 5, event_type="message_updated" if conflict == "invitation" else "message_created", message_id=500 if conflict == "invitation" else 600,
                        content_type="input_csat", updated_at=(START + timedelta(minutes=5)).isoformat(),
                        content_attributes={"ics_round_id": "business-second" if conflict == "invitation" else "business-first", "ics_closed_at": (START + timedelta(minutes=4)).isoformat(),
                            "submitted_values": {"csat_survey_response": {"rating": 5}}}))
                self.assertTrue(all(row["status"] == "unlinked" for row in result.ratings))
                self.assertEqual([row["rating_status"] for row in result.rounds], ["not_invited", "not_invited"])

    def test_unprovable_rating_is_explicitly_unlinked(self):
        result = project(event(1), event(2, 1, sender="agent_bot", direction="outgoing", content_type="input_csat",
                         content_attributes={"ics_round_id": "unknown", "rating": 5}))
        self.assertEqual(result.ratings[0]["status"], "unlinked")
        self.assertIsNone(result.ratings[0]["round_id"])
        self.assertEqual(result.rounds[0]["rating_status"], "not_invited")

    def test_same_contact_merges_history_but_name_phone_and_unverified_identifier_do_not(self):
        first = project(event(1))
        second = project(event(2, conversation_id=22))
        self.assertEqual(first.customer["customer_id"], second.customer["customer_id"])
        different = event(3, conversation_id=33)
        different["payload"]["conversation"]["meta"]["sender"].update(id=43, phone_number="123", identifier="same-user")
        third = project(different)
        self.assertNotEqual(first.customer["customer_id"], third.customer["customer_id"])
        self.assertIsNone(third.customer["business_user_id"])

    def test_source_scope_prevents_customer_and_round_collision(self):
        one = project(event(1, source="one"))
        two = project(event(1, source="two"))
        self.assertNotEqual(one.customer["customer_id"], two.customer["customer_id"])
        self.assertNotEqual(one.rounds[0]["round_id"], two.rounds[0]["round_id"])

    def test_unknown_channel_never_falls_back_to_default(self):
        result = project_conversation([event(1)])
        self.assertIsNone(result.rounds[0]["channel_key"])
        self.assertIsNone(result.messages[0]["data"]["channel_key"])

    def test_attachment_source_metadata_is_preserved(self):
        result = project(event(1, attachments=[{"id": 91, "file_name": "图片.png", "file_type": "image",
                                               "data_url": "https://example.invalid/attachment", "file_size": 10}]))
        self.assertEqual(result.rounds[0]["data"]["counts"]["attachments"], 1)
        self.assertEqual(result.messages[0]["data"]["attachments"][0]["source_attachment_id"], "91")

    def test_unknown_reply_retains_unknown_sender_and_default_ai_reception(self):
        result = project(event(1), event(2, 1, sender="mystery", direction="outgoing"))
        self.assertEqual(result.rounds[0]["reception_type"], "ai_only")
        self.assertEqual(result.messages[1]["sender_role"], "unknown")
        self.assertEqual(result.rounds[0]["reply_composition"], "none")

    def test_assignment_release_clears_current_but_preserves_agent_history(self):
        result = project(event(1), event(2, 1, event_type="conversation_updated", assignee={"id": 8}),
                         event(3, 2, event_type="conversation_updated", assignee=None))
        self.assertIsNone(result.rounds[0]["data"]["current_assignee_id"])
        self.assertTrue(result.agents[0]["was_assigned"])

    def test_missing_creation_time_preserves_evidence_without_inventing_round(self):
        missing = event(1)
        missing["payload"].pop("created_at")
        result = project(missing)
        self.assertEqual(len(result.snapshots), 1)
        self.assertEqual(result.rounds, [])
        self.assertIsNone(result.messages[0]["sent_at"])
        self.assertEqual(result.messages[0]["data"]["timestamp_status"], "unknown")

    def test_idle_end_has_boundary_time_without_fake_resolved_time(self):
        result = project(event(1), event(2, 360))
        first, second = result.rounds
        self.assertNotIn("closed_at", first["data"])
        self.assertIn("boundary_at", first["data"])
        self.assertTrue(first["data"]["classification_finalized"])
        self.assertFalse(second["data"]["classification_finalized"])
        self.assertEqual(first["data"]["end_reason"], "idle_timeout")

    def test_assignment_in_old_message_snapshot_uses_conversation_update_time(self):
        first = event(1)
        later_snapshot = event(2, 0, message_id=1)
        later_snapshot["payload"]["conversation"].update(updated_at=(START+timedelta(minutes=400)).isoformat(),
            meta={"sender": {"id": 42, "type": "contact"}, "assignee": {"id": 99}})
        result = project(first, event(3, 360), later_snapshot)
        self.assertIsNone(result.rounds[0]["data"]["current_assignee_id"])
        self.assertEqual(result.rounds[1]["data"]["current_assignee_id"], "99")

    def test_worker_configuration_does_not_require_receiver_secret_or_bot_settings(self):
        settings = ProcessingSettings(_env_file=None, ingest_password="test-credential", idle_timeout_seconds=99)
        self.assertEqual(settings.idle_timeout_seconds, 99)
        self.assertEqual(settings.processor_pool_size, 2)
        self.assertEqual(settings.connection_kwargs["application_name"], "ics-quality-processor")

    def test_malformed_optional_message_fields_stay_unknown_without_losing_body(self):
        result = project(event(1, message_type={"unexpected": "value"}, attachments="invalid", content_attributes=[]))
        self.assertEqual(len(result.snapshots), 1)
        self.assertEqual(result.messages[0]["direction"], "unknown")
        self.assertEqual(result.rounds, [])

    def test_status_without_source_time_preserves_evidence_without_closing_latest_round(self):
        unknown = event(2, 1, event_type="conversation_status_changed", status="resolved")
        unknown["payload"].pop("updated_at")
        result = project(event(1), unknown, event(3, 2))
        self.assertEqual(len(result.rounds), 1)
        self.assertEqual(result.rounds[0]["status"], "active")
        snapshot = next(row for row in result.conversation_snapshots if row["event_id"] == 2)
        self.assertIsNone(snapshot["source_time"])

    def test_update_only_public_agent_message_in_known_round_proves_reply_once(self):
        result = project(event(1),
            event(2, 1, event_type="message_updated", message_id=100, sender="user", direction="outgoing", content="human reply",
                  updated_at=(START+timedelta(minutes=2)).isoformat()),
            event(3, 1, event_type="message_updated", message_id=100, sender="user", direction="outgoing", content="edited reply",
                  updated_at=(START+timedelta(minutes=3)).isoformat()))
        self.assertEqual(len(result.rounds), 1)
        self.assertEqual(result.rounds[0]["reception_type"], "human")
        self.assertEqual(result.rounds[0]["data"]["counts"]["agent"], 1)
        self.assertTrue(result.rounds[0]["data"]["has_agent_reply"])
        self.assertEqual(result.agents[0]["reply_count"], 1)
        message = next(item for item in result.messages if item["message_id"] == 100)
        self.assertFalse(message["data"]["has_creation_event"])
        self.assertEqual(message["round_id"], result.rounds[0]["round_id"])

    def test_late_creation_evidence_does_not_double_count_update_only_reply(self):
        result = project(event(1),
            event(2, 1, event_type="message_updated", message_id=100, sender="user", direction="outgoing",
                  updated_at=(START+timedelta(minutes=2)).isoformat()),
            event(3, 1, message_id=100, sender="user", direction="outgoing"))
        self.assertEqual(result.rounds[0]["data"]["counts"]["agent"], 1)
        self.assertEqual(result.agents[0]["reply_count"], 1)
        self.assertTrue(next(item for item in result.messages if item["message_id"] == 100)["data"]["has_creation_event"])

    def test_update_only_customer_observation_cannot_open_or_split_a_round(self):
        result = project(event(1), event(2, 400, event_type="message_updated", message_id=100,
                         updated_at=(START+timedelta(minutes=401)).isoformat()))
        self.assertEqual(len(result.rounds), 1)
        self.assertEqual(result.rounds[0]["data"]["counts"]["contact"], 2)
        self.assertEqual(result.rounds[0]["data"]["last_customer_at"], START.isoformat())
        self.assertEqual(result.rounds[0]["data"]["observed_only_message_count"], 1)

    def test_same_second_source_message_order_survives_reversed_webhook_arrival(self):
        result = project(event(2, message_id=1), event(1, event_type="message_updated", message_id=100,
                         sender="user", direction="outgoing"))
        self.assertEqual(result.rounds[0]["reception_type"], "human")
        self.assertEqual(result.rounds[0]["data"]["counts"]["agent"], 1)

    def test_first_customer_and_same_second_resolution_preserve_closed_status(self):
        result = project(event(2, message_id=1),
                         event(1, event_type="conversation_status_changed", status="resolved"))
        self.assertEqual(len(result.rounds), 1)
        self.assertEqual(result.rounds[0]["status"], "resolved")
        self.assertEqual(result.rounds[0]["data"]["closed_at"], START.isoformat())
        self.assertTrue(result.rounds[0]["data"]["classification_finalized"])

    def test_first_customer_and_same_second_confirmation_preserve_human_takeover(self):
        result = project(event(2, message_id=1),
                         event(1, event_type="conversation_updated", custom_attributes={"handoff_accepted": True}))
        self.assertEqual(len(result.rounds), 1)
        self.assertEqual(result.rounds[0]["reception_type"], "human")
        self.assertEqual(result.rounds[0]["data"]["handoff_result"], "completed")
        self.assertFalse(result.rounds[0]["data"]["has_agent_reply"])

    def test_later_creation_time_recovers_round_using_original_body_and_sender(self):
        original = event(1, message_id=100, content="原始客户问题")
        original["payload"].pop("created_at")
        update = event(2, message_id=100, event_type="message_updated", sender="user", direction="outgoing",
                       content="我要转人工", updated_at=(START + timedelta(minutes=1)).isoformat())
        result = project(original, update)
        self.assertEqual(len(result.rounds), 1)
        self.assertEqual(result.rounds[0]["started_at"], START)
        self.assertEqual(result.rounds[0]["data"]["counts"]["contact"], 1)
        self.assertEqual(result.rounds[0]["data"]["counts"]["agent"], 0)
        self.assertFalse(result.rounds[0]["data"]["has_agent_reply"])
        self.assertEqual(result.rounds[0]["data"]["responsibility_basis"], "assignment_unobserved")
        self.assertIsNone(result.rounds[0]["data"]["responsible_agent_id"])
        self.assertNotIn("handoff_requested_at", result.rounds[0]["data"])
        self.assertEqual(result.snapshots[0]["content"], "原始客户问题")
        self.assertEqual(result.snapshots[0]["sender_role"], "contact")
        self.assertIsNone(result.snapshots[0]["sent_at"])
        self.assertTrue(result.messages[0]["data"]["has_creation_event"])
        self.assertEqual(result.messages[0]["sent_at"], START)


if __name__ == "__main__":
    unittest.main()
