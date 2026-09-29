import unittest
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

from ics_agent.app.chatwoot import routes as main
from ics_agent.app.chatwoot.state import ConversationControl
from ics_agent.app.core.models import ChatwootWebhookEvent


class HandoffIntentTest(unittest.TestCase):
    def test_accepts_direct_handoff_requests(self) -> None:
        positives = [
            "人工",
            "转人工",
            "我要人工客服",
            "麻烦帮我转人工",
            "请转接人工客服处理一下",
            "能不能转人工",
        ]

        for text in positives:
            with self.subTest(text=text):
                self.assertTrue(main.is_explicit_handoff_request(text))

    def test_rejects_negated_handoff_mentions(self) -> None:
        negatives = [
            "不要转人工",
            "先不要转人工，我再看看",
            "不需要人工客服",
            "不用人工服务",
            "别给我转人工",
            "不要给我转人工",
        ]

        for text in negatives:
            with self.subTest(text=text):
                self.assertFalse(main.is_explicit_handoff_request(text))

    def test_rejects_informational_or_question_mentions(self) -> None:
        negatives = [
            "人工服务时间是几点",
            "人工客服入口在哪",
            "为什么总是转人工",
            "这个需要人工审核吗",
            "你是人工智能客服吗",
            "你是人工客服吗",
        ]

        for text in negatives:
            with self.subTest(text=text):
                self.assertFalse(main.is_explicit_handoff_request(text))


class HandoffProcessingTest(unittest.IsolatedAsyncioTestCase):
    async def test_active_handoff_suppresses_ai_reply(self) -> None:
        control = _control(handoff_required=True)
        repository = Mock()
        repository.record_user_message = Mock(return_value=control)
        event = ChatwootWebhookEvent(
            event="message_created",
            id=1201,
            account={"id": 1},
            conversation={"id": 56, "account_id": 1, "status": "open"},
            message_type="incoming",
            content="我补充一句",
        )
        job = main.WebhookJob(event_key="test-active-handoff", event=event)

        with (
            patch.object(
                main,
                "conversation_control_repository",
                repository,
            ),
            patch.object(
                main,
                "_refresh_handoff_from_chatwoot_status",
                new=AsyncMock(return_value=control),
            ),
            patch.object(
                main,
                "_run_rag_query",
                new=AsyncMock(),
            ) as run_rag_query,
        ):
            result = await main._process_webhook_job_unlocked(job)

        run_rag_query.assert_not_called()
        self.assertEqual(result["route"], "handoff_suppressed")
        self.assertFalse(result["chatwoot_reply"]["sent"])

    async def test_explicit_handoff_completes_in_worker(self) -> None:
        initial_control = _control(handoff_required=False)
        handoff_control = _control(handoff_required=True)
        repository = Mock()
        repository.record_user_message = Mock(return_value=initial_control)
        repository.request_handoff = Mock(return_value=(handoff_control, True))
        event = ChatwootWebhookEvent(
            event="message_created",
            id=1202,
            account={"id": 1},
            conversation={"id": 56, "account_id": 1, "status": "pending"},
            message_type="incoming",
            content="转人工",
        )
        job = main.WebhookJob(event_key="test-explicit-handoff", event=event)

        with (
            patch.object(
                main,
                "conversation_control_repository",
                repository,
            ),
            patch.object(
                main,
                "_refresh_handoff_from_chatwoot_status",
                new=AsyncMock(return_value=initial_control),
            ),
            patch.object(
                main,
                "_chatwoot_contact_blocked",
                new=AsyncMock(return_value=False),
            ),
            patch.object(
                main,
                "_is_handoff_service_online",
                return_value=True,
            ),
            patch.object(
                main,
                "_complete_handoff",
                new=AsyncMock(return_value={"route": "handoff"}),
            ) as complete_handoff,
        ):
            result = await main._process_webhook_job_unlocked(job)

        repository.request_handoff.assert_called_once()
        complete_handoff.assert_awaited_once()
        self.assertEqual(result["route"], "handoff")

    def test_pending_status_does_not_release_handoff(self) -> None:
        self.assertFalse(main._is_handoff_release_status("pending"))
        self.assertTrue(main._is_handoff_release_status("resolved"))

    def test_handoff_service_time_boundaries(self) -> None:
        self.assertFalse(
            main._is_handoff_service_online(datetime(2026, 9, 29, 8, 59))
        )
        self.assertTrue(
            main._is_handoff_service_online(datetime(2026, 9, 29, 9, 0))
        )
        self.assertTrue(
            main._is_handoff_service_online(datetime(2026, 9, 29, 21, 59))
        )
        self.assertFalse(
            main._is_handoff_service_online(datetime(2026, 9, 29, 22, 0))
        )

    async def test_explicit_handoff_off_hours_sends_notice_only(self) -> None:
        initial_control = _control(handoff_required=False)
        repository = Mock()
        repository.record_user_message = Mock(return_value=initial_control)
        repository.request_handoff = Mock()
        event = ChatwootWebhookEvent(
            event="message_created",
            id=1203,
            account={"id": 1},
            conversation={"id": 56, "account_id": 1, "status": "pending"},
            message_type="incoming",
            content="转人工",
        )
        job = main.WebhookJob(event_key="test-off-hours-handoff", event=event)

        with (
            patch.object(
                main,
                "conversation_control_repository",
                repository,
            ),
            patch.object(
                main,
                "_refresh_handoff_from_chatwoot_status",
                new=AsyncMock(return_value=initial_control),
            ),
            patch.object(
                main,
                "_is_handoff_service_online",
                return_value=False,
            ),
            patch.object(
                main,
                "_suppress_off_hours_handoff",
                new=AsyncMock(return_value={"route": "handoff_off_hours"}),
            ) as off_hours_notice,
            patch.object(
                main,
                "_complete_handoff",
                new=AsyncMock(),
            ) as complete_handoff,
        ):
            result = await main._process_webhook_job_unlocked(job)

        repository.request_handoff.assert_not_called()
        complete_handoff.assert_not_awaited()
        off_hours_notice.assert_awaited_once()
        self.assertEqual(result["route"], "handoff_off_hours")

    async def test_off_hours_notice_sends_service_time_message(self) -> None:
        client = Mock()
        client.set_conversation_status = Mock(return_value={})
        client.send_message = Mock(return_value={"id": 88})
        event = ChatwootWebhookEvent(
            event="message_created",
            id=1204,
            account={"id": 1},
            conversation={"id": 56, "account_id": 1, "status": "pending"},
            message_type="incoming",
            content="转人工",
        )

        with (
            patch.object(main, "get_chatwoot_client", return_value=client),
            patch.object(
                main.conversation_control_repository,
                "record_conversation_status",
            ) as record_status,
        ):
            result = await main._suppress_off_hours_handoff(
                event=event,
                event_key="test-off-hours-notice",
            )

        client.set_conversation_status.assert_called_once_with(
            account_id="1",
            conversation_id="56",
            status="pending",
        )
        client.send_message.assert_called_once_with(
            account_id="1",
            conversation_id="56",
            content=main.HANDOFF_OFF_HOURS_MESSAGE,
        )
        record_status.assert_called_once()
        self.assertEqual(result["route"], "handoff_off_hours")
        self.assertFalse(result["handoff_required"])
        self.assertEqual(result["chatwoot_reply"]["message_id"], "88")


def _control(*, handoff_required: bool) -> ConversationControl:
    return ConversationControl(
        account_id="1",
        conversation_id="56",
        active_thread_id="chatwoot:1:56",
        last_chatwoot_status="open",
        ai_enabled=not handoff_required,
        handoff_required=handoff_required,
        handoff_reason="explicit_keyword" if handoff_required else None,
        handoff_at="2026-09-29T10:00:00+08:00"
        if handoff_required
        else None,
        assigned_team_id=None,
        assigned_agent_id=None,
        last_user_message_id="1201",
        last_ai_message_id=None,
        last_handoff_message_id="1199" if handoff_required else None,
        handoff_conversation_updated=True,
        handoff_label_applied=True,
        handoff_note_created=True,
        handoff_assignment_applied=True,
        handoff_notice_sent=True,
    )


if __name__ == "__main__":
    unittest.main()
