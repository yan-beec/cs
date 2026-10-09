import asyncio
import json
import unittest
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

from ics_agent.app.chatwoot import routes
from ics_agent.app.chatwoot.client import ChatwootAPIError
from ics_agent.app.chatwoot.jobs import WebhookJob
from ics_agent.app.chatwoot.payload import parse_event
from ics_agent.app.chatwoot.state import ContactBlock, ConversationControl


STARTED_AT = datetime(2026, 10, 9, 5, tzinfo=timezone.utc)
BLOCKED_AT = STARTED_AT + timedelta(minutes=2)
RECEIVED_AT = BLOCKED_AT + timedelta(minutes=1)
THREAD_ID = "chatwoot:1:56:session:human-round"


def _control(*, handoff_required=True):
    return ConversationControl(
        account_id="1",
        conversation_id="56",
        active_thread_id=THREAD_ID,
        last_chatwoot_status="open" if handoff_required else "pending",
        ai_enabled=not handoff_required,
        handoff_required=handoff_required,
        handoff_reason="explicit_keyword" if handoff_required else None,
        handoff_at=STARTED_AT.isoformat() if handoff_required else None,
        assigned_team_id="3" if handoff_required else None,
        assigned_agent_id="9" if handoff_required else None,
        last_user_message_id="1201",
        last_ai_message_id=None,
        last_handoff_message_id=None,
        handoff_conversation_updated=handoff_required,
        handoff_label_applied=handoff_required,
        handoff_note_created=handoff_required,
        handoff_assignment_applied=handoff_required,
        handoff_notice_sent=handoff_required,
        session_started_at=STARTED_AT,
        session_closed_at=None,
        last_user_message_at=STARTED_AT + timedelta(minutes=1),
        last_status_event_at=STARTED_AT,
    )


def _event(*, contact_only=False, explicit_change=True, event_at=BLOCKED_AT):
    payload = {
        "event": "contact_updated" if contact_only else "conversation_updated",
        "id": 42 if contact_only else 56,
        "account": {"id": 1},
        "updated_at": event_at.isoformat(),
        "changed_attributes": (
            [{"blocked": {"previous_value": False, "current_value": True}}]
            if explicit_change else
            [{"last_activity_at": {
                "previous_value": STARTED_AT.isoformat(),
                "current_value": event_at.isoformat(),
            }}]
        ),
    }
    if contact_only:
        payload.pop("updated_at")
        payload["blocked"] = True
    else:
        payload["status"] = "open"
        payload["meta"] = {"sender": {"id": 42, "blocked": True}}
    return parse_event(json.dumps(payload).encode())


def _conversation(*, status="open", blocked=True):
    return {
        "id": 56,
        "display_id": 56,
        "account_id": 1,
        "status": status,
        "meta": {"sender": {"id": 42, "blocked": blocked}},
    }


class ContactBlockResolutionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.control = _control()
        self.repository = Mock()
        self.repository.get.side_effect = lambda **kwargs: self.control

        def record_status(*, status, event_at=None, **kwargs):
            self.control = replace(
                self.control,
                ai_enabled=True,
                handoff_required=False,
                last_chatwoot_status=status,
                session_closed_at=event_at,
                last_status_event_at=event_at,
            )
            return self.control

        self.repository.record_conversation_status.side_effect = record_status
        self.client = Mock()
        self.client.list_contact_conversations.return_value = [_conversation()]
        self.client.get_conversation.return_value = _conversation()
        self.client.get_contact.return_value = {"id": 42, "blocked": True}
        self.client.set_conversation_status.return_value = {"status": "resolved"}
        self.blocks = Mock()
        self.locks = {}
        stack = ExitStack()
        self.addCleanup(stack.close)
        for name, value in (
            ("conversation_control_repository", self.repository),
            ("contact_block_repository", self.blocks),
            ("_conversation_locks", self.locks),
        ):
            stack.enter_context(patch.object(routes, name, value))
        stack.enter_context(patch.object(routes, "get_chatwoot_client", return_value=self.client))

    async def resolve(self, event):
        return await routes._resolve_blocked_contact_conversations(
            event, received_at=RECEIVED_AT,
        )

    async def test_contact_event_resolves_its_ics_handoff_without_conversation_id(self):
        await routes._process_contact_block_event(
            _event(contact_only=True), received_at=RECEIVED_AT,
        )

        self.client.list_contact_conversations.assert_called_once_with(
            account_id="1", contact_id="42",
        )
        self.client.set_conversation_status.assert_called_once_with(
            account_id="1", conversation_id="56", status="resolved",
        )
        self.repository.record_conversation_status.assert_called_once_with(
            account_id="1", conversation_id="56", status="resolved", event_at=RECEIVED_AT,
        )
        self.assertFalse(self.control.handoff_required)
        self.assertTrue(self.control.ai_enabled)
        self.blocks.arm.assert_called_once()

    async def test_blocked_activity_snapshot_repairs_existing_handoff(self):
        await self.resolve(_event(explicit_change=False))

        self.client.list_contact_conversations.assert_not_called()
        self.client.set_conversation_status.assert_called_once_with(
            account_id="1", conversation_id="56", status="resolved",
        )
        self.assertFalse(self.control.handoff_required)

    async def test_repeated_blocked_snapshot_does_not_interrupt_ai_consultation(self):
        self.control = _control(handoff_required=False)
        for status in ("pending", "resolved", "open", "snoozed"):
            with self.subTest(status=status):
                self.client.get_conversation.return_value = _conversation(status=status)
                await self.resolve(_event(explicit_change=False))

        self.client.set_conversation_status.assert_not_called()
        self.repository.record_conversation_status.assert_not_called()
        self.assertIsNone(self.control.session_closed_at)

    async def test_explicit_block_ends_active_ai_consultation_once(self):
        self.control = _control(handoff_required=False)
        self.client.get_conversation.return_value = _conversation(status="pending")
        await self.resolve(_event())
        self.client.get_conversation.return_value = _conversation(status="resolved")
        await self.resolve(_event())

        self.client.set_conversation_status.assert_called_once()
        self.assertEqual(self.control.session_closed_at, BLOCKED_AT)

    async def test_old_block_event_does_not_resolve_new_consultation(self):
        self.control = replace(
            _control(), session_started_at=BLOCKED_AT + timedelta(seconds=1),
        )
        await self.resolve(_event())

        self.client.set_conversation_status.assert_not_called()
        self.repository.record_conversation_status.assert_not_called()
        self.assertTrue(self.control.handoff_required)

    async def test_non_ics_conversation_is_not_changed(self):
        self.repository.get.side_effect = None
        self.repository.get.return_value = None
        await self.resolve(_event(contact_only=True))

        self.client.set_conversation_status.assert_not_called()
        self.repository.record_conversation_status.assert_not_called()

    async def test_delayed_block_does_not_resolve_contact_already_unblocked(self):
        self.client.get_conversation.return_value = _conversation(blocked=False)
        self.client.get_contact.return_value = {"id": 42, "blocked": False}
        await self.resolve(_event())

        self.client.set_conversation_status.assert_not_called()
        self.repository.record_conversation_status.assert_not_called()

    async def test_already_resolved_handoff_is_released_without_extra_status_request(self):
        self.client.get_conversation.return_value = _conversation(status="resolved")
        await self.resolve(_event())

        self.client.set_conversation_status.assert_not_called()
        self.assertFalse(self.control.handoff_required)
        self.assertEqual(self.control.session_closed_at, BLOCKED_AT)

    async def test_explicit_block_resolves_when_conversation_omits_blocked_field(self):
        self.client.get_conversation.return_value = {
            "id": 56,
            "status": "open",
        }
        await self.resolve(_event())

        self.client.set_conversation_status.assert_called_once_with(
            account_id="1", conversation_id="56", status="resolved",
        )
        self.repository.record_conversation_status.assert_called_once_with(
            account_id="1", conversation_id="56", status="resolved", event_at=BLOCKED_AT,
        )

    async def test_status_failure_propagates_and_retry_preserves_original_block_time(self):
        self.client.set_conversation_status.side_effect = [
            ChatwootAPIError("Temporary failure", status_code=503),
            {"status": "resolved"},
        ]
        with self.assertRaises(ChatwootAPIError):
            await self.resolve(_event())
        self.repository.record_conversation_status.assert_not_called()
        self.assertTrue(self.control.handoff_required)

        await self.resolve(_event())

        self.assertEqual(self.client.set_conversation_status.call_count, 2)
        self.repository.record_conversation_status.assert_called_once_with(
            account_id="1", conversation_id="56", status="resolved", event_at=BLOCKED_AT,
        )

    async def test_contact_event_waits_for_in_flight_conversation_operation(self):
        attempted = asyncio.Event()

        class ObservedLock(asyncio.Lock):
            async def acquire(self):
                attempted.set()
                return await super().acquire()

        lock = ObservedLock()
        self.locks["1:56"] = lock
        await lock.acquire()
        attempted.clear()
        task = asyncio.create_task(self.resolve(_event(contact_only=True)))
        try:
            await asyncio.wait_for(attempted.wait(), timeout=2)
            self.assertFalse(task.done())
            self.client.set_conversation_status.assert_not_called()
        finally:
            lock.release()
            await asyncio.wait_for(task, timeout=2)

        self.client.set_conversation_status.assert_called_once_with(
            account_id="1", conversation_id="56", status="resolved",
        )


class BlockedMessageRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_message_after_missed_block_event_recovers_ai_without_becoming_old_message(self):
        human_control = _control()
        ai_control = replace(
            human_control, handoff_required=False, ai_enabled=True,
            last_chatwoot_status="resolved",
        )
        repository = Mock()
        repository.record_user_message.return_value = human_control
        repository.get.return_value = ai_control
        repository.release_handoff.return_value = ai_control
        payload = {
            "event": "message_created",
            "id": 1202,
            "account": {"id": 1},
            "conversation": _conversation(),
            "message_type": "incoming",
            "sender": {"id": 42, "type": "contact", "blocked": True},
            "content": "如何修改手机号？",
            "created_at": RECEIVED_AT.isoformat(),
        }
        job = WebhookJob(
            "blocked-human-recovery", parse_event(json.dumps(payload).encode()),
            received_at=RECEIVED_AT,
        )
        answer = {
            "route": "rag", "faq_route": "rag", "answer": "请打开账户设置。",
            "intent_label": "account", "intent_confidence": 0.9,
        }
        with (
            patch.object(routes, "conversation_control_repository", repository),
            patch.object(routes, "_chatwoot_contact_blocked", AsyncMock(return_value=True)),
            patch.object(routes, "_refresh_handoff_from_chatwoot_status", AsyncMock(return_value=human_control)),
            patch.object(routes, "_can_auto_reply", return_value=False),
            patch.object(routes, "_build_webhook_answer", AsyncMock(return_value=answer)) as build_answer,
            patch.object(routes, "_send_chatwoot_auto_reply", AsyncMock(return_value={"sent": True})),
            patch.object(routes, "_update_chatwoot_typing", AsyncMock()),
        ):
            result = await routes._process_webhook_job_unlocked(job)

        repository.release_handoff.assert_called_once_with(
            account_id="1", conversation_id="56", status="resolved",
        )
        repository.record_conversation_status.assert_not_called()
        build_answer.assert_awaited_once()
        self.assertEqual(result["route"], "rag")
        self.assertTrue(result["chatwoot_reply"]["sent"])


class ContactBlockReaperTest(unittest.IsolatedAsyncioTestCase):
    async def test_due_block_is_released_by_one_scan(self):
        repository = Mock()
        block = ContactBlock(
            account_id="1",
            contact_id="42",
            blocked_until=BLOCKED_AT,
        )
        repository.list_due.return_value = [block]
        repository.deactivate.return_value = None
        client = Mock()
        client.set_contact_blocked.return_value = {"blocked": False}

        with (
            patch.object(routes, "contact_block_repository", repository),
            patch.object(routes, "get_chatwoot_client", return_value=client),
        ):
            await routes._expire_due_contact_blocks_once()

        client.set_contact_blocked.assert_called_once_with(
            account_id="1", contact_id="42", blocked=False,
        )
        repository.deactivate.assert_called_once_with(
            account_id="1", contact_id="42",
        )

    async def test_failed_release_remains_active_for_next_scan(self):
        repository = Mock()
        repository.list_due.return_value = [
            ContactBlock(account_id="1", contact_id="42", blocked_until=BLOCKED_AT),
        ]
        client = Mock()
        client.set_contact_blocked.side_effect = ChatwootAPIError(
            "temporary", status_code=503,
        )

        with (
            patch.object(routes, "contact_block_repository", repository),
            patch.object(routes, "get_chatwoot_client", return_value=client),
        ):
            await routes._expire_due_contact_blocks_once()

        repository.deactivate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
