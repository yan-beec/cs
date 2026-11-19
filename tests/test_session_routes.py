import asyncio
import json
import threading
import unittest
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from starlette.requests import Request

from ics_agent.app.chatwoot import jobs, routes
from ics_agent.app.chatwoot.jobs import WebhookClaim, WebhookJob, WebhookJobQueue
from ics_agent.app.chatwoot.payload import parse_event
from ics_agent.app.chatwoot.state import ConversationControl
from ics_agent.app.core.config import AppSettings
from ics_agent.rag.config import RAGSettings


STARTED_AT = datetime(2026, 10, 8, 1, tzinfo=timezone.utc)
MESSAGE_AT = STARTED_AT + timedelta(minutes=1)
CLOSED_AT = STARTED_AT + timedelta(minutes=2)
RECEIVED_AT = STARTED_AT + timedelta(minutes=3)
THREAD_ID = "chatwoot:1:56:session:round-2"
ANSWER = {
    "route": "rag",
    "faq_route": "rag",
    "answer": "可以在账户设置中修改。",
    "intent_label": "account",
    "intent_confidence": 0.9,
}


class _FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return RECEIVED_AT


def _control() -> ConversationControl:
    return ConversationControl(
        account_id="1",
        conversation_id="56",
        active_thread_id=THREAD_ID,
        last_chatwoot_status="pending",
        ai_enabled=True,
        handoff_required=False,
        handoff_reason=None,
        handoff_at=None,
        assigned_team_id=None,
        assigned_agent_id=None,
        last_user_message_id="1201",
        last_ai_message_id=None,
        last_handoff_message_id=None,
        handoff_conversation_updated=False,
        handoff_label_applied=False,
        handoff_note_created=False,
        handoff_assignment_applied=False,
        handoff_notice_sent=False,
        session_started_at=STARTED_AT,
        session_closed_at=None,
        last_user_message_at=MESSAGE_AT,
        last_status_event_at=STARTED_AT,
    )


def _message_payload() -> dict[str, object]:
    return {
        "event": "message_created",
        "id": 1201,
        "account": {"id": 1},
        "conversation": {"id": 56, "account_id": 1, "status": "open"},
        "message_type": "incoming",
        "sender": {"id": 42, "type": "contact"},
        "content": "如何修改手机号？",
        "created_at": MESSAGE_AT.isoformat(),
    }


def _status_payload(status: str = "resolved") -> dict[str, object]:
    # Chatwoot status webhooks contain the conversation at the top level.
    return {
        "event": "conversation_status_changed",
        "id": 56,
        "account": {"id": 1},
        "status": status,
        "updated_at": CLOSED_AT.timestamp(),
    }


def _conversation_update_payload(
    *, blocked: bool, status_changed: bool = False,
) -> dict[str, object]:
    payload = _status_payload()
    payload["event"] = "conversation_updated"
    payload["meta"] = {"sender": {"id": 42, "blocked": blocked}}
    payload["changed_attributes"] = [
        {"status": {"previous_value": "pending", "current_value": "resolved"}}
        if status_changed else
        {"last_activity_at": {
            "previous_value": STARTED_AT.timestamp(),
            "current_value": CLOSED_AT.timestamp(),
        }}
    ]
    return payload


def _event(payload: dict[str, object]):
    return parse_event(json.dumps(payload).encode())


def _request(payload: dict[str, object]) -> Request:
    body = json.dumps(payload).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "method": "POST", "headers": []}, receive)


class SessionRoutesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.control = _control()
        self.repository = Mock()
        self.repository.record_user_message.side_effect = lambda **kwargs: self.control
        self.repository.get.side_effect = lambda **kwargs: self.control

        def record_status(*, status, event_at=None, **kwargs):
            if status == "resolved":
                self.control = replace(
                    self.control,
                    session_closed_at=event_at,
                    last_status_event_at=event_at,
                    last_chatwoot_status=status,
                )
            return self.control

        self.repository.record_conversation_status.side_effect = record_status
        self.webhook_repository = Mock()
        self.webhook_repository.claim.return_value = WebhookClaim(True, "queued", 1)
        self.queued: list[WebhookJob] = []
        self.queue = Mock()
        self.queue.enqueue_nowait.side_effect = self.queued.append
        self.client = Mock()
        self.client.send_message.return_value = {"id": "reply-1"}
        self.rag = AsyncMock(return_value=ANSWER)
        self.real_set_status = routes._set_ai_chatwoot_status
        self.set_status = AsyncMock(return_value=True)
        self.settings = AppSettings(
            _env_file=None,
            chatwoot_auto_reply_enabled=True,
            chatwoot_webhook_secret="",
            chatwoot_agent_bot_secret="",
            chatwoot_session_idle_timeout_seconds=21600,
        )
        self.rag_settings = RAGSettings(
            _env_file=None,
            webhook_max_retries=1,
            webhook_retry_initial_delay_seconds=0.001,
        )
        stack = ExitStack()
        self.addCleanup(stack.close)
        for name, value in (
            ("settings", self.settings),
            ("rag_settings", self.rag_settings),
            ("conversation_control_repository", self.repository),
            ("contact_block_repository", Mock()),
            ("webhook_repository", self.webhook_repository),
            ("webhook_queue", self.queue),
            ("_run_rag_query", self.rag),
            ("_set_ai_chatwoot_status", self.set_status),
            ("_update_chatwoot_typing", AsyncMock()),
            ("_conversation_locks", {}),
        ):
            stack.enter_context(patch.object(routes, name, value))
        stack.enter_context(patch.object(routes, "get_chatwoot_client", return_value=self.client))

    async def receive(self, payload: dict[str, object]):
        return await routes.chatwoot_webhook(
            _request(payload), x_chatwoot_signature=None, x_chatwoot_timestamp=None,
        )

    async def test_rag_receives_persisted_consultation_thread_and_message_time(self) -> None:
        job = WebhookJob("new-round", _event(_message_payload()), received_at=RECEIVED_AT)
        result = await routes._process_webhook_job(job)

        self.rag.assert_awaited_once_with("如何修改手机号？", thread_id=THREAD_ID)
        recorded = self.repository.record_user_message.call_args.kwargs
        self.assertEqual(recorded["thread_id"], "chatwoot:1:56")
        self.assertEqual(recorded["message_at"], MESSAGE_AT)
        self.assertEqual(recorded["idle_timeout_seconds"], 21600)
        self.assertTrue(result["chatwoot_reply"]["sent"])

    async def test_old_message_never_enters_rag_or_sends_reply(self) -> None:
        for control in (
            replace(_control(), session_closed_at=CLOSED_AT),
            replace(_control(), session_started_at=CLOSED_AT),
        ):
            with self.subTest(control=control):
                self.control = control
                result = await routes._process_webhook_job(
                    WebhookJob("old-round", _event(_message_payload()), received_at=RECEIVED_AT)
                )
                self.assertEqual(result["route"], "session_suppressed")
                self.assertEqual(result["reason"], "message_from_ended_consultation")
        self.rag.assert_not_awaited()
        self.client.send_message.assert_not_called()
        self.set_status.assert_not_awaited()

    async def test_resolved_webhook_suppresses_answer_while_rag_is_running(self) -> None:
        await self._assert_resolved_event_suppresses_answer(_status_payload())

    async def test_explicit_resolved_update_suppresses_answer_while_rag_is_running(self) -> None:
        await self._assert_resolved_event_suppresses_answer(
            _conversation_update_payload(blocked=True, status_changed=True)
        )

    async def _assert_resolved_event_suppresses_answer(
        self, payload: dict[str, object],
    ) -> None:
        rag_started = asyncio.Event()
        release_rag = asyncio.Event()

        async def generate_answer(*args, **kwargs):
            rag_started.set()
            await release_rag.wait()
            return ANSWER

        self.rag.side_effect = generate_answer
        job = WebhookJob("in-progress", _event(_message_payload()), received_at=RECEIVED_AT)
        processing = asyncio.create_task(routes._process_webhook_job(job))
        try:
            await asyncio.wait_for(rag_started.wait(), timeout=2)
            self.set_status.reset_mock()
            response = await asyncio.wait_for(self.receive(payload), timeout=2)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(self.control.session_closed_at, CLOSED_AT)
            self.assertEqual(len(self.queued), 1)
            self.assertFalse(processing.done())
        finally:
            release_rag.set()
            result = await asyncio.wait_for(processing, timeout=2)

        self.assertEqual(result["route"], "session_suppressed")
        self.assertFalse(result["chatwoot_reply"]["sent"])
        self.client.send_message.assert_not_called()
        self.set_status.assert_not_awaited()

    async def test_muted_resolved_activity_snapshot_keeps_queued_message_reply(self) -> None:
        payload = _message_payload()
        payload["conversation"]["status"] = "resolved"
        payload["conversation"]["meta"] = {"sender": {"id": 42, "blocked": True}}
        await self.receive(payload)
        message_job = self.queued[0]
        await self.receive(_conversation_update_payload(blocked=True))
        # Process the activity event first to cover both HTTP ingress and worker
        # handling before the waiting user message is allowed to run.
        await routes._process_webhook_job(self.queued[1])
        result = await routes._process_webhook_job(message_job)

        self.assertIsNone(self.control.session_closed_at)
        self.rag.assert_awaited_once_with("如何修改手机号？", thread_id=THREAD_ID)
        self.assertTrue(result["chatwoot_reply"]["sent"])
        self.client.send_message.assert_called_once()

    async def test_muted_resolved_activity_snapshot_keeps_answer_while_rag_is_running(self) -> None:
        rag_started = asyncio.Event()
        release_rag = asyncio.Event()

        async def generate_answer(*args, **kwargs):
            rag_started.set()
            await release_rag.wait()
            return ANSWER

        self.rag.side_effect = generate_answer
        payload = _message_payload()
        payload["conversation"]["status"] = "resolved"
        payload["conversation"]["meta"] = {"sender": {"id": 42, "blocked": True}}
        processing = asyncio.create_task(routes._process_webhook_job(
            WebhookJob("muted-in-progress", _event(payload), received_at=RECEIVED_AT)
        ))
        try:
            await asyncio.wait_for(rag_started.wait(), timeout=2)
            response = await self.receive(_conversation_update_payload(blocked=True))
            self.assertEqual(response.status_code, 202)
            self.assertFalse(processing.done())
        finally:
            release_rag.set()
            result = await asyncio.wait_for(processing, timeout=2)

        self.assertIsNone(self.control.session_closed_at)
        self.assertEqual(result["route"], "rag")
        self.assertTrue(result["chatwoot_reply"]["sent"])
        self.client.send_message.assert_called_once()

    async def test_unmuted_resolved_activity_snapshot_does_not_end_consultation(self) -> None:
        await self.receive(_conversation_update_payload(blocked=False))
        for job in self.queued:
            await routes._process_webhook_job(job)
        result = await routes._process_webhook_job(WebhookJob(
            "unmuted-snapshot", _event(_message_payload()), received_at=RECEIVED_AT,
        ))

        self.assertIsNone(self.control.session_closed_at)
        self.rag.assert_awaited_once_with("如何修改手机号？", thread_id=THREAD_ID)
        self.assertTrue(result["chatwoot_reply"]["sent"])
        self.client.send_message.assert_called_once()

    async def test_resolved_during_external_send_prevents_trailing_pending_update(self) -> None:
        send_started = asyncio.Event()
        release_send = threading.Event()
        loop = asyncio.get_running_loop()

        def send_message(**kwargs):
            loop.call_soon_threadsafe(send_started.set)
            if not release_send.wait(timeout=3):
                raise TimeoutError("Test did not release the message send")
            return {"id": "in-flight-reply"}

        self.client.send_message.side_effect = send_message
        sending = asyncio.create_task(routes._send_chatwoot_auto_reply(
            event=_event(_message_payload()),
            event_key="sending",
            result=ANSWER,
            thread_id=THREAD_ID,
        ))
        try:
            await asyncio.wait_for(send_started.wait(), timeout=2)
            response = await self.receive(_status_payload())
            self.assertEqual(response.status_code, 202)
            self.assertEqual(self.control.session_closed_at, CLOSED_AT)
        finally:
            release_send.set()
            result = await asyncio.wait_for(sending, timeout=2)

        self.assertTrue(result["sent"])
        self.set_status.assert_not_awaited()

    async def test_ingress_received_at_remains_stable_across_actual_worker_retry(self) -> None:
        payload = _message_payload()
        payload.pop("created_at")
        with patch.object(routes, "datetime", _FixedClock):
            response = await self.receive(payload)
        self.assertEqual(response.status_code, 202)
        job = self.queued[0]
        self.assertEqual(job.received_at, RECEIVED_AT)

        self.repository.record_user_message.side_effect = [RuntimeError("retry once"), self.control]
        queue = WebhookJobQueue(routes._process_webhook_job, self.webhook_repository)
        with patch.object(jobs, "get_rag_settings", return_value=self.rag_settings):
            await queue._process_with_retry(job, worker_index=0)

        self.assertEqual(self.repository.record_user_message.call_count, 2)
        for call in self.repository.record_user_message.call_args_list:
            self.assertEqual(call.kwargs["message_at"], RECEIVED_AT)
        self.webhook_repository.mark_completed.assert_called_once()
        self.webhook_repository.mark_failed.assert_not_called()
        self.rag.assert_awaited_once()

    async def test_boundary_storage_failure_returns_503_and_marks_claim_failed(self) -> None:
        self.repository.record_conversation_status.side_effect = RuntimeError("storage unavailable")
        with self.assertRaises(HTTPException) as failure:
            await self.receive(_status_payload())

        self.assertEqual(failure.exception.status_code, 503)
        claimed_key = self.webhook_repository.claim.call_args.kwargs["event_key"]
        self.webhook_repository.mark_failed.assert_called_once_with(
            claimed_key, "Consultation boundary could not be stored.",
        )
        self.queue.enqueue_nowait.assert_not_called()

    async def test_status_worker_uses_event_time_instead_of_job_receive_time(self) -> None:
        result = await routes._process_webhook_job(WebhookJob(
            "status-time", _event(_status_payload()), received_at=RECEIVED_AT,
        ))
        self.assertEqual(result["route"], "conversation_status")
        self.repository.record_conversation_status.assert_called_once_with(
            account_id="1", conversation_id="56", status="resolved", event_at=CLOSED_AT,
        )
        self.repository.record_user_message.assert_not_called()
        self.rag.assert_not_awaited()

    async def test_status_without_event_time_uses_same_fallback_at_ingress_and_worker(self) -> None:
        payload = _status_payload()
        payload.pop("updated_at")
        with patch.object(routes, "datetime", _FixedClock):
            await self.receive(payload)
        await routes._process_webhook_job(self.queued[0])

        self.assertEqual(self.repository.record_conversation_status.call_count, 2)
        for call in self.repository.record_conversation_status.call_args_list:
            self.assertEqual(call.kwargs["event_at"], RECEIVED_AT)

    async def test_handoff_status_lookup_propagates_api_updated_at(self) -> None:
        payload = _message_payload()
        payload["conversation"] = {"id": 56, "account_id": 1}
        self.control = replace(self.control, handoff_required=True, ai_enabled=False)
        self.client.get_conversation.return_value = {
            "status": "resolved", "updated_at": CLOSED_AT.timestamp(),
        }
        await routes._refresh_handoff_from_chatwoot_status(
            event=_event(payload), control=self.control,
        )
        self.repository.record_conversation_status.assert_called_once_with(
            account_id="1", conversation_id="56", status="resolved", event_at=CLOSED_AT,
        )

    async def test_pending_update_refuses_closed_consultation(self) -> None:
        self.control = replace(self.control, session_closed_at=CLOSED_AT)
        changed = await self.real_set_status(
            account_id="1", conversation_id="56", event_key="closed-pending",
            thread_id=THREAD_ID,
        )
        self.assertFalse(changed)
        self.client.set_conversation_status.assert_not_called()
        self.repository.record_conversation_status.assert_not_called()

    async def test_old_handoff_never_opens_or_notifies_closed_consultation(self) -> None:
        old_control = replace(self.control, handoff_required=True, ai_enabled=False)
        self.control = replace(old_control, session_closed_at=CLOSED_AT)
        event = _event(_message_payload())
        result = await routes._complete_handoff(
            event=event, job=WebhookJob("closed-handoff", event),
            control=old_control, content="转人工",
        )
        self.assertEqual(result["route"], "session_suppressed")
        self.client.set_conversation_status.assert_not_called()
        self.client.send_message.assert_not_called()
        self.client.assign_conversation.assert_not_called()

    async def test_handoff_closed_during_assignment_never_opens_or_notifies(self) -> None:
        old_control = replace(
            self.control, handoff_required=True, ai_enabled=False,
            assigned_agent_id="11", handoff_label_applied=True,
            handoff_note_created=True,
        )
        self.control = old_control

        def assign_conversation(**kwargs):
            self.control = replace(old_control, session_closed_at=CLOSED_AT)
            return {}

        self.client.assign_conversation.side_effect = assign_conversation
        event = _event(_message_payload())
        result = await routes._complete_handoff(
            event=event, job=WebhookJob("closed-during-handoff", event),
            control=old_control, content="转人工",
        )
        self.assertEqual(result["route"], "session_suppressed")
        self.client.assign_conversation.assert_called_once()
        self.client.set_conversation_status.assert_not_called()
        self.client.send_message.assert_not_called()

    async def test_http_redelivery_reuses_database_first_receipt_time(self) -> None:
        payload = _message_payload()
        payload.pop("created_at")
        self.webhook_repository.claim.side_effect = [
            WebhookClaim(True, "queued", 1, first_seen_at=RECEIVED_AT),
            WebhookClaim(True, "queued", 2, first_seen_at=RECEIVED_AT),
        ]
        for _ in range(2):
            response = await self.receive(payload)
            self.assertEqual(response.status_code, 202)
        self.assertEqual(len(self.queued), 2)
        self.assertEqual(self.queued[0].event_key, self.queued[1].event_key)
        self.assertEqual([job.received_at for job in self.queued], [RECEIVED_AT] * 2)
        for job in self.queued:
            await routes._process_webhook_job(job)
        for call in self.repository.record_user_message.call_args_list:
            self.assertEqual(call.kwargs["message_at"], RECEIVED_AT)

    async def test_status_lookup_reopens_newer_message_before_entering_rag(self) -> None:
        earlier_close = STARTED_AT + timedelta(seconds=30)
        new_thread = "chatwoot:1:56:session:after-lookup"
        self.control = replace(self.control, handoff_required=True, ai_enabled=False)
        payload = _message_payload()
        payload["conversation"] = {"id": 56, "account_id": 1}
        self.client.get_conversation.return_value = {
            "status": "resolved", "updated_at": earlier_close.timestamp(),
        }

        def record_message(**kwargs):
            if self.control.session_closed_at is not None:
                self.control = replace(
                    self.control, active_thread_id=new_thread,
                    session_started_at=MESSAGE_AT, ai_enabled=True,
                    handoff_required=False,
                )
            return self.control

        self.repository.record_user_message.side_effect = record_message
        result = await routes._process_webhook_job(WebhookJob(
            "lookup-close", _event(payload), received_at=RECEIVED_AT,
        ))

        self.assertEqual(self.repository.record_user_message.call_count, 2)
        for call in self.repository.record_user_message.call_args_list:
            self.assertEqual(call.kwargs["message_at"], MESSAGE_AT)
            self.assertEqual(call.kwargs["message_id"], "1201")
        self.rag.assert_awaited_once_with("如何修改手机号？", thread_id=new_thread)
        self.assertTrue(result["chatwoot_reply"]["sent"])

    async def test_send_guard_retries_newer_message_but_suppresses_old_message(self) -> None:
        self.control = replace(self.control, session_closed_at=CLOSED_AT)
        for message_at in (MESSAGE_AT, CLOSED_AT):
            with self.subTest(message_at=message_at):
                result = await routes._send_chatwoot_auto_reply(
                    event=_event(_message_payload()), event_key="old-send",
                    result=ANSWER, thread_id=THREAD_ID, message_at=message_at,
                )
                self.assertFalse(result["sent"])
        with self.assertRaises(routes.SessionBoundaryChanged):
            await routes._send_chatwoot_auto_reply(
                event=_event(_message_payload()), event_key="new-send",
                result=ANSWER, thread_id=THREAD_ID, message_at=RECEIVED_AT,
            )
        self.client.send_message.assert_not_called()

    async def _assert_late_close_retries_into_new_round(self, *, before_send: bool) -> None:
        earlier_close = STARTED_AT + timedelta(seconds=30)
        new_thread = "chatwoot:1:56:session:after-late-close"
        resolved_payload = _status_payload()
        resolved_payload["updated_at"] = earlier_close.timestamp()
        errors: list[Exception] = []

        def record_message(**kwargs):
            if self.control.session_closed_at is not None:
                self.control = replace(
                    self.control, active_thread_id=new_thread,
                    session_started_at=MESSAGE_AT, ai_enabled=True,
                    handoff_required=False,
                )
            return self.control

        self.repository.record_user_message.side_effect = record_message
        original_send = routes._send_chatwoot_auto_reply
        first_attempt = True

        async def close_once():
            nonlocal first_attempt
            if first_attempt:
                first_attempt = False
                await self.receive(resolved_payload)

        async def generate_answer(*args, **kwargs):
            if not before_send:
                await close_once()
            return ANSWER

        async def send_after_close(**kwargs):
            if before_send:
                await close_once()
            return await original_send(**kwargs)

        async def processor(job):
            try:
                return await routes._process_webhook_job(job)
            except routes.SessionBoundaryChanged as exc:
                errors.append(exc)
                raise

        self.rag.side_effect = generate_answer
        job = WebhookJob("late-close", _event(_message_payload()), received_at=RECEIVED_AT)
        queue = WebhookJobQueue(processor, self.webhook_repository)
        with (
            patch.object(jobs, "get_rag_settings", return_value=self.rag_settings),
            patch.object(routes, "_send_chatwoot_auto_reply", new=send_after_close),
        ):
            await queue._process_with_retry(job, worker_index=0)

        self.assertEqual(len(errors), 1)
        self.assertEqual(self.repository.record_user_message.call_count, 2)
        self.assertEqual(self.rag.await_count, 2)
        self.assertEqual(
            [call.kwargs["thread_id"] for call in self.rag.await_args_list],
            [THREAD_ID, new_thread],
        )
        self.client.send_message.assert_called_once()
        self.webhook_repository.mark_completed.assert_called_once()
        result = self.webhook_repository.mark_completed.call_args.args[1]
        self.assertEqual(result["route"], "rag")
        self.assertTrue(result["chatwoot_reply"]["sent"])
        self.webhook_repository.mark_failed.assert_not_called()

    async def test_late_resolved_after_generation_retries_newer_message(self) -> None:
        await self._assert_late_close_retries_into_new_round(before_send=False)

    async def test_late_resolved_before_send_retries_newer_message(self) -> None:
        await self._assert_late_close_retries_into_new_round(before_send=True)


if __name__ == "__main__":
    unittest.main()
