"""坐席快捷回复跨设备同步：服务编排与接口的单元测试（不连接数据库和 Chatwoot）。"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ics_agent.app.blacklist.agent_session import AgentForbidden, AgentSession, AgentSessionInvalid
from ics_agent.app.quick_replies import routes
from ics_agent.app.quick_replies.repository import QuickRepliesConflict
from ics_agent.app.quick_replies.service import (
    MAX_TOTAL, QuickRepliesInputError, QuickRepliesService, clean_items,
)

SESSION = AgentSession(access_token="tok", client="cli", uid="agent@example.com")
ITEM = {"id": "reply-1", "title": " 欢迎语 ", "content": "您好！\r\n请问有什么可以帮您？"}


def make_service():
    repository = Mock()
    gateway = Mock()
    gateway.profile.return_value = {"id": "7", "name": "坐席甲", "accounts": {"1", "3"}}
    return QuickRepliesService(SimpleNamespace(), repository, gateway=gateway), repository, gateway


class CleanItemsTest(unittest.TestCase):
    def test_normalizes_title_and_newlines(self):
        self.assertEqual(clean_items([ITEM]), [{"id": "reply-1", "title": "欢迎语", "content": "您好！\n请问有什么可以帮您？"}])

    def test_rejects_invalid_items(self):
        for items in ([ITEM, ITEM], [{**ITEM, "id": "../x"}], [{**ITEM, "title": " "}], [{**ITEM, "content": ""}],
                      [{**ITEM, "content": "a" * 12001}], [{**ITEM, "content": "\x00"}], [{"id": "a"}], ["x"],
                      [{**ITEM, "id": f"r{n}"} for n in range(201)], "not-a-list"):
            with self.assertRaises(QuickRepliesInputError, msg=repr(items)[:60]):
                clean_items(items)

    def test_total_size_limit(self):
        items = [{"id": f"r{n}", "title": "t", "content": "字" * 12000} for n in range(MAX_TOTAL // 12000 + 1)]
        with self.assertRaisesRegex(QuickRepliesInputError, "总字数"):
            clean_items(items)


class ServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_read_is_scoped_to_the_verified_agent(self):
        service, repository, gateway = make_service()
        repository.read.return_value = {"revision": "", "items": [], "updated_at": None}
        result = await service.read(SESSION, "3")
        repository.read.assert_called_once_with("3", "7")
        gateway.profile.assert_called_once_with(SESSION)
        self.assertEqual(result["agent_id"], "7")

    async def test_other_workspace_is_forbidden(self):
        service, repository, _ = make_service()
        with self.assertRaises(AgentForbidden):
            await service.read(SESSION, "2")
        with self.assertRaises(AgentForbidden):
            await service.save(SESSION, "2", revision="", items=[ITEM])
        repository.read.assert_not_called()
        repository.save.assert_not_called()

    async def test_save_cleans_items_and_passes_revision(self):
        service, repository, _ = make_service()
        repository.save.side_effect = lambda *args, **kwargs: {"revision": "r2", "items": kwargs["items"], "updated_at": None}
        result = await service.save(SESSION, "1", revision="r1", items=[ITEM])
        args, kwargs = repository.save.call_args
        self.assertEqual(args, ("1", "7"))
        self.assertEqual(kwargs["expected_revision"], "r1")
        self.assertEqual(kwargs["operator_name"], "坐席甲")
        self.assertEqual(result["items"][0]["title"], "欢迎语")

    async def test_invalid_input_never_reaches_the_database(self):
        service, repository, _ = make_service()
        with self.assertRaises(QuickRepliesInputError):
            await service.save(SESSION, "1", revision="", items=[{**ITEM, "title": ""}])
        repository.save.assert_not_called()

    async def test_corrupt_stored_data_is_not_returned(self):
        service, repository, _ = make_service()
        repository.read.return_value = {"revision": "r", "items": [{"id": "bad id"}], "updated_at": None}
        with self.assertRaises(RuntimeError):
            await service.read(SESSION, "1")


class RepositoryTest(unittest.TestCase):
    """用假连接核对乐观锁分支，SQL 本身在部署环境由真实 PostgreSQL 执行。"""

    def make(self, current, inserted=True):
        from contextlib import contextmanager
        from ics_agent.app.quick_replies.repository import QuickRepliesRepository
        connection = MagicMock()
        results = iter([current, {"updated_at": "now"} if inserted else None])
        connection.execute.side_effect = lambda *args: Mock(fetchone=Mock(return_value=next(results)))

        @contextmanager
        def borrow():
            yield connection
        pool = Mock(connection=borrow)
        return QuickRepliesRepository(None, pool=pool), connection

    def test_first_save_inserts(self):
        repository, connection = self.make(None)
        result = repository.save("1", "7", expected_revision="", items=[], operator_name="甲")
        self.assertIn("INSERT", connection.execute.call_args_list[1].args[0])
        self.assertEqual(len(result["revision"]), 36)

    def test_first_save_racing_another_device_conflicts(self):
        repository, _ = self.make(None, inserted=False)
        with self.assertRaises(QuickRepliesConflict):
            repository.save("1", "7", expected_revision="", items=[], operator_name="甲")

    def test_stale_revision_conflicts(self):
        repository, connection = self.make({"revision": "11111111-1111-1111-1111-111111111111"})
        with self.assertRaises(QuickRepliesConflict):
            repository.save("1", "7", expected_revision="", items=[], operator_name="甲")
        self.assertEqual(connection.execute.call_count, 1)

    def test_matching_revision_updates(self):
        revision = "11111111-1111-1111-1111-111111111111"
        repository, connection = self.make({"revision": revision})
        repository.save("1", "7", expected_revision=revision, items=[], operator_name="甲")
        self.assertIn("UPDATE", connection.execute.call_args_list[1].args[0])


class RoutesTest(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)
        self.service = MagicMock()
        self.headers = {"X-Chatwoot-Access-Token": "tok", "X-Chatwoot-Client": "cli", "X-Chatwoot-Uid": "u"}
        patcher = patch.object(routes, "get_quick_replies_runtime",
                               return_value=SimpleNamespace(ready=True, service=self.service))
        patcher.start(); self.addCleanup(patcher.stop)
        settings = patch.object(routes, "get_settings", return_value=SimpleNamespace(chatwoot_quick_replies_enabled=True))
        settings.start(); self.addCleanup(settings.stop)

    def test_requires_chatwoot_login_headers(self):
        self.assertEqual(self.client.get("/reminder/quick-replies", params={"account_id": 1}).status_code, 401)

    def test_disabled_returns_404(self):
        with patch.object(routes, "get_settings", return_value=SimpleNamespace(chatwoot_quick_replies_enabled=False)):
            response = self.client.get("/reminder/quick-replies", params={"account_id": 1}, headers=self.headers)
        self.assertEqual(response.status_code, 404)

    def test_not_ready_returns_503(self):
        with patch.object(routes, "get_quick_replies_runtime", return_value=SimpleNamespace(ready=False, service=None)):
            response = self.client.get("/reminder/quick-replies", params={"account_id": 1}, headers=self.headers)
        self.assertEqual(response.status_code, 503)

    def test_read(self):
        self.service.read = AsyncMock(return_value={"revision": "", "items": [], "updated_at": None,
                                                    "agent_id": "7", "agent_name": "坐席甲"})
        response = self.client.get("/reminder/quick-replies", params={"account_id": 1}, headers=self.headers)
        self.assertEqual(response.json(), {"revision": "", "items": [], "updated_at": None,
                                           "agent_id": "7", "agent_name": "坐席甲"})

    def test_save_conflict_and_errors(self):
        body = {"account_id": 1, "revision": "", "items": [ITEM]}
        for error, status in ((QuickRepliesConflict("其他设备"), 409), (QuickRepliesInputError("x"), 422),
                              (AgentSessionInvalid("x"), 401), (AgentForbidden("x"), 403)):
            self.service.save = AsyncMock(side_effect=error)
            self.assertEqual(self.client.post("/reminder/quick-replies", headers=self.headers, json=body).status_code, status)

    def test_save_rejects_malformed_revision(self):
        response = self.client.post("/reminder/quick-replies", headers=self.headers,
                                    json={"account_id": 1, "revision": "x' OR 1=1", "items": []})
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
