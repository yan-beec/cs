"""坐席客户端黑名单：服务编排与接口的单元测试（不连接数据库和 Chatwoot）。"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ics_agent.app.blacklist import routes
from ics_agent.app.blacklist.agent_session import AgentForbidden, AgentSession, conversation_contact
from ics_agent.app.blacklist.options import blocked_until_for
from ics_agent.app.blacklist.repository import BlacklistConflict, Operator
from ics_agent.app.blacklist.service import BlacklistInputError, BlacklistService, BlacklistUpstreamError
from ics_agent.app.chatwoot.client import ChatwootAPIError

SESSION = AgentSession(access_token="tok", client="cli", uid="agent@example.com")


def make_service(account="1"):
    settings = SimpleNamespace(chatwoot_account_id=account, chatwoot_api_timeout=5, chatwoot_base_url="http://chatwoot")
    repository = Mock()
    gateway = Mock()
    gateway.profile.return_value = {"id": "7", "name": "坐席甲", "accounts": {"1"}}
    gateway.conversation.return_value = {"id": 22, "meta": {"sender": {"id": 42, "name": "张三", "identifier": "u-42"}}}
    client = Mock()
    return BlacklistService(settings, repository, gateway=gateway, chatwoot_client=client), repository, gateway, client


class OptionsTest(unittest.TestCase):
    def test_only_four_durations(self):
        from datetime import datetime, timezone
        now = datetime(2026, 11, 9, tzinfo=timezone.utc)
        self.assertEqual((blocked_until_for("2h", now) - now).total_seconds(), 7200)
        self.assertEqual((blocked_until_for("24h", now) - now).total_seconds(), 86400)
        self.assertEqual((blocked_until_for("72h", now) - now).total_seconds(), 259200)
        self.assertIsNone(blocked_until_for("permanent", now))
        with self.assertRaises(ValueError):
            blocked_until_for("7d", now)

    def test_conversation_contact(self):
        self.assertEqual(conversation_contact({"meta": {"sender": {"id": 9, "name": "李四"}}}),
                         {"id": "9", "name": "李四", "identifier": None})
        self.assertEqual(conversation_contact({"contact_id": 5}), {"id": "5", "name": None, "identifier": None})
        self.assertEqual(conversation_contact({}), {})


class ServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_block_records_ics_blacklist_without_muting_chatwoot(self):
        service, repository, gateway, client = make_service()
        repository.block.return_value = {"id": 1}
        await service.block(SESSION, account_id="1", conversation_id="22", contact_id="42",
                            duration="72h", reason=" 多次发送引流链接 ")
        kwargs = repository.block.call_args.kwargs
        self.assertEqual(kwargs["operator"], Operator("7", "坐席甲"))
        self.assertEqual(kwargs["remark"], "多次发送引流链接")
        self.assertNotIn("reason_code", kwargs)
        self.assertEqual(kwargs["contact_name"], "张三")
        self.assertEqual(kwargs["duration_code"], "72h")
        # 黑名单与 Chatwoot 静音脱钩：拉黑不改动 contact.blocked。
        client.set_contact_blocked.assert_not_called()
        gateway.conversation.assert_called_with(SESSION, "1", "22")

    async def test_block_ends_ongoing_handoffs_after_recording(self):
        service, repository, _, _ = make_service()
        order = []
        repository.block.side_effect = lambda **kwargs: order.append("record") or {"id": 1}
        service.on_blocked = AsyncMock(side_effect=lambda **kwargs: order.append("handoff"))
        await service.block(SESSION, account_id="1", conversation_id="22", contact_id="42",
                            duration="24h", reason="辱骂坐席")
        self.assertEqual(order, ["record", "handoff"])
        kwargs = service.on_blocked.call_args.kwargs
        self.assertEqual((kwargs["account_id"], kwargs["contact_id"]), ("1", "42"))

    async def test_handoff_cleanup_failure_does_not_fail_block(self):
        service, repository, _, _ = make_service()
        repository.block.return_value = {"id": 1}
        service.on_blocked = AsyncMock(side_effect=ChatwootAPIError("down", status_code=500))
        entry = await service.block(SESSION, account_id="1", conversation_id="22", contact_id="42",
                                    duration="permanent", reason="冒充客服诈骗")
        self.assertEqual(entry, {"id": 1})
        self.assertIsNone(repository.block.call_args.kwargs["blocked_until"])

    async def test_block_rejects_contact_that_is_not_in_the_conversation(self):
        service, repository, _, client = make_service()
        with self.assertRaises(BlacklistInputError):
            await service.block(SESSION, account_id="1", conversation_id="22", contact_id="43",
                                duration="24h", reason="辱骂坐席")
        repository.block.assert_not_called()
        client.set_contact_blocked.assert_not_called()

    async def test_reason_is_required_free_text(self):
        service, repository, gateway, _ = make_service()
        for reason in (None, "", "   ", "\n\t", "x" * 501):
            with self.subTest(reason=reason), self.assertRaises(BlacklistInputError):
                await service.block(SESSION, account_id="1", conversation_id="22", contact_id="42",
                                    duration="24h", reason=reason)
        repository.block.assert_not_called()
        gateway.profile.assert_not_called()
        repository.block.return_value = {"id": 1}
        await service.block(SESSION, account_id="1", conversation_id="22", contact_id="42",
                            duration="24h", reason="x" * 500)
        self.assertEqual(repository.block.call_args.kwargs["remark"], "x" * 500)

    async def test_operator_must_belong_to_the_ics_account(self):
        service, _, gateway, _ = make_service(account="2")
        with self.assertRaises(AgentForbidden):
            await service.operator(SESSION, "1")
        service, _, gateway, _ = make_service()
        gateway.profile.return_value = {"id": "7", "name": "x", "accounts": {"3"}}
        with self.assertRaises(AgentForbidden):
            await service.operator(SESSION, "1")

    async def test_unblock_requires_remark_and_does_not_touch_chatwoot_mute(self):
        service, repository, _, client = make_service()
        with self.assertRaises(BlacklistInputError):
            await service.unblock(SESSION, account_id="1", contact_id="42", version=3, remark="")
        repository.check_unblock.return_value = {"block": {"chatwoot_muted": False}, "latest": {}}
        repository.complete_unblock.return_value = {"id": 4}
        await service.unblock(SESSION, account_id="1", contact_id="42", version=3, remark="误操作")
        client.set_contact_blocked.assert_not_called()
        self.assertEqual(repository.complete_unblock.call_args.kwargs["operator"].agent_id, "7")
        self.assertEqual(repository.complete_unblock.call_args.kwargs["remark"], "误操作")

    async def test_unblock_of_legacy_record_also_unmutes_chatwoot_once(self):
        service, repository, _, client = make_service()
        repository.check_unblock.return_value = {"block": {"chatwoot_muted": True}, "latest": {}}
        repository.complete_unblock.return_value = {"id": 4}
        await service.unblock(SESSION, account_id="1", contact_id="42", version=3, remark="误操作")
        client.set_contact_blocked.assert_called_once_with(account_id="1", contact_id="42", blocked=False)
        client.set_contact_blocked.reset_mock()
        client.set_contact_blocked.side_effect = ChatwootAPIError("down", status_code=500)
        repository.complete_unblock.reset_mock()
        with self.assertRaises(BlacklistUpstreamError):
            await service.unblock(SESSION, account_id="1", contact_id="42", version=3, remark="误操作")
        repository.complete_unblock.assert_not_called()

    async def test_change_requires_reason_and_does_not_call_chatwoot(self):
        service, repository, _, client = make_service()
        for remark in (None, "  "):
            with self.subTest(remark=remark), self.assertRaises(BlacklistInputError):
                await service.change(SESSION, account_id="1", contact_id="42", version=3, duration="permanent",
                                     remark=remark)
        repository.change.assert_not_called()
        repository.change.return_value = {"id": 5}
        await service.change(SESSION, account_id="1", contact_id="42", version=3, duration="permanent",
                             remark=" 再次辱骂，改为永久 ")
        client.set_contact_blocked.assert_not_called()
        self.assertIsNone(repository.change.call_args.kwargs["blocked_until"])
        self.assertEqual(repository.change.call_args.kwargs["remark"], "再次辱骂，改为永久")

    async def test_reconcile_names_web_blocks(self):
        service, repository, _, client = make_service()
        repository.reconcile_candidates.return_value = [{"account_id": "1", "contact_id": "42", "active": True}]
        client.get_contact.return_value = {"payload": {"name": "张三", "identifier": "u-42"}}
        repository.record_reconciled.return_value = {"id": 1}
        self.assertEqual(await service.reconcile_once(), 1)
        repository.record_reconciled.assert_called_once()
        self.assertEqual(repository.record_reconciled.call_args.kwargs["contact_name"], "张三")


class RoutesTest(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)
        self.service = MagicMock()
        self.service.operator = AsyncMock(return_value=Operator("7", "坐席甲"))
        self.headers = {"X-Chatwoot-Access-Token": "tok", "X-Chatwoot-Client": "cli", "X-Chatwoot-Uid": "u"}
        runtime = SimpleNamespace(ready=True, service=self.service)
        patcher = patch.object(routes, "get_blacklist_runtime", return_value=runtime)
        patcher.start(); self.addCleanup(patcher.stop)
        settings = patch.object(routes, "get_settings", return_value=SimpleNamespace(chatwoot_blacklist_enabled=True))
        settings.start(); self.addCleanup(settings.stop)

    def test_requires_chatwoot_login_headers(self):
        response = self.client.get("/reminder/blacklist", params={"account_id": 1})
        self.assertEqual(response.status_code, 401)

    def test_service_not_ready(self):
        with patch.object(routes, "get_blacklist_runtime", return_value=SimpleNamespace(ready=False, service=None)):
            response = self.client.get("/reminder/blacklist", params={"account_id": 1}, headers=self.headers)
        self.assertEqual(response.status_code, 503)

    def test_list_adds_labels(self):
        self.service.list_active = AsyncMock(return_value=[
            {"contact_id": "42", "reason_code": "abuse", "source": "client"},
            {"contact_id": "43", "reason_code": None, "remark": "多次发广告", "source": "client"},
        ])
        response = self.client.get("/reminder/blacklist", params={"account_id": 1}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        legacy, manual = response.json()["items"]
        self.assertEqual(legacy["reason_label"], "辱骂/骚扰")  # 旧记录的预设原因照常显示
        self.assertEqual(legacy["source_label"], "坐席客户端")
        self.assertNotIn("reason_label", manual)
        self.assertEqual(manual["remark"], "多次发广告")

    def test_block_passes_free_text_reason(self):
        self.service.block = AsyncMock(return_value={"id": 1, "action": "block", "remark": "引流"})
        response = self.client.post("/reminder/blacklist/block", headers=self.headers, json={
            "account_id": 1, "conversation_id": 2, "contact_id": 3, "duration": "24h", "reason": "引流"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.block.call_args.kwargs["reason"], "引流")
        self.assertNotIn("remark", self.service.block.call_args.kwargs)

    def test_block_rejects_old_client_payload(self):
        self.service.block = AsyncMock()
        for remark in (None, "说明"):
            response = self.client.post("/reminder/blacklist/block", headers=self.headers, json={
                "account_id": 1, "conversation_id": 2, "contact_id": 3, "duration": "24h", "reason": "abuse",
                "remark": remark})
            self.assertEqual(response.status_code, 422)
            self.assertIn("升级", response.json()["detail"])
        self.service.block.assert_not_called()

    def test_block_empty_reason_maps_to_422(self):
        self.service.block = AsyncMock(side_effect=BlacklistInputError("请填写拉黑原因。"))
        response = self.client.post("/reminder/blacklist/block", headers=self.headers, json={
            "account_id": 1, "conversation_id": 2, "contact_id": 3, "duration": "24h", "reason": " "})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "请填写拉黑原因。")

    def test_two_hour_block_and_change_accepted(self):
        self.service.block = AsyncMock(return_value={"id": 1, "action": "block", "duration_code": "2h"})
        response = self.client.post("/reminder/blacklist/block", headers=self.headers, json={
            "account_id": 1, "conversation_id": 2, "contact_id": 3, "duration": "2h", "reason": "刷屏"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.block.call_args.kwargs["duration"], "2h")
        self.assertEqual(response.json()["entry"]["duration_label"], "2 小时")
        self.service.change = AsyncMock(return_value={"id": 1, "action": "change", "duration_code": "2h"})
        response = self.client.post("/reminder/blacklist/change", headers=self.headers, json={
            "account_id": 1, "contact_id": 3, "version": 1, "duration": "2h", "remark": "缩短"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.change.call_args.kwargs["duration"], "2h")

    def test_block_rejects_unknown_duration(self):
        response = self.client.post("/reminder/blacklist/block", headers=self.headers, json={
            "account_id": 1, "conversation_id": 2, "contact_id": 3, "duration": "7d", "reason": "辱骂"})
        self.assertEqual(response.status_code, 422)

    def test_conflict_maps_to_409(self):
        self.service.change = AsyncMock(side_effect=BlacklistConflict("该记录已被其他坐席修改，请刷新后重试。"))
        response = self.client.post("/reminder/blacklist/change", headers=self.headers, json={
            "account_id": 1, "contact_id": 3, "version": 1, "duration": "24h", "remark": "延长"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("其他坐席", response.json()["detail"])

    def test_options_are_public(self):
        response = self.client.get("/reminder/blacklist/options")
        body = response.json()
        self.assertEqual([item["code"] for item in body["durations"]], ["2h", "24h", "72h", "permanent"])
        self.assertEqual(body["default_duration"], "24h")  # 2 小时只是偶尔使用，默认仍为 24 小时
        self.assertEqual(body["reasons"], [])  # 不再提供预设原因
        self.assertEqual(body["reason_mode"], "text")
        self.assertEqual(body["reason_max_length"], 500)


if __name__ == "__main__":
    unittest.main()
