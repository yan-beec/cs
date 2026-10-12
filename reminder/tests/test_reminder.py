import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from ics_agent.app.core.auth import require_admin
from ics_agent.app.core.config import get_settings
from ics_agent.app.main import app
from reminder.backend import routes, service
from reminder.client.state import ReminderState
from reminder.client.storage import crypt
from reminder.protocol import conversation_url, handoff_key, workbench_url
from tests.test_handoff import _control


class ReminderRulesTest(unittest.TestCase):
    def test_source_uses_trigger_not_memory_round(self):
        first = replace(_control(handoff_required=True), handoff_trigger_message_id="1211")
        second = replace(first, active_thread_id="a-new-memory-round")
        self.assertEqual(handoff_key(first.handoff_trigger_message_id, first.handoff_at),
                         handoff_key(second.handoff_trigger_message_id, second.handoff_at))
        self.assertNotEqual(handoff_key("1211", first.handoff_at), handoff_key("1212", first.handoff_at))

    def test_window_deduplicates_and_reassignment_invalidates_old_confirmation(self):
        state = ReminderState()
        item = {"id": "one", "version": 1}
        self.assertTrue(state.replace([item]))
        self.assertFalse(state.replace([item, item]))
        state.confirm(item)
        self.assertEqual(state.visible(), [])
        self.assertTrue(state.replace([{**item, "version": 2}]))
        self.assertEqual(len(state.visible()), 1)
        self.assertEqual(state.confirmations, set())
        state.replace([])
        self.assertEqual(state.visible(), [])

    def test_url_rejects_credentials_and_uses_public_base(self):
        self.assertEqual(conversation_url("https://support.example", "1", "56"),
                         "https://support.example/app/accounts/1/conversations/56")
        self.assertEqual(workbench_url("http://localhost:3000/", "1"),
                         "http://localhost:3000/app/accounts/1/dashboard")
        self.assertEqual(workbench_url("", "1"), "")
        for url in ("javascript:alert(1)", "https://user:password@example.com", "https://example.com?token=x"):
            with self.assertRaises(ValueError):
                conversation_url(url, "1", "56")
            with self.assertRaises(ValueError):
                workbench_url(url, "1")

    def test_assignment_distinguishes_unassigned_from_missing_data(self):
        self.assertEqual(service.assignment({"status": "open", "meta": {"assignee": None}}), ("", "open"))
        self.assertEqual(service.assignment({"status": "open", "meta": {"assignee": {"id": 11}}}), ("11", "open"))
        with self.assertRaises(service.ChatwootAPIError):
            service.assignment({"status": "open"})

    @unittest.skipUnless(__import__("os").name == "nt", "Windows DPAPI")
    def test_windows_credential_protection_roundtrip(self):
        encrypted = crypt(b"test-device-token")
        self.assertNotEqual(encrypted, b"test-device-token")
        self.assertEqual(crypt(encrypted, decrypt=True), b"test-device-token")


class ReminderSnapshotTest(unittest.IsolatedAsyncioTestCase):
    async def test_trial_opens_workbench_and_handoff_keeps_its_conversation(self):
        test_item = {"id": "test-reminder", "is_test": True, "conversation_id": "test-only",
                     "agent_id": "11", "state": "pending"}
        real_item = {**test_item, "id": "real-reminder", "is_test": False, "conversation_id": "56"}
        repository = Mock()
        repository.active.return_value = [test_item, real_item]
        chatwoot = Mock()
        chatwoot.get_conversation.return_value = {"status": "open", "meta": {"assignee": {"id": 11}}}
        settings = get_settings().model_copy(update={"chatwoot_public_base_url": "http://localhost:3000"})
        with (patch.object(service, "get_repository", return_value=repository),
              patch.object(service, "get_chatwoot_client", return_value=chatwoot),
              patch.object(service, "get_settings", return_value=settings),
              patch.object(service, "_locks", {}), patch.object(service, "_last_sync", {})):
            result = await service.snapshot({"account_id": "1", "agent_id": "11"}, force=True)
        self.assertEqual([item["conversation_url"] for item in result], [
            "http://localhost:3000/app/accounts/1/dashboard",
            "http://localhost:3000/app/accounts/1/conversations/56",
        ])
        # 试验提醒只使用账户工作台，不把虚拟会话 ID 发给 Chatwoot。
        chatwoot.get_conversation.assert_called_once_with(account_id="1", conversation_id="56")
        repository.synchronize.assert_called_once_with(real_item, "11", "open")

    async def test_one_failed_conversation_does_not_block_other_reminders(self):
        failed = {"id": "failed", "is_test": False, "conversation_id": "55",
                  "agent_id": "11", "state": "pending"}
        healthy = {"id": "healthy", "is_test": False, "conversation_id": "56",
                   "agent_id": "11", "state": "pending"}
        repository = Mock()
        repository.active.return_value = [failed, healthy]
        chatwoot = Mock()
        def conversation(**kwargs):
            if kwargs["conversation_id"] == "55":
                raise service.ChatwootAPIError("conversation unavailable")
            return {"status": "open", "meta": {"assignee": {"id": 11}}}
        chatwoot.get_conversation.side_effect = conversation
        settings = get_settings().model_copy(update={"chatwoot_public_base_url": "http://localhost:3000"})
        with (patch.object(service, "get_repository", return_value=repository),
              patch.object(service, "get_chatwoot_client", return_value=chatwoot),
              patch.object(service, "get_settings", return_value=settings),
              patch.object(service, "_locks", {}), patch.object(service, "_last_sync", {})):
            result = await service.snapshot({"account_id": "1", "agent_id": "11"}, force=True)
        self.assertEqual([item["id"] for item in result], ["failed", "healthy"])
        repository.synchronize.assert_called_once_with(healthy, "11", "open")

    async def test_ack_sync_reads_only_target_notification(self):
        item = {"id": "target", "is_test": False, "conversation_id": "56",
                "agent_id": "11", "version": 4, "state": "pending"}
        repository = Mock()
        repository.active_notification.return_value = item
        chatwoot = Mock()
        chatwoot.get_conversation.return_value = {"status": "open", "meta": {"assignee": {"id": 11}}}
        with (patch.object(service, "get_repository", return_value=repository),
              patch.object(service, "get_chatwoot_client", return_value=chatwoot),
              patch.object(service, "_locks", {})):
            verified = await service.synchronize_notification(
                {"account_id": "1", "agent_id": "11"}, "target", 4)
        self.assertTrue(verified)
        repository.active_notification.assert_called_once_with("1", "target")
        repository.active.assert_not_called()
        repository.synchronize.assert_called_once_with(item, "11", "open")


class ReminderRoutesTest(unittest.TestCase):
    def setUp(self):
        self.settings = get_settings().model_copy(update={"reminder_enabled": True, "chatwoot_account_id": "1"})
        self.settings_patch = patch.object(routes, "get_settings", return_value=self.settings)
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        self.repo = Mock()
        self.repo_patch = patch.object(routes, "get_repository", return_value=self.repo)
        self.repo_patch.start()
        self.addCleanup(self.repo_patch.stop)
        self.client = TestClient(app)
        self.device = {"id": "device", "account_id": "1", "agent_id": "11", "agent_name": "张三"}

    def test_admin_binding_requires_existing_admin_auth(self):
        response = self.client.post("/admin/reminder/devices", json={"agent_id": "11"})
        self.assertEqual(response.status_code, 401)
        self.repo.bind_device.assert_not_called()

    def test_bind_validates_against_chatwoot_agent_list(self):
        app.dependency_overrides[require_admin] = lambda: Mock()
        self.addCleanup(app.dependency_overrides.pop, require_admin)
        client = Mock()
        client.list_agents.return_value = [{"id": 11, "name": "张三", "availability_status": "online"}]
        with patch.object(routes, "get_chatwoot_client", return_value=client):
            denied = self.client.post("/admin/reminder/devices", json={"agent_id": "99"})
            self.assertEqual(denied.status_code, 400)
            self.repo.bind_device.assert_not_called()
            self.repo.bind_device.return_value = {"token": "device-token", "device_id": "device", "agent_name": "张三"}
            accepted = self.client.post("/admin/reminder/devices", json={"agent_id": "11"})
        self.assertEqual(accepted.status_code, 200)
        self.repo.bind_device.assert_called_once_with("1", "11", "张三")

    def test_poll_cannot_select_a_different_agent(self):
        self.repo.authenticate_device.return_value = self.device
        with patch.object(routes, "snapshot", new=AsyncMock(return_value=[])) as snapshot:
            response = self.client.get("/reminder/notifications?agent_id=99", headers={"Authorization": "Bearer device-token"})
        self.assertEqual(response.status_code, 200)
        snapshot.assert_awaited_once_with(self.device)

    def test_revoked_device_cannot_poll(self):
        self.repo.authenticate_device.return_value = None
        self.assertEqual(self.client.get("/reminder/notifications", headers={"Authorization": "Bearer revoked"}).status_code, 401)

    def test_stale_acknowledgment_conflicts(self):
        self.repo.authenticate_device.return_value = self.device
        with patch.object(routes, "synchronize_notification", new=AsyncMock(return_value=False)) as synchronize:
            response = self.client.post("/reminder/notifications/notification/ack", json={"version": 1},
                                        headers={"Authorization": "Bearer device-token"})
        self.assertEqual(response.status_code, 409)
        synchronize.assert_awaited_once_with(self.device, "notification", 1)
        self.repo.acknowledge.assert_not_called()

    def test_acknowledgment_only_synchronizes_target_notification(self):
        self.repo.authenticate_device.return_value = self.device
        self.repo.acknowledge.return_value = True
        with patch.object(routes, "synchronize_notification", new=AsyncMock(return_value=True)) as synchronize:
            response = self.client.post("/reminder/notifications/notification/ack", json={"version": 3},
                                        headers={"Authorization": "Bearer device-token"})
        self.assertEqual(response.status_code, 200)
        synchronize.assert_awaited_once_with(self.device, "notification", 3)
        self.repo.acknowledge.assert_called_once_with(self.device, "notification", 3)


class HandoffReminderHookTest(unittest.IsolatedAsyncioTestCase):
    async def test_hook_is_attached_after_assignment_and_open(self):
        from ics_agent.app.chatwoot import routes as handoff
        from ics_agent.app.core.models import ChatwootWebhookEvent
        initial = replace(_control(handoff_required=True), assigned_agent_id="11",
                          handoff_trigger_message_id="1211", handoff_assignment_applied=False,
                          handoff_conversation_updated=False, handoff_notice_sent=True)
        repo = Mock()
        repo.mark_handoff_action.return_value = True
        client = Mock()
        event = ChatwootWebhookEvent(account={"id": 1}, conversation={"id": 56}, id=1211,
                                     sender={"name": "客户甲"}, content="转人工")
        events = []
        client.assign_conversation.side_effect = lambda **kw: events.append("assign")
        client.set_conversation_status.side_effect = lambda **kw: events.append("open")
        repo.mark_handoff_action.side_effect = lambda **kw: events.append(kw["action"]) or True
        with (patch.object(handoff, "settings", handoff.settings.model_copy(update={"reminder_enabled": True})),
              patch.object(handoff, "conversation_control_repository", repo),
              patch.object(handoff, "get_chatwoot_client", return_value=client),
              patch.object(handoff, "_handoff_session_is_current", new=AsyncMock(return_value=True)),
              patch.object(handoff, "capture_hook", return_value="transaction-hook") as hook):
            await handoff._complete_handoff(event=event, job=handoff.WebhookJob("test", event),
                                           control=initial, content="转人工")
        self.assertLess(events.index("assign"), events.index("open"))
        self.assertLess(events.index("open"), events.index("conversation_updated"))
        mark = next(call for call in repo.mark_handoff_action.call_args_list if call.kwargs["action"] == "conversation_updated")
        self.assertEqual(mark.kwargs["on_mark"], "transaction-hook")
        hook.assert_called_once_with(initial, "客户甲", "转人工")
