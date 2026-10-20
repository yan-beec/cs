"""Read-only API, authorization and optional-resource isolation checks."""

import asyncio
from contextlib import nullcontext
from datetime import datetime, timezone
import threading
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from ics_agent.app.core.auth import require_admin
from quality.query import routes
from quality.query.config import QuerySettings
from quality.query.repository import ArchiveFilters, QueryRepository, customer_conditions, round_conditions
from quality.query.runtime import ArchiveUnavailable, QueryRuntime


@pytest.fixture
def client(monkeypatch):
    runtime = Mock(enabled=True, ready=True, status="ready")
    runtime.call = AsyncMock(return_value={"items": [], "total": 0, "page": 1, "page_size": 30})
    monkeypatch.setattr(routes, "get_query_runtime", lambda: runtime)
    assignment = Mock(status="disabled", pending_count=None, blocked_count=None, counts_sampled_at=None)
    monkeypatch.setattr(routes, "get_assignment_sync_runtime", lambda: assignment)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_admin] = lambda: object()
    return TestClient(app), runtime, app


@pytest.mark.parametrize("endpoint", ["rounds", "customers", "overview"])
@pytest.mark.parametrize("verdicts", [
    {"ai_review_status": "passed"}, {"ai_review_status": "failed"},
    {"human_review_status": "passed"}, {"human_review_status": "failed"},
    {"ai_review_status": "failed", "human_review_status": "failed"},
    {"ai_review_status": "pending", "human_review_status": "passed"},
])
def test_review_filters_reach_lists_and_overview(client, endpoint, verdicts):
    browser, runtime, _ = client
    assert browser.get("/admin/api/quality/" + endpoint, params=verdicts).status_code == 200
    filters = runtime.call.call_args.args[1]
    assert filters.ai_review_status == verdicts.get("ai_review_status")
    assert filters.human_review_status == verdicts.get("human_review_status")


@pytest.mark.parametrize("endpoint", ["rounds", "customers", "overview", "export.xlsx"])
@pytest.mark.parametrize("field", ["ai_review_status", "human_review_status"])
def test_invalid_review_filters_never_reach_database(client, endpoint, field):
    browser, runtime, _ = client
    assert browser.get("/admin/api/quality/" + endpoint, params={field: "bad"}).status_code == 422
    runtime.call.assert_not_called()


@pytest.mark.parametrize("mode", ["rounds", "customers"])
def test_review_filters_are_shared_by_query_and_export_selection(mode):
    from quality.query.export import ExportRepository
    filters = ArchiveFilters(ai_review_status="failed", human_review_status="passed")
    where, values = (round_conditions if mode == "rounds" else customer_conditions)(filters)
    selection, export_values = ExportRepository._selection(mode, filters)
    assert where in selection and export_values == values == ["failed", "passed"]
    assert "review.ai_status = %s AND review.human_status = %s" in where
    assert "review.round_id=r.round_id" in where


def test_queries_reuse_existing_admin_auth(client):
    browser, runtime, app = client
    app.dependency_overrides.clear()
    for path in ("status", "filters", "overview", "presence", "presence/daily", "rounds", "customers", "unlinked-ratings", "rounds/id", "customers/id/history", "customers/id/rounds", "messages/1/versions?source_key=s&account_id=1", "events/1?source_key=s&account_id=1"):
        assert browser.get("/admin/api/quality/" + path).status_code == 401
    runtime.call.assert_not_called()


def test_filters_preserve_scope_and_beijing_time(client):
    browser, runtime, _ = client
    response = browser.get("/admin/api/quality/rounds", params={
        "source_key": "server-a", "account_id": 2, "agent_id": "9", "agent_scope": "assigned",
        "inbox_id": 3,
        "keyword": "原文", "channel_key": "unmapped", "time_field": "message_at",
        "start": "2026-10-16T08:00:00", "end": "2026-10-17T08:00:00+08:00",
        "rating_status": "pending", "has_attachment": "false", "page_size": 15,
    })
    assert response.status_code == 200
    method, filters = runtime.call.call_args.args
    assert method == "list_rounds"
    assert filters.source_key == "server-a" and filters.account_id == 2
    assert filters.inbox_id == 3
    assert filters.agent_scope == "assigned" and filters.has_attachment is False
    assert filters.start.astimezone(timezone.utc) == datetime(2026, 10, 16, tzinfo=timezone.utc)
    assert runtime.call.call_args.kwargs == {"page": 1, "page_size": 15}


def test_overview_uses_identical_filters_without_pagination_or_customer_history_expansion(client):
    browser, runtime, _ = client
    runtime.call.return_value = {"total_rounds": 105, "total_customers": 3, "average_rating": None,
        "participating_agents": [], "participating_agent_count": 0, "agents_truncated": False,
        "generated_at": "2026-10-16T00:00:00+00:00"}
    response = browser.get("/admin/api/quality/overview", params={
        "source_key": "server-a", "account_id": 2, "agent_id": "9", "agent_scope": "replied",
        "inbox_id": 3, "customer_query": " 客户 ", "keyword": " 原文 ", "channel_key": "unmapped",
        "time_field": "message_at", "start": "2026-10-16T08:00:00", "end": "2026-10-17T08:00:00+08:00",
        "reception_type": "human", "rating_status": "rated", "rating": 2, "message_scope": "contact",
        "has_attachment": "false", "conversation_id": 12, "round_no": 2, "status": "resolved",
        "page": 99, "page_size": 1,
    })
    assert response.status_code == 200 and response.json()["total_rounds"] == 105
    method, filters = runtime.call.call_args.args
    assert method == "overview" and runtime.call.call_args.kwargs == {}
    assert filters == ArchiveFilters(source_key="server-a", account_id=2, agent_id="9", agent_scope="replied",
        inbox_id=3, customer_query="客户", keyword="原文", channel_key="unmapped", time_field="message_at",
        start=datetime(2026, 10, 16, tzinfo=timezone.utc), end=datetime(2026, 10, 17, tzinfo=timezone.utc),
        reception_type="human", rating_status="rated", rating=2, message_scope="contact", has_attachment=False,
        conversation_id=12, round_no=2, status="resolved")


@pytest.mark.parametrize("params", [{"agent_id": "1"}, {"inbox_id": 3}, {"rating": 0},
    {"agent_scope": "bad"}, {"start": "2026-10-17", "end": "2026-10-16"}])
def test_overview_invalid_filters_never_reach_repository(client, params):
    browser, runtime, _ = client
    assert browser.get("/admin/api/quality/overview", params=params).status_code == 422
    runtime.call.assert_not_called()


def test_overview_unavailable_uses_existing_degraded_response(client):
    browser, runtime, _ = client
    runtime.call.side_effect = ArchiveUnavailable("private database details")
    response = browser.get("/admin/api/quality/overview")
    assert response.status_code == 503 and response.headers["Retry-After"] == "10"
    assert "private" not in response.text


@pytest.mark.parametrize("count", [0, 200, 201])
def test_overview_is_one_bounded_aggregate_statement_in_a_read_only_snapshot(count):
    connection = Mock()
    connection.transaction.return_value = nullcontext()
    connection.execute.return_value.fetchone.return_value = {
        "participating_agent_count": count, "participating_agents": [],
        "generated_at": datetime(2026, 10, 16, tzinfo=timezone.utc), "average_rating": None,
    }
    repository = QueryRepository.__new__(QueryRepository)
    repository.pool = Mock()
    repository.pool.connection.return_value = nullcontext(connection)
    filters = ArchiveFilters(source_key="server-a", account_id=2, customer_query="selected", keyword="proof")
    result = repository.overview(filters)
    assert result["agents_truncated"] is (count > 200)
    assert result["generated_at"] == "2026-10-16T00:00:00+00:00"
    calls = connection.execute.call_args_list
    assert len(calls) == 3
    assert calls[0].args[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    assert calls[1].args[0] == "SET LOCAL statement_timeout = '3000ms'"
    statement, values = calls[2].args
    assert "WITH selected AS MATERIALIZED" in statement and "LIMIT 200" in statement
    expected_where, expected_values = round_conditions(filters)
    assert expected_where in statement and values == expected_values
    assert "count(DISTINCT customer_id)" in statement
    assert "FROM ops.message_index m" in statement and "archive.message_snapshots" in statement
    assert "WHERE r.customer_id IN" not in statement
    connection.execute.return_value.fetchall.assert_not_called()


@pytest.mark.parametrize("params", [
    {"agent_id": "1"}, {"agent_id": "1", "account_id": "2"},
    {"inbox_id": 3}, {"inbox_id": 3, "source_key": "s"}, {"inbox_id": 3, "account_id": 1},
    {"inbox_id": 0},
    {"reception_type": "anything"}, {"time_field": "raw_body"}, {"rating": 0},
    {"start": "2026-10-17T08:00:00+08:00", "end": "2026-10-16T08:00:00+08:00"},
    {"page": 0}, {"page_size": 1001}, {"keyword": "x" * 201},
])
def test_invalid_filters_never_reach_database(client, params):
    browser, runtime, _ = client
    assert browser.get("/admin/api/quality/rounds", params=params).status_code == 422
    runtime.call.assert_not_called()


def test_history_is_complete_not_inherited_from_list_filters(client):
    browser, runtime, _ = client
    runtime.call.return_value = {"customer": {"customer_id": "customer-one"}, "messages": [], "total": 0}
    assert browser.get("/admin/api/quality/customers/customer-one/history", params={"keyword": "ignored", "reception_type": "human"}).status_code == 200
    runtime.call.assert_awaited_once_with("customer_history", "customer-one", page=1, page_size=100)


def test_customer_round_directory_is_paginated_and_not_limited_by_list_filters(client):
    browser, runtime, _ = client
    response = browser.get("/admin/api/quality/customers/customer-one/rounds",
        params={"page": 2, "page_size": 50, "keyword": "ignored", "start": "2026-10-16"})
    assert response.status_code == 200
    runtime.call.assert_awaited_once_with("customer_rounds", "customer-one", page=2, page_size=50)


@pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 101}, {"page_size": 0}])
def test_customer_round_directory_rejects_unbounded_pagination(client, params):
    browser, runtime, _ = client
    assert browser.get("/admin/api/quality/customers/id/rounds", params=params).status_code == 422
    runtime.call.assert_not_called()


def test_customer_round_directory_missing_customer_is_explicit(client):
    browser, runtime, _ = client
    runtime.call.return_value = None
    assert browser.get("/admin/api/quality/customers/missing/rounds").status_code == 404


def test_customer_round_directory_reads_only_selected_customers_round_metadata():
    connection = Mock()
    exists, count, page = Mock(), Mock(), Mock()
    exists.fetchone.return_value = {"customer_id": "selected"}
    count.fetchone.return_value = {"total": 60}
    page.fetchall.return_value = [{"round_id": "r51", "data": {"reception_type": "human"}}]
    connection.execute.side_effect = [exists, count, page]
    repository = QueryRepository.__new__(QueryRepository)
    repository.pool = Mock()
    repository.pool.connection.return_value = nullcontext(connection)
    result = repository.customer_rounds("selected", page=2, page_size=50)
    assert result["total"] == 60 and result["page"] == 2
    assert result["items"][0]["round_id"] == "r51"
    calls = connection.execute.call_args_list
    assert calls[0].args[1] == ("selected",) and calls[1].args[1] == ("selected",)
    statement, values = calls[2].args
    assert "WHERE r.customer_id=%s" in statement
    assert "ORDER BY r.started_at DESC,r.round_id DESC" in statement
    assert "LIMIT %s OFFSET %s" in statement and values == ("selected", 50, 50)
    assert "message_snapshots" not in statement and "message_index" not in statement


def test_customer_metrics_batch_only_page_customers_and_stream_message_metadata():
    connection = Mock()
    connection.execute.return_value.fetchall.return_value = [
        {"customer_id": "a", "round_id": "latest-a", "started_at": "2026-10-19T08:00:00Z",
         "last_message_at": "2026-10-19T08:03:00Z", "data": {"handoff_requested_at": "2026-10-19T08:01:00Z"},
         "review": {"ai_status": "failed", "human_status": "passed", "revision": "7"}},
        {"customer_id": "b", "round_id": "latest-b", "data": {}},
    ]
    cursor = Mock()
    cursor.fetchmany.side_effect = [[{"round_id": "latest-a", "sent_at": "2026-10-19T08:03:00Z",
                                    "sender_role": "agent", "direction": "outgoing"}], []]
    connection.cursor.return_value = nullcontext(cursor)
    customers = [{"customer_id": "a"}, {"customer_id": "b"}, {"customer_id": "no-round"}]
    QueryRepository._customer_service_metrics(connection, customers)
    assert connection.execute.call_args.args[1] == (["a", "b", "no-round"],)
    assert "DISTINCT ON (r.customer_id)" in connection.execute.call_args.args[0]
    assert cursor.execute.call_args.args[1] == (["latest-a", "latest-b"],)
    assert "content" not in cursor.execute.call_args.args[0]
    assert cursor.fetchmany.call_count == 2 and connection.cursor.call_count == 1
    assert customers[0]["latest_round"]["service_metrics"]["first_response_seconds"] == 120
    assert customers[0]["latest_round"]["review"] == {"ai_status": "failed", "human_status": "passed", "revision": "7"}
    assert customers[2]["latest_round"] is None


def test_versions_require_full_message_identity(client):
    browser, runtime, _ = client
    assert browser.get("/admin/api/quality/messages/42/versions").status_code == 422
    assert browser.get("/admin/api/quality/messages/42/versions?source_key=other&account_id=7&page=2").status_code == 200
    runtime.call.assert_awaited_once_with("message_versions", "other", 7, 42, page=2, page_size=100)


def test_unlinked_ratings_have_an_independent_paginated_entry(client):
    browser, runtime, _ = client
    assert browser.get("/admin/api/quality/unlinked-ratings", params={"keyword": "ignored", "page": 2}).status_code == 200
    runtime.call.assert_awaited_once_with("unlinked_ratings", page=2, page_size=30)


def test_evidence_download_preserves_exact_original_bytes(client):
    browser, runtime, _ = client
    raw = b'{ "event":"message_created", "precise":12345678901234567890.1234567890 }\n'
    runtime.call.return_value = raw
    response = browser.get("/admin/api/quality/events/7?source_key=source-a&account_id=1")
    assert response.status_code == 200 and response.content == raw
    assert "attachment" in response.headers["Content-Disposition"]
    assert response.headers["Cache-Control"] == "no-store"
    runtime.call.assert_awaited_once_with("raw_event", 7, "source-a", 1)


def test_missing_round_and_unavailable_archive_are_explicit(client):
    browser, runtime, _ = client
    runtime.call.return_value = None
    assert browser.get("/admin/api/quality/rounds/missing").status_code == 404
    runtime.call.side_effect = ArchiveUnavailable("sensitive SQL parameters")
    response = browser.get("/admin/api/quality/rounds")
    assert response.status_code == 503
    assert "sensitive" not in response.text
    assert response.headers["Retry-After"] == "10"


def test_status_is_separate_from_original_health(client):
    browser, runtime, app = client

    @app.get("/health")
    def original_health():
        return {"status": "ok"}

    runtime.ready = False
    runtime.status = "unavailable"
    result = browser.get("/admin/api/quality/status").json()
    assert result == {"enabled": True, "ready": False, "status": "unavailable", "assignment_sync_status": "disabled",
        "assignment_sync_pending_count": None, "assignment_sync_blocked_count": None,
        "assignment_sync_counts_sampled_at": None}
    assert browser.get("/health").json() == {"status": "ok"}
    runtime.call.assert_not_called()


def test_assignment_sync_degradation_does_not_disable_archive_queries(client, monkeypatch):
    browser, runtime, _ = client
    runtime.call.return_value = {"jobs": {"queued": 0}}
    assignment = Mock(status="degraded", pending_count=2, blocked_count=1,
        counts_sampled_at="2026-10-16T00:00:00+00:00")
    monkeypatch.setattr(routes, "get_assignment_sync_runtime", lambda: assignment)
    response = browser.get("/admin/api/quality/status")
    assert response.status_code == 200
    result = response.json()
    assert result["ready"] is True
    assert result["assignment_sync_status"] == "degraded"
    assert result["assignment_sync_pending_count"] == 2
    assert result["assignment_sync_blocked_count"] == 1
    assert result["assignment_sync_counts_sampled_at"] == assignment.counts_sampled_at


def test_keyword_searches_immutable_versions_and_escapes_literal_wildcards():
    where, values = round_conditions(ArchiveFilters(keyword="100%_literal'", source_key="s", account_id=1))
    assert "archive.message_snapshots" in where
    assert "s.source_key=m.source_key AND s.account_id=m.account_id" in where
    assert "100%" not in where
    assert values == ["s", 1, "%100\\%\\_literal'%"]


def test_seat_search_and_unknown_channels_have_precise_semantics():
    where, values = round_conditions(ArchiveFilters(source_key="s", account_id=1, agent_id="7", channel_key="unmapped"))
    assert "r.data ->> 'responsible_agent_id' = %s" in where and "r.channel_key IS NULL" in where
    assert "r.data ->> 'responsibility_basis' = 'ics_assignment'" in where
    assert "a.replied" not in where and "current_assignee_id" not in where
    assert values == ["s", 1, "7"]
    with pytest.raises(ValueError):
        round_conditions(ArchiveFilters(agent_id="7"))


@pytest.mark.parametrize("endpoint", ["rounds", "customers", "export.xlsx"])
def test_agent_filters_default_to_round_responsibility(client, endpoint):
    browser, runtime, _ = client
    if endpoint == "export.xlsx":
        runtime.export = AsyncMock(side_effect=ArchiveUnavailable())
    response = browser.get("/admin/api/quality/" + endpoint, params={"source_key": "s", "account_id": 1, "agent_id": "7"})
    assert response.status_code == (503 if endpoint == "export.xlsx" else 200)
    filters = runtime.export.call_args.args[1] if endpoint == "export.xlsx" else runtime.call.call_args.args[1]
    assert filters.agent_scope == "responsible"


@pytest.mark.parametrize("builder", [round_conditions, customer_conditions])
def test_responsibility_filter_is_scoped_to_the_same_round_as_other_conditions(builder):
    where, values = builder(ArchiveFilters(source_key="s", account_id=1, agent_id="7", round_no=2, keyword="proof"))
    assert "r.data ->> 'responsible_agent_id' = %s" in where
    assert "r.round_no = %s" in where and "m.round_id=r.round_id" in where
    assert "current_assignee_id" not in where and "round_agent_index" not in where
    assert where.count("%s") == len(values)


@pytest.mark.parametrize("builder", [round_conditions, customer_conditions])
@pytest.mark.parametrize("reception_type", ["ai_only", "human", "unknown"])
def test_reception_filters_use_current_classification_without_completion_gate(builder, reception_type):
    where, values = builder(ArchiveFilters(reception_type=reception_type))
    assert "r.reception_type = %s" in where
    assert "classification_finalized" not in where
    assert values == [reception_type]


@pytest.mark.parametrize("endpoint", ["rounds", "customers"])
def test_active_ai_filter_is_forwarded_for_rounds_and_customers(client, endpoint):
    browser, runtime, _ = client
    assert browser.get("/admin/api/quality/" + endpoint, params={"reception_type": "ai_only", "status": "active"}).status_code == 200
    method, filters = runtime.call.call_args.args
    assert method == "list_" + endpoint
    assert filters.reception_type == "ai_only" and filters.status == "active"


@pytest.mark.parametrize("builder", [round_conditions, customer_conditions])
@pytest.mark.parametrize("scope", [{}, {"source_key": "s"}, {"account_id": 1}])
def test_inbox_ids_require_source_and_account_even_outside_http(builder, scope):
    with pytest.raises(ValueError, match="source_key and account_id"):
        builder(ArchiveFilters(inbox_id=3, **scope))


def test_attachment_absence_repeats_the_exact_eligible_customer_message_scope():
    start = datetime(2026, 10, 16, tzinfo=timezone.utc)
    where, values = customer_conditions(ArchiveFilters(
        source_key="s", account_id=1, inbox_id=3, conversation_id=12,
        keyword="proof", message_scope="agent", has_attachment=False,
        time_field="message_at", start=start,
    ))
    assert where.count("m.conversation_id=%s") == 2
    assert where.count("m.sender_role=%s AND NOT m.is_private") == 2
    assert where.count("m.sent_at >= %s") == 2
    assert where.count("m.data ->> 'inbox_id'") == 2
    assert "NOT EXISTS" in where
    assert values == ["s", 1, 12, "3", "%proof%", "agent", start, 12, "3", "%proof%", "agent", start]
    assert where.count("%s") == len(values)


def test_round_attachment_presence_uses_one_matching_message():
    where, values = round_conditions(ArchiveFilters(keyword="proof", message_scope="private", has_attachment=True))
    assert where.count("SELECT 1 FROM ops.message_index m") == 1
    assert "m.is_private" in where and "m.data -> 'attachments'" in where
    assert values == ["%proof%"]


@pytest.mark.parametrize("builder", [round_conditions, customer_conditions])
@pytest.mark.parametrize("attachment", [None, True, False])
def test_filter_parameter_counts_remain_valid_for_both_query_and_export(builder, attachment):
    where, values = builder(ArchiveFilters(
        source_key="s", account_id=1, inbox_id=3, customer_query="name", conversation_id=12,
        round_no=2, reception_type="human", agent_id="7", agent_scope="current", channel_key="xingliao",
        keyword="proof", message_scope="private", has_attachment=attachment,
        time_field="message_at", start=datetime(2026, 10, 16, tzinfo=timezone.utc),
    ))
    assert "r.inbox_id = %s" in where
    assert "r.round_no = %s" in where
    assert "ci.source_key=r.source_key AND ci.account_id=r.account_id AND ci.conversation_id=r.conversation_id" in where
    assert "ci.current_assignee_id=%s" in where
    assert where.count("%s") == len(values)


def test_query_pool_uses_reader_and_read_only_timeouts():
    settings = QuerySettings(_env_file=None, reader_password="test-reader-password")
    assert settings.connection_kwargs["user"] == "archive_reader"
    assert "default_transaction_read_only=on" in settings.connection_kwargs["options"]
    assert settings.connection_kwargs["application_name"] == "ics-quality-query"


def test_disabled_query_runtime_never_creates_database_resources():
    async def check():
        runtime = QueryRuntime()
        with patch("quality.query.runtime.QuerySettings", return_value=QuerySettings(_env_file=None, query_enabled=False)), patch("quality.query.runtime.QueryRepository") as repository:
            await runtime.start()
            assert runtime.status == "disabled" and runtime.executor is None
            repository.assert_not_called()
            await runtime.close()
    asyncio.run(check())


def test_bad_reader_configuration_does_not_fail_application_start():
    async def check():
        runtime = QueryRuntime()
        with patch("quality.query.runtime.QuerySettings", return_value=QuerySettings(_env_file=None, query_enabled=True, reader_password="")):
            await runtime.start()
            assert runtime.status == "configuration_error" and not runtime.ready
            assert runtime.executor is None
            await runtime.close()
    asyncio.run(check())


def test_archive_connection_failure_recovers_independently():
    async def check():
        runtime = QueryRuntime()
        attempted = asyncio.Event()
        loop = asyncio.get_running_loop()
        repository = Mock()

        def fail():
            loop.call_soon_threadsafe(attempted.set)
            raise OSError("connection unavailable")

        repository.open.side_effect = fail
        with patch("quality.query.runtime.QuerySettings", return_value=QuerySettings(_env_file=None, query_enabled=True, reader_password="test-reader")), patch("quality.query.runtime.QueryRepository", return_value=repository):
            await runtime.start()
            await asyncio.wait_for(attempted.wait(), timeout=2)

            async def recovery_observed():
                while runtime.status == "connecting":
                    await asyncio.sleep(0)

            await asyncio.wait_for(recovery_observed(), timeout=2)
            assert not runtime.ready and runtime.status == "unavailable"
            with pytest.raises(ArchiveUnavailable):
                await runtime.call("list_rounds", ArchiveFilters())
            await runtime.close()
            repository.close.assert_called_once()
    asyncio.run(check())


def test_cancelled_query_retains_its_slot_until_the_database_thread_finishes():
    async def check():
        runtime = QueryRuntime()
        started, released = threading.Event(), threading.Event()
        with patch("quality.query.runtime.QuerySettings", return_value=QuerySettings(_env_file=None, query_enabled=True, reader_password="test-reader")):
            # Allocate the same dedicated resources without running a recovery query.
            with patch.object(runtime, "_recover", new=AsyncMock()):
                await runtime.start()
            runtime.slots = asyncio.Semaphore(1)
            def blocked_query():
                started.set()
                released.wait(timeout=3)
            task = asyncio.create_task(runtime._execute(blocked_query))
            try:
                await asyncio.to_thread(started.wait, 2)
                assert started.is_set()
                task.cancel()
                with pytest.raises(ArchiveUnavailable):
                    await runtime._execute(lambda: None)
                assert not task.done()
            finally:
                released.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
                await runtime.close()
    asyncio.run(check())


def test_background_probe_does_not_mark_a_busy_query_pool_unavailable():
    async def check():
        runtime = QueryRuntime()
        runtime.repository = Mock()
        runtime.ready, runtime.status = True, "ready"
        attempted = asyncio.Event()
        async def busy(*args):
            attempted.set()
            raise ArchiveUnavailable("capacity busy")
        with patch.object(runtime, "_execute", side_effect=busy):
            task = asyncio.create_task(runtime._recover())
            try:
                await asyncio.wait_for(attempted.wait(), timeout=2)
                assert runtime.ready and runtime.status == "ready"
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(check())
