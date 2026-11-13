"""补算只处理主查询可能命中的轮次，同时保留指定客户的搜索词例外。"""

from dataclasses import replace
from datetime import timedelta
import os
from unittest.mock import patch

import pytest
from openpyxl import load_workbook
from psycopg.types.json import Jsonb

from quality.processing.service_facts import FactSource, store
from quality.query import export as export_module
from quality.query import repository
from quality.query.export import ExportRepository
from quality.query.config import QuerySettings
from quality.query.repository import ArchiveFilters
from quality.tests import test_query_database as query_tests
from quality.tests.test_processing import START, agent_reply, event


pytestmark = pytest.mark.skipif(os.environ.get("PROCESSING_TEST_DATABASE") != "1",
                                reason="需要独立归档测试数据库")


@pytest.fixture
def scope_archive(request):
    case = query_tests.QueryDatabaseTest()
    case.setUpClass()
    request.addfinalizer(case.tearDownClass)
    request.addfinalizer(case.doCleanups)
    case.setUp()
    for conversation, contact in ((12, 42), (32, 42), (22, 99)):
        messages = [event(conversation * 10, conversation_id=conversation),
                    agent_reply(conversation * 10 + 1, 1, conversation_id=conversation),
                    event(conversation * 10 + 2, 9, conversation_id=conversation)]
        for item in messages:
            item["payload"]["conversation"]["meta"]["sender"].update(
                id=contact, name="目标客户" if contact == 42 else "其他客户")
            if item["payload"]["message_type"] == "incoming":
                item["payload"]["sender"].update(id=contact)
        case.receive(*messages)
    case.drain()
    case.admin.execute("""UPDATE ops.round_index SET data=data || %s
        WHERE source_key=%s""", (Jsonb({"responsibility_basis": "ics_assignment",
        "responsible_agent_id": "7", "responsible_assigned_at": START.isoformat()}), case.source))
    case.admin.execute("""UPDATE ops.round_index SET channel_key=NULL
        WHERE source_key=%s AND conversation_id=12""", (case.source,))
    case.admin.execute("""UPDATE ops.round_index SET round_no=2,inbox_id=4
        WHERE source_key=%s AND conversation_id=32""", (case.source,))
    case.admin.execute("""UPDATE ops.round_index SET reception_type='ai_only'
        WHERE source_key=%s AND conversation_id=22""", (case.source,))
    rows = case.rows("ops.round_index")
    case.target_rounds = {row["round_id"] for row in rows if row["conversation_id"] in (12, 32)}
    case.target_customer = next(row["customer_id"] for row in rows if row["conversation_id"] == 12)
    store(case.admin, [row["round_id"] for row in rows])
    exporter = ExportRepository(QuerySettings(_env_file=None, query_enabled=True,
        reader_password=os.environ["ARCHIVE_READER_PASSWORD"], postgres_host=case.settings.postgres_host,
        postgres_port=case.settings.postgres_port, postgres_db=case.settings.postgres_db,
        postgres_sslmode=case.settings.postgres_sslmode))
    exporter.open()
    request.addfinalizer(exporter.close)
    case.exporter = exporter
    return case


def broad_facts(connection, filters, now=None, *, customer_id=None):
    """保留变更前范围作为结果等价性基线；不使用新增的客户和轮次预过滤。"""
    observed = filters.as_of or now
    return FactSource.load(connection, observed, start=filters.start, end=filters.end,
        scope="r.source_key=%s AND r.account_id=%s AND r.started_at < %s",
        scope_params=[filters.source_key, filters.account_id, observed])


def prepare_trigger(case, trigger):
    if trigger == "pending":
        case.admin.execute("DELETE FROM ops.round_service_facts WHERE source_key=%s", (case.source,))
        assert case.admin.execute("""SELECT count(*) AS n FROM ops.round_fact_pending p
            JOIN ops.round_index r USING(round_id) WHERE r.source_key=%s""",
            (case.source,)).fetchone()["n"] == 3
        return START + timedelta(minutes=20)
    assert case.admin.execute("""SELECT count(*) AS n FROM ops.round_service_facts
        WHERE source_key=%s AND messages_until >= %s""",
        (case.source, START + timedelta(minutes=5))).fetchone()["n"] == 3
    assert case.admin.execute("""SELECT count(*) AS n FROM ops.round_fact_pending p
        JOIN ops.round_index r USING(round_id) WHERE r.source_key=%s""",
        (case.source,)).fetchone()["n"] == 0
    return START + timedelta(minutes=5)


def run_path(case, path, filters):
    if path == "history":
        return case.reader.customer_history(case.target_customer, filters=filters)
    if path == "directory":
        return case.reader.customer_rounds(case.target_customer, filters=filters)
    if path == "customers":
        return case.reader.list_customers(filters)
    if path == "rounds":
        return case.reader.list_rounds(filters)
    if path == "overview":
        result = case.reader.overview(filters)
        result.pop("generated_at")
        return result
    mode = "rounds" if path == "export_rounds" else "customers"
    scope = {"customer_id": case.target_customer} if path == "export_customer" else {}
    artifact = case.exporter.create_export(mode, filters, **scope)
    try:
        workbook = load_workbook(artifact.path)
        try:
            return [list(sheet.values) for sheet in workbook.worksheets]
        finally:
            workbook.close()
    finally:
        artifact.cleanup()


def assert_scope_equivalent(case, path, filters, expected_ids):
    # 基线完整计算三个轮次；新增实现必须保持全部展示字段与工作簿内容一致。
    with patch.object(repository, "load_facts", broad_facts), patch.object(export_module, "load_facts", broad_facts):
        expected = run_path(case, path, filters)
    loaded = []
    original = repository.load_facts

    def observed(*args, **kwargs):
        facts = original(*args, **kwargs)
        loaded.append(facts)
        return facts

    with patch.object(repository, "load_facts", observed), patch.object(export_module, "load_facts", observed):
        actual = run_path(case, path, filters)
    assert actual == expected
    assert loaded and all(set(facts.live_ids) == expected_ids for facts in loaded)
    if path in ("history", "directory", "customers", "rounds"):
        assert actual["total"] > 0
    elif path == "overview":
        assert actual["total_rounds"] == len(expected_ids)
    else:
        conversation_column = actual[0][0].index("会话ID")
        assert {row[conversation_column] for row in actual[0][1:]} == {"12", "32"}


@pytest.mark.parametrize("trigger", ["pending", "old_lock"])
@pytest.mark.parametrize("path,search_mismatch", [("history", False), ("directory", False),
    ("customers", False), ("export_customer", False), ("history", True),
    ("directory", True), ("export_customer", True)])
def test_customer_fact_scope_preserves_results(scope_archive, trigger, path, search_mismatch):
    case = scope_archive
    observed = prepare_trigger(case, trigger)
    filters = ArchiveFilters(source_key=case.source, account_id=1, agent_id="7", as_of=observed,
                             customer_query="其他客户" if search_mismatch else "目标客户")
    assert_scope_equivalent(case, path, filters, case.target_rounds)


@pytest.mark.parametrize("path", ["rounds", "overview", "export_rounds", "export_customers"])
def test_filter_export_and_round_queries_share_customer_scope(scope_archive, path):
    case = scope_archive
    observed = prepare_trigger(case, "pending")
    filters = ArchiveFilters(source_key=case.source, account_id=1, agent_id="7", as_of=observed,
                             customer_query="目标客户")
    assert_scope_equivalent(case, path, filters, case.target_rounds)


@pytest.mark.parametrize("extra,conversation", [({"channel_key": "unmapped"}, 12),
    ({"channel_key": "xingliao", "customer_query": "目标客户"}, 32),
    ({"round_no": 2}, 32), ({"conversation_id": 12}, 12), ({"inbox_id": 4}, 32),
    ({"reception_type": "human"}, None)])
def test_index_filters_bound_live_computation(scope_archive, extra, conversation):
    case = scope_archive
    observed = prepare_trigger(case, "pending")
    filters = replace(ArchiveFilters(source_key=case.source, account_id=1, agent_id="7", as_of=observed), **extra)
    expected = case.target_rounds if conversation is None else {
        row["round_id"] for row in case.rows("ops.round_index") if row["conversation_id"] == conversation}
    assert_scope_equivalent(case, "rounds", filters, expected)
