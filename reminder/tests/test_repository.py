"""在隔离 PostgreSQL schema 中验证真实事务和坐席权限。"""

import unittest
from uuid import uuid4
from unittest.mock import patch

from fastapi.testclient import TestClient
from ics_agent.app.core.config import get_settings
from ics_agent.app.core.auth import require_admin
from ics_agent.app.main import app
from reminder.backend import routes
from reminder.backend import auth as plugin_auth
from unittest.mock import Mock

from ics_agent.app.chatwoot.state import ConversationControlRepository
from reminder.backend import service
from reminder.backend.repository import ReminderRepository
from tests import test_handoff_repository as handoff_tests

_SchemaConnections = handoff_tests._SchemaConnections
_connect = handoff_tests._connect
sql = handoff_tests.sql


class ReminderPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        handoff_tests.HandoffRepositoryPostgresTest.setUpClass.__func__(cls)

    def setUp(self):
        self.schema_name = "test_reminder_" + uuid4().hex
        with _connect() as connection:
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema_name)))
        self.addCleanup(self.cleanup_schema)
        self.connections = _SchemaConnections(self.schema_name)
        self.controls = ConversationControlRepository(self.connections)
        self.controls.ensure_table()
        self.repo = ReminderRepository(self.connections)
        self.repo.ensure_table()

    def cleanup_schema(self):
        self.assertRegex(self.schema_name, r"\Atest_reminder_[a-f0-9]{32}\Z")
        with _connect() as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema_name)))

    def prepare(self, message_id="1211"):
        self.controls.record_user_message(account_id="1", conversation_id="56", thread_id="test",
                                          message_id=message_id, chatwoot_status="pending")
        self.controls.request_handoff(account_id="1", conversation_id="56", thread_id=None,
                                      message_id=message_id, reason="explicit_keyword")
        with self.connections.acquire() as connection:
            connection.execute("UPDATE chatwoot_conversation_control SET assigned_agent_id='11', "
                               "handoff_assignment_applied=TRUE,last_chatwoot_status='open' "
                               "WHERE account_id='1' AND conversation_id='56'")
        return self.controls.get(account_id="1", conversation_id="56")

    def capture(self, control, **inbox):
        with patch.object(service, "get_repository", return_value=self.repo):
            return self.controls.mark_handoff_action(account_id="1", conversation_id="56",
                    action="conversation_updated", on_mark=service.capture_hook(control, "客户甲", "转人工", **inbox))

    def test_existing_notification_table_migrates_without_changing_reminder_state(self):
        self.capture(self.prepare())
        before = self.repo.active("1")[0]
        self.repo.acknowledge({"account_id": "1", "agent_id": "11"}, before["id"], before["version"])
        with self.connections.acquire() as connection:
            connection.execute("ALTER TABLE reminder_notifications DROP COLUMN inbox_id, DROP COLUMN inbox_name")
        self.repo.ensure_table()
        migrated = self.repo.active("1")[0]
        self.assertEqual(migrated["state"], "acknowledged")
        self.assertEqual(migrated["version"], before["version"])
        self.assertEqual((migrated["inbox_id"], migrated["inbox_name"]), ("", ""))

    def test_inbox_metadata_survives_capture_retry_and_confirmation(self):
        control = self.prepare()
        self.capture(control, inbox_id="7", inbox_name="官网咨询")
        item = self.repo.active("1")[0]
        self.assertEqual((item["inbox_id"], item["inbox_name"]), ("7", "官网咨询"))
        self.assertTrue(self.repo.acknowledge({"account_id": "1", "agent_id": "11"}, item["id"], 1))
        self.capture(control)
        persisted = self.repo.active("1")[0]
        self.assertEqual((persisted["inbox_id"], persisted["inbox_name"]), ("7", "官网咨询"))
        self.repo.update_inbox(persisted, "8", "微信咨询")
        updated = self.repo.active("1")[0]
        self.assertEqual((updated["inbox_id"], updated["inbox_name"]), ("8", "微信咨询"))
        self.assertEqual(updated["version"], 1)
        self.assertEqual(updated["state"], "acknowledged")

    def test_late_handoff_capture_enriches_existing_notification_without_new_version(self):
        control = self.prepare()
        self.capture(control)
        self.repo.ensure_handoff(control, inbox_id="7", inbox_name="官网咨询")
        item = self.repo.active("1")[0]
        self.assertEqual((item["inbox_id"], item["inbox_name"]), ("7", "官网咨询"))
        self.assertEqual(item["version"], 1)
        self.assertEqual(len(self.repo.active("1")), 1)

    def test_atomic_capture_retry_and_confirmation_does_not_revive(self):
        control = self.prepare()
        self.assertTrue(self.capture(control))
        self.assertTrue(self.capture(control))
        items = self.repo.active("1")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["agent_id"], "11")
        device = {"account_id": "1", "agent_id": "11"}
        self.assertTrue(self.repo.acknowledge(device, items[0]["id"], 1))
        self.capture(control)
        self.assertEqual(self.repo.active("1")[0]["state"], "acknowledged")

    def test_notification_failure_rolls_back_success_marker_and_insert(self):
        control = self.prepare()
        def failed(cursor, current):
            self.repo.capture(cursor, current)
            raise RuntimeError("injected storage failure")
        with self.assertRaises(RuntimeError):
            self.controls.mark_handoff_action(account_id="1", conversation_id="56",
                                              action="conversation_updated", on_mark=failed)
        self.assertFalse(self.controls.get(account_id="1", conversation_id="56").handoff_conversation_updated)
        self.assertEqual(self.repo.active("1"), [])
        self.assertTrue(self.capture(control))

    def test_old_hook_cannot_notify_after_resolved(self):
        control = self.prepare()
        self.controls.release_handoff(account_id="1", conversation_id="56", status="resolved")
        self.assertFalse(self.capture(control))
        self.assertEqual(self.repo.active("1"), [])

    def test_resolved_cancels_and_new_trigger_can_notify_again(self):
        self.capture(self.prepare())
        self.controls.release_handoff(account_id="1", conversation_id="56", status="resolved")
        self.assertEqual(self.repo.active("1"), [])
        self.capture(self.prepare("1212"))
        self.assertEqual(len(self.repo.active("1")), 1)
        self.assertEqual(self.repo.active("1")[0]["source_key"], "message:1212")

    def test_reassignment_rejects_old_ack_and_a_to_b_to_a_gets_new_version(self):
        self.capture(self.prepare())
        item = self.repo.active("1")[0]
        self.repo.synchronize(item, "12", "open")
        self.assertFalse(self.repo.acknowledge({"account_id": "1", "agent_id": "11"}, item["id"], 1))
        changed = self.repo.active("1")[0]
        self.assertEqual(changed["version"], 2)
        self.repo.synchronize(changed, "11", "open")
        self.assertEqual(self.repo.active("1")[0]["version"], 3)

    def test_device_tokens_are_hashed_and_rebinding_revokes_previous(self):
        first = self.repo.bind_device("1", "11", "张三")
        self.assertEqual(self.repo.authenticate_device(first["token"])["agent_id"], "11")
        with self.connections.acquire() as connection:
            stored = connection.execute("SELECT token_hash FROM reminder_devices WHERE id=%s",
                                        (first["device_id"],)).fetchone()[0]
        self.assertNotEqual(stored, first["token"])
        second = self.repo.bind_device("1", "11", "张三")
        self.assertIsNone(self.repo.authenticate_device(first["token"]))
        self.assertIsNotNone(self.repo.authenticate_device(second["token"]))
        self.assertFalse(self.repo.revoke_device(second["device_id"], "other-account"))

    def test_plugin_seat_keeps_three_devices_then_revokes_oldest(self):
        devices = [self.repo.bind_device("1", "11", "张三", auth_kind="plugin",
                                         auth_version="version", max_devices=3)
                   for _ in range(3)]
        for device in devices:
            self.assertIsNotNone(self.repo.authenticate_device(device["token"]))

        fourth = self.repo.bind_device("1", "11", "张三", auth_kind="plugin",
                                       auth_version="version", max_devices=3)
        active = []
        for device in devices + [fourth]:
            if self.repo.authenticate_device(device["token"]) is not None:
                active.append(device)
        self.assertEqual(len(active), 3)
        self.assertIn(fourth, active)

    def test_legacy_handoff_time_key_and_trial_lifecycle(self):
        self.capture(self.prepare(None))
        self.assertTrue(self.repo.active("1")[0]["source_key"].startswith("at:"))
        device = self.repo.bind_device("1", "12", "李四")
        value = self.repo.create_test(device["device_id"], "1")
        item = next(row for row in self.repo.active("1") if row["id"] == value)
        self.assertTrue(item["is_test"])
        self.assertFalse(self.repo.acknowledge({"account_id": "1", "agent_id": "11"}, value, 1))
        self.assertTrue(self.repo.acknowledge({"account_id": "1", "agent_id": "12"}, value, 1))

    def test_http_bind_trial_poll_ack_and_reconnect_use_real_storage(self):
        settings = get_settings().model_copy(update={"reminder_enabled": True, "chatwoot_account_id": "1"})
        chatwoot = Mock()
        chatwoot.list_agents.return_value = [{"id": 11, "name": "张三", "availability_status": "online"}]
        app.dependency_overrides[require_admin] = lambda: Mock()
        self.addCleanup(app.dependency_overrides.pop, require_admin)
        service._locks.clear()
        service._last_sync.clear()
        with (patch.object(routes, "get_settings", return_value=settings),
              patch.object(routes, "get_repository", return_value=self.repo),
              patch.object(service, "get_repository", return_value=self.repo),
              patch.object(routes, "get_chatwoot_client", return_value=chatwoot)):
            client = TestClient(app)
            bound = client.post("/admin/reminder/devices", json={"agent_id": "11"})
            self.assertEqual(bound.status_code, 200)
            device = bound.json()
            trial = client.post("/admin/reminder/test", json={"device_id": device["device_id"]})
            self.assertEqual(trial.status_code, 200)
            headers = {"Authorization": "Bearer " + device["token"]}
            listed = client.get("/reminder/notifications", headers=headers)
            self.assertEqual(listed.status_code, 200)
            item = listed.json()["notifications"][0]
            self.assertTrue(item["is_test"])
            self.assertEqual(item["id"], trial.json()["notification_id"])
            accepted = client.post(f'/reminder/notifications/{item["id"]}/ack', json={"version": item["version"]}, headers=headers)
            self.assertEqual(accepted.status_code, 200)
            self.assertEqual(TestClient(app).get("/reminder/notifications", headers=headers).json()["notifications"], [])

    def test_http_reassignment_and_resolved_sync_from_chatwoot(self):
        self.capture(self.prepare())
        first = self.repo.bind_device("1", "11", "张三")
        second = self.repo.bind_device("1", "12", "李四")
        settings = get_settings().model_copy(update={"reminder_enabled": True, "chatwoot_account_id": "1",
                                                    "chatwoot_public_base_url": "https://support.example"})
        chatwoot = Mock()
        chatwoot.get_conversation.return_value = {"status": "open", "meta": {"assignee": {"id": 11}}}
        service._locks.clear()
        service._last_sync.clear()
        with (patch.object(routes, "get_settings", return_value=settings),
              patch.object(service, "get_settings", return_value=settings),
              patch.object(routes, "get_repository", return_value=self.repo),
              patch.object(service, "get_repository", return_value=self.repo),
              patch.object(service, "get_chatwoot_client", return_value=chatwoot)):
            client = TestClient(app)
            header_a = {"Authorization": "Bearer " + first["token"]}
            header_b = {"Authorization": "Bearer " + second["token"]}
            item = client.get("/reminder/notifications", headers=header_a).json()["notifications"][0]
            self.assertEqual(item["conversation_url"], "https://support.example/app/accounts/1/conversations/56")
            self.assertEqual(client.get("/reminder/notifications", headers=header_b).json()["notifications"], [])

            chatwoot.get_conversation.return_value = {"status": "open", "meta": {"assignee": {"id": 12}}}
            stale_ack = client.post(f'/reminder/notifications/{item["id"]}/ack', json={"version": 1}, headers=header_a)
            self.assertEqual(stale_ack.status_code, 409)
            changed = client.get("/reminder/notifications", headers=header_b).json()["notifications"][0]
            self.assertEqual(changed["version"], 2)
            chatwoot.get_conversation.return_value = {"status": "resolved"}
            resolved_ack = client.post(f'/reminder/notifications/{item["id"]}/ack', json={"version": 2}, headers=header_b)
            self.assertEqual(resolved_ack.status_code, 409)
            self.assertEqual(client.get("/reminder/notifications", headers=header_b).json()["notifications"], [])

    def plugin_client(self):
        settings = get_settings().model_copy(update={
            "reminder_enabled": True, "chatwoot_account_id": "1",
            "reminder_plugin_username": "reminder",
            "reminder_plugin_password": "separate-plugin-password",
            "reminder_plugin_password_hash": "",
            "reminder_plugin_secret": "plugin-test-secret-" * 3,
            "reminder_plugin_allowed_agent_ids": "11,12",
        })
        chatwoot = Mock()
        chatwoot.list_agents.return_value = [
            {"id": 11, "name": "张三", "availability_status": "online"},
            {"id": 12, "name": "李四", "availability_status": "offline"},
            {"id": 13, "name": "未授权", "availability_status": "online"},
        ]
        for module in (routes, service, plugin_auth):
            helper = patch.object(module, "get_settings", return_value=settings)
            helper.start()
            self.addCleanup(helper.stop)
        for module in (routes, service):
            helper = patch.object(module, "get_repository", return_value=self.repo)
            helper.start()
            self.addCleanup(helper.stop)
        helper = patch.object(routes, "get_chatwoot_client", return_value=chatwoot)
        helper.start()
        self.addCleanup(helper.stop)
        routes._login_attempts.clear()
        service._locks.clear()
        service._last_sync.clear()
        client = TestClient(app)
        logged_in = client.post("/reminder/plugin/login", json={"username": "reminder", "password": "separate-plugin-password"})
        self.assertEqual(logged_in.status_code, 200)
        return client, {"Authorization": "Bearer " + logged_in.json()["access_token"]}, settings

    def test_plugin_password_and_jwt_are_separate_from_admin_permissions(self):
        client, headers, settings = self.plugin_client()
        self.assertEqual(client.get("/reminder/plugin/agents").status_code, 401)
        wrong = client.post("/reminder/plugin/login", json={"username": "reminder", "password": "admin-password"})
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(client.get("/admin/reminder/agents", headers=headers).status_code, 401)
        listed = client.get("/reminder/plugin/agents", headers=headers)
        self.assertEqual([item["id"] for item in listed.json()["agents"]], ["11", "12"])
        denied = client.post("/reminder/plugin/devices", headers=headers, json={"agent_id": "13"})
        self.assertEqual(denied.status_code, 403)
        changed = settings.model_copy(update={"reminder_plugin_password": "changed-plugin-password"})
        with patch.object(plugin_auth, "get_settings", return_value=changed):
            self.assertEqual(client.get("/reminder/plugin/agents", headers=headers).status_code, 401)

    def test_shared_device_switches_without_logging_in_or_losing_pending_reminders(self):
        client, login_headers, _ = self.plugin_client()
        bound = client.post("/reminder/plugin/devices", headers=login_headers, json={"agent_id": "11"})
        self.assertEqual(bound.status_code, 200)
        device = bound.json()
        self.assertTrue(device["switchable"])
        headers = {"Authorization": "Bearer " + device["token"]}
        self.assertEqual(client.get("/reminder/device/agents", headers=headers).status_code, 200)
        trial_a = client.post("/reminder/device/test", headers=headers, json={}).json()["notification_id"]
        before = client.get("/reminder/notifications", headers=headers).json()
        self.assertEqual(before["notifications"][0]["id"], trial_a)
        stale_identity = self.repo.authenticate_device(device["token"])

        switched = client.post("/reminder/device/switch", headers=headers,
                               json={"agent_id": "12", "binding_version": 1})
        self.assertEqual(switched.status_code, 200)
        self.assertEqual(switched.json()["device_id"], device["device_id"])
        self.assertEqual(switched.json()["binding_version"], 2)
        self.assertEqual(client.get("/reminder/notifications?agent_id=11", headers=headers).json()["notifications"], [])
        self.assertFalse(self.repo.acknowledge(stale_identity, trial_a, 1))
        self.assertEqual(client.post(f"/reminder/notifications/{trial_a}/ack", headers=headers,
                                    json={"version": 1, "binding_version": 1}).status_code, 409)
        # 旧的切换请求也不能覆盖较新的席位选择。
        self.assertEqual(client.post("/reminder/device/switch", headers=headers,
                                    json={"agent_id": "11", "binding_version": 1}).status_code, 409)
        self.assertEqual(client.post("/reminder/device/switch", headers=headers,
                                    json={"agent_id": "13", "binding_version": 2}).status_code, 403)
        trial_b = client.post("/reminder/device/test", headers=headers, json={}).json()["notification_id"]
        self.assertEqual(client.get("/reminder/notifications", headers=headers).json()["notifications"][0]["id"], trial_b)
        switched_back = client.post("/reminder/device/switch", headers=headers,
                                    json={"agent_id": "11", "binding_version": 2})
        self.assertEqual(switched_back.json()["binding_version"], 3)
        self.assertEqual(client.get("/reminder/notifications", headers=headers).json()["notifications"][0]["id"], trial_a)
        self.assertEqual(client.post(f"/reminder/notifications/{trial_a}/ack", headers=headers,
                                    json={"version": 1, "binding_version": 3}).status_code, 200)
        self.assertEqual(client.get("/reminder/notifications", headers=headers).json()["notifications"], [])

    def test_plugin_switch_rejects_legacy_credentials_and_password_changes(self):
        client, login_headers, settings = self.plugin_client()
        old = self.repo.bind_device("1", "11", "张三")
        headers = {"Authorization": "Bearer " + old["token"]}
        self.assertEqual(client.post("/reminder/device/switch", headers=headers,
                                    json={"agent_id": "12", "binding_version": 1}).status_code, 403)
        bound = client.post("/reminder/plugin/devices", headers=login_headers, json={"agent_id": "11"}).json()
        headers = {"Authorization": "Bearer " + bound["token"]}
        changed = settings.model_copy(update={"reminder_plugin_password": "new-plugin-password"})
        with patch.object(routes, "get_settings", return_value=changed):
            self.assertEqual(client.get("/reminder/notifications", headers=headers).status_code, 401)
        restricted = settings.model_copy(update={"reminder_plugin_allowed_agent_ids": "12"})
        with patch.object(routes, "get_settings", return_value=restricted):
            self.assertEqual(client.get("/reminder/device/agents", headers=headers).status_code, 401)
        rotated = settings.model_copy(update={"reminder_plugin_secret": "new-plugin-secret-" * 3})
        with patch.object(routes, "get_settings", return_value=rotated):
            self.assertEqual(client.get("/reminder/notifications", headers=headers).status_code, 401)
        self.repo.revoke_device(bound["device_id"], "1")
        self.assertEqual(client.post("/reminder/device/switch", headers=headers,
                                    json={"agent_id": "12", "binding_version": 1}).status_code, 401)

    def test_switch_takes_over_target_slot_but_keeps_current_device_token(self):
        version = "plugin-config-version"
        a = self.repo.bind_device("1", "11", "张三", auth_kind="plugin", auth_version=version)
        b = self.repo.bind_device("1", "12", "李四", auth_kind="plugin", auth_version=version)
        identity = self.repo.authenticate_device(a["token"])
        self.assertIsNotNone(self.repo.switch_device_agent(identity, "12", "李四", 1))
        self.assertIsNone(self.repo.authenticate_device(b["token"]))
        self.assertEqual(self.repo.authenticate_device(a["token"])["agent_id"], "12")
        self.repo.ensure_table()
        self.assertEqual(self.repo.authenticate_device(a["token"])["binding_version"], 2)

    def test_plugin_logout_revokes_device(self):
        client, login_headers, _ = self.plugin_client()
        device = client.post("/reminder/plugin/devices", headers=login_headers, json={"agent_id": "11"}).json()
        headers = {"Authorization": "Bearer " + device["token"]}
        self.assertEqual(client.post("/reminder/device/logout", headers=headers, json={}).status_code, 200)
        self.assertEqual(client.get("/reminder/notifications", headers=headers).status_code, 401)

    def test_concurrent_switches_do_not_overwrite_a_newer_device_binding(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        bound = self.repo.bind_device("1", "11", "张三", auth_kind="plugin", auth_version="version")
        identity = self.repo.authenticate_device(bound["token"])
        barrier = Barrier(2)

        def switch(agent):
            barrier.wait(timeout=3)
            return self.repo.switch_device_agent(identity, agent, "新席位", 1)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(switch, agent) for agent in ("12", "13")]
            results = [future.result(timeout=5) for future in futures]
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.repo.authenticate_device(bound["token"])["binding_version"], 2)
