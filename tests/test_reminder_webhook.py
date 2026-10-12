"""转人工提醒保留 Webhook 的收件箱元数据。"""

import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, Mock, patch

from ics_agent.app.chatwoot import routes
from ics_agent.app.chatwoot.state import ConversationControl
from ics_agent.app.core.models import ChatwootWebhookEvent


def _control() -> ConversationControl:
    return ConversationControl(
        account_id="1", conversation_id="56", active_thread_id="chatwoot:1:56",
        last_chatwoot_status="open", ai_enabled=False, handoff_required=True,
        handoff_reason="explicit_keyword", handoff_at="2026-09-29T10:00:00+08:00",
        assigned_team_id=None, assigned_agent_id="11", last_user_message_id="1201",
        last_ai_message_id=None, last_handoff_message_id="1199",
        handoff_conversation_updated=True, handoff_label_applied=True,
        handoff_note_created=True, handoff_assignment_applied=True, handoff_notice_sent=True,
    )


def _event(**kwargs) -> ChatwootWebhookEvent:
    return ChatwootWebhookEvent(
        event="message_created", id=1211, account={"id": 1}, message_type="incoming",
        sender={"name": "客户甲"}, content="转人工", **kwargs,
    )


class WebhookInboxMetadataTest(unittest.TestCase):
    def test_reads_conversation_and_top_level_inbox_variants(self):
        cases = (
            ({"inbox_id": 7}, {"inbox": {"id": 7, "name": "官网咨询"}}, "7", "官网咨询"),
            ({"inbox": {"id": 7, "name": "官网咨询"}}, {}, "7", "官网咨询"),
            ({"meta": {"inbox": {"id": 7, "name": "微信"}}}, {}, "7", "微信"),
            ({"inbox_id": 7, "inbox_name": "微信"}, {}, "7", "微信"),
            ({}, {"inbox_id": 7, "inbox_name": "微信"}, "7", "微信"),
            ({"inbox_id": 7}, {}, "7", ""),
            ({"inbox": None}, {"inbox": []}, "", ""),
        )
        for conversation, raw, inbox_id, inbox_name in cases:
            with self.subTest(conversation=conversation, raw=raw):
                self.assertEqual(
                    routes._webhook_reminder_inbox(_event(conversation=conversation, raw=raw)),
                    {"inbox_id": inbox_id, "inbox_name": inbox_name},
                )

    def test_does_not_pair_another_inbox_name_with_conversation_inbox(self):
        event = _event(conversation={"inbox_id": 7}, raw={"inbox": {"id": 8, "name": "另一收件箱"}})
        self.assertEqual(routes._webhook_reminder_inbox(event), {"inbox_id": "7", "inbox_name": ""})


class HandoffInboxMetadataTest(unittest.IsolatedAsyncioTestCase):
    async def test_initial_handoff_captures_webhook_inbox_in_transaction_hook(self):
        control = replace(_control(), handoff_conversation_updated=False)
        event = _event(conversation={"id": 56, "inbox_id": 7},
                       raw={"inbox": {"id": 7, "name": "官网咨询"}})
        repo = Mock()
        with (
            patch.object(routes, "settings", routes.settings.model_copy(update={"reminder_enabled": True})),
            patch.object(routes, "conversation_control_repository", repo),
            patch.object(routes, "get_chatwoot_client", return_value=Mock()) as get_client,
            patch.object(routes, "_handoff_session_is_current", new=AsyncMock(return_value=True)),
            patch.object(routes, "capture_hook", return_value="transaction-hook") as capture,
        ):
            await routes._complete_handoff(
                event=event, job=routes.WebhookJob("inbox-first", event), control=control, content="转人工",
            )
        capture.assert_called_once_with(control, "客户甲", "转人工", inbox_id="7", inbox_name="官网咨询")
        repo.mark_handoff_action.assert_called_once_with(
            account_id="1", conversation_id="56", action="conversation_updated", on_mark="transaction-hook",
        )
        get_client.return_value.get_inbox.assert_not_called()

    async def test_completed_handoff_retry_supplies_metadata_to_missing_reminder(self):
        control = _control()
        event = _event(conversation={"id": 56, "meta": {"inbox": {"id": 7, "name": "微信"}}})
        with (
            patch.object(routes, "settings", routes.settings.model_copy(update={"reminder_enabled": True})),
            patch.object(routes, "conversation_control_repository", Mock()),
            patch.object(routes, "get_chatwoot_client", return_value=Mock()),
            patch.object(routes, "_handoff_session_is_current", new=AsyncMock(return_value=True)),
            patch.object(routes, "ensure_notification") as ensure,
        ):
            await routes._complete_handoff(
                event=event, job=routes.WebhookJob("inbox-retry", event), control=control, content="转人工",
            )
        ensure.assert_called_once_with(control, "客户甲", "转人工", inbox_id="7", inbox_name="微信")

    async def test_active_handoff_backfills_inbox_without_replacing_original_summary(self):
        control = _control()
        repo = Mock()
        repo.record_user_message.return_value = control
        event = _event(conversation={"id": 56, "account_id": 1, "status": "open", "inbox_id": 7},
                       raw={"inbox": {"id": 7, "name": "官网咨询"}})
        with (
            patch.object(routes, "settings", routes.settings.model_copy(update={"reminder_enabled": True})),
            patch.object(routes, "conversation_control_repository", repo),
            patch.object(routes, "_refresh_handoff_from_chatwoot_status", new=AsyncMock(return_value=control)),
            patch.object(routes, "_chatwoot_contact_blocked", new=AsyncMock(return_value=False)),
            patch.object(routes, "ensure_notification") as ensure,
            patch.object(routes, "_run_rag_query", new=AsyncMock()) as rag,
        ):
            result = await routes._process_webhook_job_unlocked(routes.WebhookJob("inbox-backfill", event))
        self.assertEqual(result["route"], "handoff_suppressed")
        ensure.assert_called_once_with(control, inbox_id="7", inbox_name="官网咨询")
        rag.assert_not_awaited()
