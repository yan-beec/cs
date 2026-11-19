"""在显式指定的隔离库验证快捷回复整份覆盖及版本冲突。"""

from concurrent.futures import ThreadPoolExecutor
import os
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict
import pytest

from ics_agent.app.quick_replies import routes
from ics_agent.app.quick_replies.repository import QuickRepliesConflict, QuickRepliesRepository
from ics_agent.app.quick_replies.service import QuickRepliesService


@pytest.fixture
def stored_replies():
    dsn = os.environ.get("QUICK_REPLIES_TEST_DSN")
    if not dsn:
        pytest.skip("需要显式配置独立快捷回复测试数据库")
    if not conninfo_to_dict(dsn).get("dbname", "").startswith(("quality_query_test_", "quick_replies_test_")):
        pytest.fail("快捷回复数据库测试只允许一次性测试库名称")
    repository = QuickRepliesRepository(SimpleNamespace(postgres_dsn=dsn))
    repository.open()
    account = str(uuid.uuid4().int % (2**62) + 1)
    try:
        repository.ensure_table()
        yield repository, account
    finally:
        try:
            with repository.pool.connection() as connection:
                connection.execute("DELETE FROM agent_quick_replies WHERE account_id=%s", (account,))
        finally:
            repository.close()


def reply(identity, content):
    return {"id": identity, "title": identity, "content": content}


def test_manual_upload_replaces_single_server_version_and_409_preserves_it(stored_replies):
    repository, account = stored_replies
    gateway = Mock()
    gateway.profile.return_value = {"id": "7", "name": "测试坐席", "accounts": {account}}
    service = QuickRepliesService(None, repository, gateway=gateway)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.service] = lambda: service
    headers = {"X-Chatwoot-Access-Token": "test", "X-Chatwoot-Client": "test", "X-Chatwoot-Uid": "test"}
    with TestClient(app) as client:
        def upload(revision, items):
            return client.post("/reminder/quick-replies", headers=headers,
                               json={"account_id": int(account), "revision": revision, "items": items})

        initial = upload("", [reply("a", "原内容甲"), reply("b", "原内容乙")])
        assert initial.status_code == 200
        first_revision = initial.json()["revision"]
        latest = upload(first_revision, [reply("c", "整份替换")])
        assert latest.status_code == 200
        latest_data = repository.read(account, "7")
        assert latest_data["items"] == [reply("c", "整份替换")]
        assert latest_data["revision"] != first_revision
        assert upload(first_revision, [reply("old", "旧设备内容")]).status_code == 409
        assert repository.read(account, "7") == latest_data

        # 超过合计容量时整份拒绝，不能留下截断后的服务器内容。
        oversized = [reply(f"r{index}", "字" * 12000) for index in range(26)]
        assert upload(latest_data["revision"], oversized).status_code == 422
        assert repository.read(account, "7") == latest_data
        cleared = upload(latest_data["revision"], [])
        assert cleared.status_code == 200
        assert repository.read(account, "7")["items"] == []
        with repository.pool.connection() as connection:
            rows = connection.execute("SELECT items,item_count FROM agent_quick_replies WHERE account_id=%s AND agent_id='7'",
                                      (account,)).fetchall()
        assert rows == [{"items": [], "item_count": 0}]


def test_concurrent_first_upload_keeps_one_complete_winner(stored_replies):
    repository, account = stored_replies

    def upload(label):
        try:
            return repository.save(account, "7", expected_revision="", items=[reply(label, label)],
                                   operator_name="测试坐席")
        except QuickRepliesConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(upload, ("first", "second")))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert repository.read(account, "7")["items"] == winners[0]["items"]
    with repository.pool.connection() as connection:
        row = connection.execute("SELECT count(*) AS count FROM agent_quick_replies WHERE account_id=%s AND agent_id='7'",
                                 (account,)).fetchone()
    assert row["count"] == 1
