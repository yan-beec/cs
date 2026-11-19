import json
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, Mock, call, patch

from ics_agent.app.chatwoot import routes as main
from ics_agent.app.chatwoot.jobs import WebhookJob
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

    def test_web_mute_contact_update_is_not_processed(self) -> None:
        # Chatwoot 网页端静音只保留 Chatwoot 自身功能，不进入 ICS。
        for blocked in (True, False):
            with self.subTest(blocked=blocked):
                event = ChatwootWebhookEvent(
                    event="contact_updated",
                    id=42,
                    account={"id": 1},
                    raw={
                        "event": "contact_updated",
                        "id": 42,
                        "account": {"id": 1},
                        "contact": {"id": 42, "blocked": blocked},
                        "changed_attributes": [
                            {"blocked": {"previous_value": not blocked, "current_value": blocked}}
                        ],
                    },
                )

                self.assertFalse(main._should_process_webhook_event(event))

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


class WebMuteIsolationTest(unittest.IsolatedAsyncioTestCase):
    async def test_conversation_update_with_mute_change_does_not_touch_blacklist(
        self,
    ) -> None:
        for blocked in (True, False):
            with self.subTest(blocked=blocked):
                event = parse_event(json.dumps({
                    "event": "conversation_updated",
                    "id": 58,
                    "account": {"id": 1},
                    "status": "open",
                    "meta": {"sender": {"id": 42, "blocked": blocked}},
                    "changed_attributes": [
                        {"blocked": {"previous_value": not blocked, "current_value": blocked}}
                    ],
                }).encode())
                blocks = Mock()
                client = Mock()
                controls = Mock()

                with (
                    patch.object(main, "contact_block_repository", blocks),
                    patch.object(main, "conversation_control_repository", controls),
                    patch.object(main, "get_chatwoot_client", return_value=client),
                ):
                    result = await main._process_webhook_job_unlocked(
                        WebhookJob("mute-change", event),
                    )

                self.assertEqual(result["route"], "conversation_status")
                self.assertEqual(blocks.mock_calls, [])
                client.set_conversation_status.assert_not_called()
                client.set_contact_blocked.assert_not_called()


class ContactBlockRepositoryTest(unittest.TestCase):
    def _repository(self):
        connection = Mock()
        connection.transaction = MagicMock()
        connection.transaction.return_value.__enter__.return_value = None
        cursor = connection.cursor.return_value
        pool = Mock()
        pool.acquire.return_value = connection
        return ContactBlockRepository(connections=pool), cursor

    def test_is_active_requires_unexpired_active_row(self) -> None:
        repository, cursor = self._repository()
        cursor.fetchone.return_value = (1,)
        now = datetime.now(timezone.utc)

        self.assertTrue(repository.is_active(account_id="1", contact_id="42", now=now))
        sql, params = cursor.execute.call_args.args
        self.assertIn("active = TRUE AND blocked_until > %s", sql)
        self.assertEqual(params, ("1", "42", now))
        cursor.fetchone.return_value = None
        self.assertFalse(repository.is_active(account_id="1", contact_id="42", now=now))

    def test_legacy_rows_are_marked_chatwoot_muted_on_upgrade(self) -> None:
        repository, cursor = self._repository()
        repository.ensure_table()
        statements = " ".join(item.args[0] for item in cursor.execute.call_args_list)
        self.assertIn("ADD COLUMN IF NOT EXISTS chatwoot_muted BOOLEAN NOT NULL DEFAULT TRUE", statements)
        self.assertIn("ALTER COLUMN chatwoot_muted SET DEFAULT FALSE", statements)

    def test_deactivate_clears_legacy_mute_flag(self) -> None:
        repository, cursor = self._repository()
        repository.deactivate(account_id="1", contact_id="42")
        self.assertIn("chatwoot_muted = FALSE", cursor.execute.call_args.args[0])


class MutedHandoffNoticeTest(unittest.IsolatedAsyncioTestCase):
    async def test_muted_handoff_looks_like_busy_seats_and_keeps_ai(
        self,
    ) -> None:
        from ics_agent.app.chatwoot import routes as main

        client = Mock()
        client.set_conversation_status = Mock(return_value={})
        client.send_message = Mock(side_effect=[{"id": 98}, {"id": 99}])
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
            patch.object(main, "settings", main.settings.model_copy(update={
                "chatwoot_handoff_notice_enabled": True,
            })),
        ):
            result = await main._suppress_muted_handoff(
                event=event,
                event_key="test-event",
            )

        # 不结束会话（结束会触发满意度评价），保持 AI 的 pending。
        client.set_conversation_status.assert_called_once_with(
            account_id="1",
            conversation_id="22",
            status="pending",
        )
        self.assertNotIn("resolved", str(record_status.call_args_list))
        self.assertEqual(
            client.send_message.call_args_list,
            [
                call(account_id="1", conversation_id="22", content="正在为您转接人工客服，请稍等..."),
                call(account_id="1", conversation_id="22", content=main.HANDOFF_NO_AGENT_MESSAGE),
            ],
        )
        self.assertEqual(result["route"], "muted_contact")
        self.assertEqual(result["handoff_reason"], "contact_blocked")
        self.assertFalse(result["handoff_required"])
        self.assertEqual(result["answer"], main.HANDOFF_NO_AGENT_MESSAGE)
        self.assertEqual(result["chatwoot_reply"]["message_id"], "99")


if __name__ == "__main__":
    unittest.main()
