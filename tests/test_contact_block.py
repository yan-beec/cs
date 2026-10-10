import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, Mock, call, patch

from ics_agent.app.chatwoot import routes as main
from ics_agent.app.chatwoot.state import ContactBlockRepository
from ics_agent.app.core.models import ChatwootWebhookEvent
from ics_agent.app.chatwoot.payload import (
    contact_block_change_from_payload,
    contact_blocked_from_payload,
    parse_event,
)


class ContactBlockedPayloadTest(unittest.TestCase):
    def test_reads_blocked_from_conversation_sender(self) -> None:
        payload = {
            "conversation": {
                "meta": {
                    "sender": {
                        "id": 42,
                        "blocked": True,
                    }
                }
            }
        }

        self.assertTrue(contact_blocked_from_payload(payload))

    def test_reads_top_level_contact_update(self) -> None:
        self.assertTrue(
            contact_blocked_from_payload(
                {"event": "contact_updated", "blocked": True}
            )
        )

    def test_returns_false_for_explicitly_unblocked_contact(self) -> None:
        self.assertFalse(
            contact_blocked_from_payload(
                {"sender": {"blocked": False}}
            )
        )

    def test_returns_none_when_payload_has_no_blocked_state(self) -> None:
        self.assertIsNone(
            contact_blocked_from_payload(
                {"conversation": {"id": 10}}
            )
        )

    def test_reads_current_value_from_changed_attributes(self) -> None:
        self.assertTrue(
            contact_blocked_from_payload(
                {
                    "changed_attributes": [
                        {
                            "blocked": {
                                "previous_value": False,
                                "current_value": True,
                            }
                        }
                    ]
                }
            )
        )

    def test_ignores_snapshot_blocked_without_explicit_change(self) -> None:
        self.assertIsNone(
            contact_block_change_from_payload(
                {"contact": {"id": 42, "blocked": False}}
            )
        )

    def test_contact_update_is_recognized_as_block_event(self) -> None:
        event = ChatwootWebhookEvent(
            event="contact_updated",
            id=42,
            account={"id": 1},
            raw={
                "event": "contact_updated",
                "id": 42,
                "account": {"id": 1},
                "contact": {"id": 42, "blocked": True},
            },
        )

        self.assertTrue(main._is_contact_block_event(event))
        self.assertTrue(main._should_process_webhook_event(event))

    def test_conversation_update_with_blocked_sender_is_block_event(self) -> None:
        event = ChatwootWebhookEvent(
            event="conversation_updated",
            id=58,
            account={"id": 1},
            conversation={
                "id": 58,
                "account_id": 1,
                "meta": {
                    "sender": {
                        "id": 42,
                        "blocked": True,
                    }
                },
            },
            raw={
                "event": "conversation_updated",
                "id": 58,
                "account": {"id": 1},
                "conversation": {
                    "id": 58,
                    "account_id": 1,
                    "meta": {
                        "sender": {
                            "id": 42,
                            "blocked": True,
                        }
                    },
                },
            },
        )

        self.assertTrue(main._is_contact_block_event(event))
        self.assertTrue(main._should_process_webhook_event(event))

    def test_conversation_update_with_unblocked_sender_is_block_event(self) -> None:
        event = parse_event(json.dumps({
            "event": "conversation_updated",
            "id": 58,
            "account": {"id": 1},
            "conversation": {
                "id": 58,
                "account_id": 1,
                "meta": {"sender": {"id": 42, "blocked": False}},
            },
        }).encode())

        self.assertTrue(main._is_contact_block_event(event))
        self.assertTrue(main._should_process_webhook_event(event))

    def test_reads_contact_id_from_flat_conversation_event_sender(self) -> None:
        for event_name in ("conversation_updated", "conversation_status_changed"):
            with self.subTest(event=event_name):
                event = parse_event(json.dumps({
                    "event": event_name,
                    "id": 58,
                    "account": {"id": 1},
                    "status": "resolved",
                    "meta": {"sender": {"id": 42, "blocked": True}},
                }).encode())

                self.assertEqual(main._webhook_contact_id(event), "42")

    def test_does_not_use_conversation_id_as_missing_contact_id(self) -> None:
        event = parse_event(json.dumps({
            "event": "conversation_updated",
            "id": 58,
            "account": {"id": 1},
            "status": "resolved",
            "meta": {"sender": {"blocked": True}},
        }).encode())

        self.assertIsNone(main._webhook_contact_id(event))


class ContactBlockEventTest(unittest.IsolatedAsyncioTestCase):
    async def test_flat_conversation_update_arms_twenty_four_hour_contact_block(
        self,
    ) -> None:
        event = parse_event(json.dumps({
            "event": "conversation_updated",
            "id": 58,
            "account": {"id": 1},
            "inbox_id": 7,
            "status": "resolved",
            "meta": {
                "sender": {"id": 42, "blocked": True, "type": "contact"},
                "assignee": {"id": 9, "type": "user"},
                "assignee_type": "User",
            },
            "changed_attributes": [{
                "last_activity_at": {
                    "previous_value": "2026-10-09T06:44:00Z",
                    "current_value": "2026-10-09T06:44:39Z",
                },
            }],
        }).encode())
        now = datetime(2026, 10, 9, 6, 44, 39, tzinfo=timezone.utc)
        expected_expiry = now + timedelta(hours=24)
        repository = Mock()

        self.assertTrue(main._is_contact_block_event(event))
        self.assertTrue(main._should_process_webhook_event(event))
        with (
            patch.object(main, "contact_block_repository", repository),
            patch.object(main, "_resolve_blocked_contact_conversations", AsyncMock()),
            patch.object(
                main,
                "settings",
                main.settings.model_copy(
                    update={"chatwoot_block_duration_seconds": 86400}
                ),
            ),
            patch.object(main, "datetime") as datetime_mock,
        ):
            datetime_mock.now.return_value = now
            result = await main._process_contact_block_event(event)

        repository.arm.assert_called_once_with(
            account_id="1",
            contact_id="42",
            blocked_until=expected_expiry,
            refresh_existing=False,
        )
        repository.deactivate.assert_not_called()
        self.assertEqual(result["route"], "contact_block_armed")
        self.assertEqual(result["blocked_until"], expected_expiry.isoformat())

    async def test_contact_update_arms_seven_day_block(self) -> None:
        event = ChatwootWebhookEvent(
            event="contact_updated",
            id=42,
            account={"id": 1},
            raw={
                "event": "contact_updated",
                "id": 42,
                "account": {"id": 1},
                "contact": {"id": 42, "blocked": True},
                "changed_attributes": [
                    {"blocked": {"previous_value": False, "current_value": True}}
                ],
            },
        )
        blocked_until = datetime.now(timezone.utc)
        repository = Mock()
        repository.arm = Mock()

        with (
            patch.object(main, "contact_block_repository", repository),
            patch.object(main, "_resolve_blocked_contact_conversations", AsyncMock()),
            patch.object(
                main,
                "settings",
                main.settings.model_copy(
                    update={"chatwoot_block_duration_seconds": 604800}
                ),
            ),
            patch.object(main, "datetime") as datetime_mock,
        ):
            datetime_mock.now.return_value = blocked_until
            result = await main._process_contact_block_event(event)

        repository.arm.assert_called_once()
        call = repository.arm.call_args.kwargs
        self.assertEqual(call["account_id"], "1")
        self.assertEqual(call["contact_id"], "42")
        self.assertEqual(
            call["blocked_until"],
            blocked_until.replace() + main.timedelta(seconds=604800),
        )
        self.assertTrue(call["refresh_existing"])
        self.assertEqual(result["route"], "contact_block_armed")

    async def test_conversation_update_with_blocked_sender_arms_block(
        self,
    ) -> None:
        event = ChatwootWebhookEvent(
            event="conversation_updated",
            id=58,
            account={"id": 1},
            conversation={
                "id": 58,
                "account_id": 1,
                "meta": {"sender": {"id": 42, "blocked": True}},
            },
            raw={
                "event": "conversation_updated",
                "id": 58,
                "account": {"id": 1},
                "conversation": {
                    "id": 58,
                    "account_id": 1,
                    "meta": {"sender": {"id": 42, "blocked": True}},
                },
            },
        )
        repository = Mock()
        repository.arm = Mock()

        with (
            patch.object(main, "contact_block_repository", repository),
            patch.object(main, "_resolve_blocked_contact_conversations", AsyncMock()),
        ):
            result = await main._process_contact_block_event(event)

        repository.arm.assert_called_once()
        call = repository.arm.call_args.kwargs
        self.assertEqual(call["account_id"], "1")
        self.assertEqual(call["contact_id"], "42")
        self.assertFalse(call["refresh_existing"])
        self.assertEqual(result["route"], "contact_block_armed")

    async def test_contact_update_with_unblocked_contact_deactivates_block(self) -> None:
        event = ChatwootWebhookEvent(
            event="contact_updated",
            id=42,
            account={"id": 1},
            raw={
                "event": "contact_updated",
                "id": 42,
                "account": {"id": 1},
                "contact": {"id": 42, "blocked": False},
                "changed_attributes": [
                    {"blocked": {"previous_value": True, "current_value": False}}
                ],
            },
        )
        repository = Mock()

        with patch.object(main, "contact_block_repository", repository):
            result = await main._process_contact_block_event(event)

        repository.deactivate.assert_called_once_with(
            account_id="1", contact_id="42",
        )
        repository.arm.assert_not_called()
        self.assertEqual(result["route"], "contact_block_deactivated")


class ContactBlockRepositoryTest(unittest.TestCase):
    def test_arm_qualifies_existing_active_column(self) -> None:
        connection = Mock()
        connection.transaction = MagicMock()
        connection.transaction.return_value.__enter__.return_value = None
        cursor = connection.cursor.return_value
        pool = Mock()
        pool.acquire.return_value = connection
        repository = ContactBlockRepository(connections=pool)

        repository.arm(
            account_id="1",
            contact_id="42",
            blocked_until=datetime.now(timezone.utc),
        )

        sql = cursor.execute.call_args.args[0]
        self.assertIn("INSERT INTO chatwoot_contact_block AS current_block", sql)
        self.assertIn("WHEN current_block.active", sql)
        self.assertIn("THEN current_block.blocked_until", sql)


class MutedHandoffNoticeTest(unittest.IsolatedAsyncioTestCase):
    async def test_muted_handoff_sends_notice_without_opening_conversation(
        self,
    ) -> None:
        from ics_agent.app.chatwoot import routes as main

        client = Mock()
        client.set_conversation_status = Mock(return_value={})
        client.send_message = Mock(return_value={"id": 99})
        event = ChatwootWebhookEvent(
            event="message_created",
            account={"id": 1},
            conversation={"id": 22, "account_id": 1},
            content="转人工",
            message_type="incoming",
        )

        with (
            patch.object(main, "get_chatwoot_client", return_value=client),
            patch.object(
                main.conversation_control_repository,
                "record_conversation_status",
            ) as record_status,
        ):
            result = await main._suppress_muted_handoff(
                event=event,
                event_key="test-event",
            )

        client.set_conversation_status.assert_called_once_with(
            account_id="1",
            conversation_id="22",
            status="resolved",
        )
        self.assertEqual(
            client.send_message.call_args_list,
            [
                call(account_id="1", conversation_id="22", content="已为您转接人工客服，请稍等..."),
                call(account_id="1", conversation_id="22", content="非常抱歉，当前人工座席繁忙，请稍后再试。"),
            ],
        )
        record_status.assert_called_once()
        self.assertEqual(result["route"], "muted_contact")
        self.assertEqual(
            result["answer"],
            "非常抱歉，当前人工座席繁忙，请稍后再试。",
        )
        self.assertEqual(result["chatwoot_reply"]["message_id"], "99")


if __name__ == "__main__":
    unittest.main()
