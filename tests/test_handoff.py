import unittest
from dataclasses import replace
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
    def test_image_message_without_text_is_processed(self) -> None:
        event = ChatwootWebhookEvent(
            event="message_created",
            id=1200,
            account={"id": 1},
            conversation={"id": 56, "account_id": 1},
            message_type="incoming",
            content="",
            raw={
                "attachments": [
                    {
                        "file_type": "image",
                        "data_url": "https://example.test/photo.jpg",
                    }
                ]
            },
        )

        self.assertTrue(main._should_process_webhook_event(event))

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
            raw={"sender": {"id": 42, "blocked": False}},
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

    async def test_same_handoff_retry_reuses_assignee_when_agent_is_unavailable(
        self,
    ) -> None:
        # assignment 之后的队列重试不能因坐席暂时离线而重新轮询换人。
        control = replace(
            _control(handoff_required=True),
            assigned_agent_id="11",
            handoff_trigger_message_id="1210",
            handoff_assignment_applied=False,
            handoff_notice_sent=False,
        )
        repository = Mock()
        repository.record_user_message = Mock(return_value=control)
        repository.request_handoff = Mock(return_value=(control, False))
        event = ChatwootWebhookEvent(
            event="message_created",
            id=1210,
            account={"id": 1},
            conversation={"id": 56, "account_id": 1, "status": "pending"},
            message_type="incoming",
            content="转人工",
        )
        job = main.WebhookJob(event_key="test-handoff-retry", event=event)

        with (
            patch.object(main, "conversation_control_repository", repository),
            patch.object(
                main,
                "_refresh_handoff_from_chatwoot_status",
                new=AsyncMock(return_value=control),
            ),
            patch.object(
                main,
                "_has_available_handoff_agent",
                new=AsyncMock(return_value=False),
            ) as has_available,
            patch.object(
                main,
                "_chatwoot_contact_blocked",
                new=AsyncMock(return_value=False),
            ),
            patch.object(
                main,
                "_complete_handoff",
                new=AsyncMock(return_value={"route": "handoff"}),
            ) as complete_handoff,
        ):
            result = await main._process_webhook_job_unlocked(job)

        has_available.assert_not_awaited()
        complete_handoff.assert_awaited_once()
        self.assertEqual(result["route"], "handoff")

    async def test_disabled_handoff_does_not_reserve_or_write_control(self) -> None:
        control = _control(handoff_required=False)
        repository = Mock()
        repository.record_user_message = Mock(return_value=control)
        event = ChatwootWebhookEvent(
            event="message_created",
            id=1212,
            account={"id": 1},
            conversation={"id": 56, "account_id": 1, "status": "pending"},
            message_type="incoming",
            content="转人工",
        )
        job = main.WebhookJob(event_key="test-disabled-handoff", event=event)

        with (
            patch.object(main, "conversation_control_repository", repository),
            patch.object(
                main,
                "_refresh_handoff_from_chatwoot_status",
                new=AsyncMock(return_value=control),
            ),
            patch.object(
                main,
                "_has_available_handoff_agent",
                new=AsyncMock(),
            ) as has_available,
            patch.object(
                main,
                "_complete_handoff",
                new=AsyncMock(return_value={"route": "handoff_disabled"}),
            ) as complete_handoff,
            patch.object(
                main,
                "settings",
                main.settings.model_copy(update={"chatwoot_handoff_enabled": False}),
            ),
        ):
            result = await main._process_webhook_job_unlocked(job)

        has_available.assert_not_awaited()
        repository.request_handoff.assert_not_called()
        complete_handoff.assert_awaited_once()
        self.assertEqual(result["route"], "handoff_disabled")

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
                "_has_available_handoff_agent",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                main,
                "_select_handoff_agent",
                new=AsyncMock(return_value="11"),
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

    async def test_image_message_requests_handoff_with_image_notice(self) -> None:
        initial_control = _control(handoff_required=False)
        handoff_control = _control(handoff_required=True)
        repository = Mock()
        repository.record_user_message = Mock(return_value=initial_control)
        repository.request_handoff = Mock(return_value=(handoff_control, True))
        event = ChatwootWebhookEvent(
            event="message_created",
            id=1205,
            account={"id": 1},
            conversation={"id": 56, "account_id": 1, "status": "pending"},
            message_type="incoming",
            content="",
            raw={
                "attachments": [
                    {
                        "file_type": "image",
                        "data_url": "https://example.test/photo.jpg",
                    }
                ]
            },
        )
        job = main.WebhookJob(event_key="test-image-handoff", event=event)

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
                "_has_available_handoff_agent",
                new=AsyncMock(return_value=True),
            ) as has_available_agent,
            patch.object(
                main,
                "_select_handoff_agent",
                new=AsyncMock(return_value="11"),
            ),
            patch.object(
                main,
                "_complete_handoff",
                new=AsyncMock(return_value={"route": "handoff"}),
            ) as complete_handoff,
        ):
            result = await main._process_webhook_job_unlocked(job)

        has_available_agent.assert_awaited_once()
        request_call = repository.request_handoff.call_args.kwargs
        self.assertEqual(request_call["reason"], "image_attachment")
        complete_call = complete_handoff.call_args.kwargs
        self.assertEqual(
            complete_call["notice_message"],
            main.IMAGE_HANDOFF_NOTICE_MESSAGE,
        )
        self.assertEqual(result["route"], "handoff")

    async def test_complete_handoff_assigns_before_open_and_releases_reservation(
        self,
    ) -> None:
        control = replace(
            _control(handoff_required=True),
            assigned_agent_id=None,
            handoff_conversation_updated=False,
            handoff_label_applied=False,
            handoff_note_created=False,
            handoff_assignment_applied=False,
            handoff_notice_sent=False,
        )
        event = ChatwootWebhookEvent(
            event="message_created",
            id=1211,
            account={"id": 1},
            conversation={"id": 56, "account_id": 1, "status": "pending"},
            message_type="incoming",
            content="转人工",
        )
        job = main.WebhookJob(event_key="test-complete-handoff", event=event)
        client = Mock()
        client.create_private_note.return_value = {"id": 91}
        repository = Mock()

        with (
            patch.object(main, "get_chatwoot_client", return_value=client),
            patch.object(main, "conversation_control_repository", repository),
            patch.object(
                main,
                "_select_handoff_agent",
                new=AsyncMock(return_value="11"),
            ) as select_agent,
            patch.object(
                main,
                "settings",
                main.settings.model_copy(
                    update={
                        "chatwoot_handoff_label": "handoff_required",
                        "chatwoot_handoff_notice_enabled": False,
                        "chatwoot_handoff_team_id": "",
                        "chatwoot_handoff_assignee_id": "",
                    }
                ),
            ),
        ):
            result = await main._complete_handoff(
                event=event,
                job=job,
                control=control,
                content="转人工",
            )

        select_agent.assert_awaited_once_with(event)
        client.assign_conversation.assert_called_once_with(
            account_id="1",
            conversation_id="56",
            team_id=None,
            assignee_id=11,
        )
        client.set_conversation_status.assert_called_once_with(
            account_id="1",
            conversation_id="56",
            status="open",
        )
        method_names = [call[0] for call in client.method_calls]
        self.assertLess(
            method_names.index("assign_conversation"),
            method_names.index("set_conversation_status"),
        )
        repository.release_handoff_reservation.assert_called_once_with(
            account_id="1",
            conversation_id="56",
        )
        self.assertEqual(result["route"], "handoff")

    def test_pending_status_does_not_release_handoff(self) -> None:
        self.assertFalse(main._is_handoff_release_status("pending"))
        self.assertTrue(main._is_handoff_release_status("resolved"))

    def test_agent_availability_only_accepts_online(self) -> None:
        self.assertTrue(
            main._is_available_agent({"id": 1, "availability_status": "online"})
        )
        self.assertFalse(
            main._is_available_agent({"id": 2, "availability_status": "busy"})
        )
        self.assertFalse(
            main._is_available_agent({"id": 3, "availability_status": "offline"})
        )

    async def test_explicit_handoff_without_available_agent_sends_notice_only(
        self,
    ) -> None:
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
                "_has_available_handoff_agent",
                new=AsyncMock(return_value=False),
            ),
            patch.object(
                main,
                "_suppress_no_agent_handoff",
                new=AsyncMock(return_value={"route": "handoff_no_available_agent"}),
            ) as no_agent_notice,
            patch.object(
                main,
                "_complete_handoff",
                new=AsyncMock(),
            ) as complete_handoff,
        ):
            result = await main._process_webhook_job_unlocked(job)

        repository.request_handoff.assert_not_called()
        complete_handoff.assert_not_awaited()
        no_agent_notice.assert_awaited_once()
        self.assertEqual(result["route"], "handoff_no_available_agent")

    async def test_no_agent_notice_sends_availability_message(self) -> None:
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
            result = await main._suppress_no_agent_handoff(
                event=event,
                event_key="test-no-agent-notice",
                availability_known=True,
            )

        client.set_conversation_status.assert_called_once_with(
            account_id="1",
            conversation_id="56",
            status="pending",
        )
        client.send_message.assert_called_once_with(
            account_id="1",
            conversation_id="56",
            content=main.HANDOFF_NO_AGENT_MESSAGE,
        )
        record_status.assert_called_once()
        self.assertEqual(result["route"], "handoff_no_available_agent")
        self.assertFalse(result["handoff_required"])
        self.assertEqual(result["handoff_reason"], "no_available_agent")
        self.assertEqual(result["chatwoot_reply"]["message_id"], "88")

    async def test_agent_availability_is_read_from_chatwoot(self) -> None:
        event = ChatwootWebhookEvent(
            event="message_created",
            account={"id": 1},
            conversation={"id": 56, "account_id": 1, "inbox_id": 4},
            content="转人工",
            message_type="incoming",
        )
        client = Mock()
        client.list_inbox_members = Mock(
            return_value=[
                {"id": 10, "availability_status": "busy"},
                {"id": 11, "availability_status": "online"},
            ]
        )

        with (
            patch.object(main, "get_chatwoot_client", return_value=client),
            patch.object(
                main,
                "settings",
                main.settings.model_copy(
                    update={
                        "chatwoot_handoff_team_id": "",
                        "chatwoot_handoff_assignee_id": "",
                    }
                ),
            ),
        ):
            result = await main._has_available_handoff_agent(event)

        self.assertTrue(result)
        client.list_inbox_members.assert_called_once_with(
            account_id="1",
            inbox_id=4,
        )

    async def test_agent_availability_uses_event_inbox(self) -> None:
        event = ChatwootWebhookEvent(
            event="message_created",
            account={"id": 1},
            conversation={"id": 56, "account_id": 1, "inbox_id": 3},
            content="转人工",
            message_type="incoming",
        )
        client = Mock()
        client.list_inbox_members = Mock(
            return_value=[{"id": 11, "availability_status": "online"}]
        )

        with (
            patch.object(main, "get_chatwoot_client", return_value=client),
            patch.object(
                main,
                "settings",
                main.settings.model_copy(
                    update={
                        "chatwoot_handoff_team_id": "",
                        "chatwoot_handoff_assignee_id": "",
                    }
                ),
            ),
        ):
            result = await main._has_available_handoff_agent(event)

        self.assertTrue(result)
        client.list_inbox_members.assert_called_once_with(
            account_id="1",
            inbox_id=3,
        )

    async def test_released_handoff_does_not_move_inbox(self) -> None:
        event = ChatwootWebhookEvent(
            event="conversation_updated",
            id=56,
            account={"id": 1},
            conversation={
                "id": 56,
                "account_id": 1,
                "inbox_id": 4,
                "status": "resolved",
            },
            message_type="incoming",
            content="",
            raw={
                "changed_attributes": [
                    {"status": {"previous_value": "open", "current_value": "resolved"}}
                ],
            },
        )
        repository = Mock()
        repository.record_conversation_status = Mock(
            return_value=_control(handoff_required=False)
        )
        client = Mock()

        with patch.object(main, "conversation_control_repository", repository):
            result = await main._process_webhook_job_unlocked(
                main.WebhookJob(event_key="test-resolved-no-inbox-move", event=event)
            )

        client.update_conversation.assert_not_called()
        repository.record_conversation_status.assert_called_once()
        self.assertTrue(result["handoff_released"])
        self.assertFalse(result["returned_to_ai_inbox"])


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
