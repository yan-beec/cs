import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from pydantic import ValidationError

from ics_agent.app.chatwoot.payload import (
    parse_event,
    webhook_message_time,
    webhook_status_time,
)
from ics_agent.app.core.config import AppSettings
from ics_agent.app.core.models import ChatwootWebhookEvent


class SessionEventTimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.expected = datetime(2026, 10, 8, 1, 2, 3, 125000, timezone.utc)

    def message(self, created_at: object, **overrides: object) -> ChatwootWebhookEvent:
        payload = {
            "event": "message_created",
            "message_type": "incoming",
            "sender": {"type": "contact", "id": 42},
            "created_at": created_at,
            **overrides,
        }
        return parse_event(json.dumps(payload).encode())

    def test_message_accepts_chatwoot_iso_and_normalizes_offset(self) -> None:
        for value in (
            "2026-10-08T01:02:03.125Z",
            "2026-10-08T09:02:03.125+08:00",
            "2026-10-08T01:02:03.125+00:00",
            "2026-10-08T01:02:03.125",
        ):
            with self.subTest(value=value):
                actual = webhook_message_time(self.message(value))
                self.assertEqual(actual, self.expected)
                self.assertIs(actual.tzinfo, timezone.utc)

    def test_message_accepts_epoch_seconds_and_milliseconds(self) -> None:
        seconds = self.expected.timestamp()
        for value in (seconds, str(seconds), seconds * 1000, str(seconds * 1000)):
            with self.subTest(value=value):
                self.assertEqual(webhook_message_time(self.message(value)), self.expected)

    def test_message_accepts_integer_seconds_and_milliseconds(self) -> None:
        expected = self.expected.replace(microsecond=0)
        for value in (int(expected.timestamp()), int(expected.timestamp() * 1000)):
            with self.subTest(value=value):
                self.assertEqual(webhook_message_time(self.message(value)), expected)

    def test_invalid_or_unrepresentable_time_returns_none(self) -> None:
        for value in (
            None,
            True,
            False,
            "",
            "not-a-time",
            "2026-02-30T01:02:03Z",
            "9999-12-31T23:59:59-12:00",
            "NaN",
            "Infinity",
            "-Infinity",
            float("nan"),
            float("inf"),
            float("-inf"),
            -1,
            10**1000,
            1e308,
            {},
            [],
        ):
            with self.subTest(value=str(value)[:40]):
                self.assertIsNone(webhook_message_time(self.message(value)))

    def test_user_message_never_falls_back_to_conversation_or_delivery_time(self) -> None:
        event = self.message(
            None,
            updated_at="2026-10-08T08:00:00Z",
            timestamp=1791446400,
            conversation={
                "created_at": "2026-10-08T00:00:00Z",
                "updated_at": "2026-10-08T08:00:00Z",
                "last_activity_at": 1791446400,
                "messages": [{"created_at": "2026-10-08T08:00:00Z"}],
            },
            message={"created_at": "2026-10-08T08:00:00Z"},
        )
        self.assertIsNone(webhook_message_time(event))

    def test_user_message_prefers_its_own_time_over_later_conversation_activity(self) -> None:
        event = self.message(
            self.expected.isoformat(),
            conversation={"updated_at": "2026-10-09T00:00:00Z"},
        )
        self.assertEqual(webhook_message_time(event), self.expected)

    def test_outgoing_activity_and_message_edits_do_not_count_as_user_messages(self) -> None:
        for overrides in (
            {"message_type": "outgoing"},
            {"message_type": "activity"},
            {"message_type": None},
            {"private": True},
            {"sender": {"type": "user"}},
            {"sender": {"type": "agent_bot"}},
            {"event": "message_updated"},
            {"event": "conversation_status_changed"},
            {"event": "contact_updated"},
        ):
            with self.subTest(overrides=overrides):
                self.assertIsNone(
                    webhook_message_time(self.message(self.expected.isoformat(), **overrides))
                )

    def test_incoming_message_without_sender_type_is_accepted(self) -> None:
        event = self.message(self.expected.isoformat(), sender={"id": 42})
        self.assertEqual(webhook_message_time(event), self.expected)

    def test_status_uses_actual_chatwoot_top_level_updated_at(self) -> None:
        event = parse_event(json.dumps({
            "event": "conversation_status_changed",
            "id": 56,
            "status": "resolved",
            "updated_at": self.expected.timestamp(),
            "timestamp": 1000,
            "created_at": 1000,
            "messages": [{"created_at": 1000}],
        }).encode())
        self.assertEqual(webhook_status_time(event), self.expected)
        self.assertIsNone(webhook_message_time(event))

    def test_status_supports_nested_conversation_iso_time(self) -> None:
        for event_name in ("conversation_updated", "conversation.status_changed"):
            with self.subTest(event_name=event_name):
                event = parse_event(json.dumps({
                    "event": event_name,
                    "conversation": {"updated_at": self.expected.isoformat()},
                }).encode())
                self.assertEqual(webhook_status_time(event), self.expected)

    def test_status_supports_directly_constructed_event(self) -> None:
        event = ChatwootWebhookEvent(
            event="conversation_resolved",
            conversation={"updated_at": self.expected.timestamp() * 1000},
        )
        self.assertEqual(webhook_status_time(event), self.expected)

    def test_status_without_updated_at_does_not_use_other_times(self) -> None:
        event = ChatwootWebhookEvent(
            event="conversation_status_changed",
            raw={"created_at": self.expected.isoformat(), "timestamp": 1791446400},
        )
        self.assertIsNone(webhook_status_time(event))

    def test_message_and_contact_updates_are_not_status_events(self) -> None:
        for event_name in ("message_created", "contact_updated"):
            with self.subTest(event_name=event_name):
                event = ChatwootWebhookEvent(
                    event=event_name,
                    raw={"updated_at": self.expected.isoformat()},
                )
                self.assertIsNone(webhook_status_time(event))


class SessionIdleTimeoutSettingsTest(unittest.TestCase):
    def test_default_is_six_hours(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(
                AppSettings(_env_file=None).chatwoot_session_idle_timeout_seconds,
                6 * 60 * 60,
            )

    def test_environment_can_override_timeout(self) -> None:
        with patch.dict("os.environ", {"CHATWOOT_SESSION_IDLE_TIMEOUT_SECONDS": "60"}):
            self.assertEqual(
                AppSettings(_env_file=None).chatwoot_session_idle_timeout_seconds,
                60,
            )

    def test_timeout_must_be_positive(self) -> None:
        for value in (0, -1):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                AppSettings(_env_file=None, chatwoot_session_idle_timeout_seconds=value)


if __name__ == "__main__":
    unittest.main()
