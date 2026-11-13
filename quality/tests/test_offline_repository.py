"""离线提取沿用页面筛选、轮次评价与统计事实，月份按结束时间归属。"""
from quality.offline.repository import scope_predicate


def test_month_selects_whole_rounds_by_end_time_and_year_rollover():
    sql, bounds = scope_predicate({"mode": "month", "month": "2026-12"})
    assert "m.round_id IN (SELECT r.round_id" in sql
    assert "COALESCE(r.data ->> 'resolved_at', r.data ->> 'closed_at') IS NOT NULL" in sql
    assert "r.last_message_at >= %s" in sql
    assert "m.round_id IS NULL AND (m.sent_at >= %s AND m.sent_at < %s) IS TRUE" in sql
    assert [bound.isoformat() for bound in bounds[:2]] == ["2026-12-01T00:00:00+08:00", "2027-01-01T00:00:00+08:00"]
    assert bounds == bounds[:2] * 3


def test_full_archive_has_no_implicit_filters_or_round_requirement():
    assert scope_predicate({"mode": "all", "filters": {"keyword": "ignored"}}) == ("TRUE", [])


def test_customer_view_includes_only_matching_unrounded_messages_and_outer_lock():
    request = {"mode": "rounds", "view": "customers", "summary": "展示摘要",
               "filters": {"keyword": "退款", "time_field": "message_at", "as_of": "2026-11-12T10:00:00+08:00",
                           "source_key": "local", "account_id": 1, "customer_query": "访客"}}
    sql, values = scope_predicate(request)
    assert "OR (" in sql and "m.round_id IS NULL" in sql
    assert "m.customer_id IN (SELECT c.customer_id" in sql
    assert sql.endswith(") AND (m.sent_at IS NULL OR m.sent_at < %s)")
    assert "展示摘要" not in str(values)
    request.pop("view")
    assert "m.round_id IS NULL" not in scope_predicate(request)[0]


def test_default_round_time_filters_exclude_unrounded_even_in_customer_view():
    sql, _ = scope_predicate({"mode": "rounds", "view": "customers",
                              "filters": {"start": "2026-11-01T00:00:00+08:00"}})
    assert "m.round_id IS NULL" not in sql


def test_extracted_query_uses_round_ratings_current_reviews_and_complete_metrics():
    from quality.offline.repository import FIELDS, message_statement
    sql = message_statement("TRUE")
    for field in ("round_rating_status", "round_rating", "round_rating_feedback", "closed_at",
                  "resolved_at", "last_message_at", "responsible_assigned_at", "valid_content",
                  "assigned_agent_id", "responsible_agent_name", "response_samples", "snapshot_at"):
        assert field in FIELDS
        if field != "snapshot_at":
            assert field in sql
    assert "quality.round_review_current review" in sql
    assert "rating.round_id=m.round_id" in sql
    assert "rating_status='rated'" in sql
    assert "NOT m.is_deleted" in sql
    assert "FROM (SELECT m.* FROM ops.message_index m WHERE TRUE OFFSET 0) m" in sql


def test_export_row_keeps_fact_responsibility_and_metrics_without_message_data_override():
    from datetime import datetime, timezone
    from quality.offline.repository import export_row
    row = {"message_id": 4, "round_id": "r", "reception_type": "human", "responsibility_basis": "assignment_unobserved",
           "responsible_agent_id": 8, "responsible_agent_name": "事实坐席", "closed_at": "2026-11-12T10:00:00Z",
           "snapshot_at": datetime(2026, 11, 12, tzinfo=timezone.utc), "valid_content": False,
           "round_rating": None, "data": {"round_rating": 1, "attachments": []}}
    result = export_row(row)
    assert result["responsible_agent_id"] == "8"
    assert result["responsible_agent_name"] == "事实坐席"
    assert result["closed_at"] == row["closed_at"]
    assert result["valid_content"] is False
    assert result["round_rating"] is None
    assert result["snapshot_at"] == "2026-11-12T00:00:00+00:00"
    assert export_row({"round_id": "legacy", "reception_type": "human", "responsibility_basis": None})[
        "responsibility_basis"] == "assignment_unobserved"


def test_fact_names_use_scoped_directory_then_archived_name_then_id():
    from quality.offline.repository import export_row
    row = {"source_key": "local", "account_id": 1, "assigned_agent_id": 8, "assigned_agent_name": "旧姓名",
           "responsible_agent_id": 9, "responsible_agent_name": "归档姓名"}
    names = {("local", "1", "8"): "目录姓名", ("other", "1", "9"): "其他来源姓名"}
    result = export_row(row, names)
    assert result["assigned_agent_name"] == "目录姓名"
    assert result["responsible_agent_name"] == "归档姓名"
    row["responsible_agent_name"] = "分配坐席9"
    assert export_row(row, names)["responsible_agent_name"] == "Chatwoot 坐席 ID 9 · 名称未同步"


def test_messages_extracts_scoped_facts_and_snapshot_before_connection_closes(monkeypatch):
    from contextlib import contextmanager
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from quality.offline import repository as module

    now = datetime(2026, 11, 12, tzinfo=timezone.utc)
    locked = "2026-11-11T00:00:00+08:00"
    observations = {}

    class Connection:
        def execute(self, sql, parameters=None):
            assert sql.startswith("SET ") or sql == "SELECT now() AS now"
            return SimpleNamespace(fetchone=lambda: {"now": now})

        def fetchmany(self, size):
            if observations.get("fetched"):
                return []
            observations["fetched"] = True
            return [{**dict.fromkeys(module.SNAPSHOT_FIELDS), "message_id": 1, "round_id": "chosen", "data": {}}]

    connection = Connection()

    @contextmanager
    def connect():
        yield connection
        observations["connection_closed"] = True

    @contextmanager
    def cursor(name):
        def execute(sql, parameters):
            observations.update(sql=sql, parameters=parameters)
        yield SimpleNamespace(execute=execute, fetchmany=connection.fetchmany)

    connection.cursor = cursor
    repository = SimpleNamespace(open=lambda: None, close=lambda: observations.update(repository_closed=True),
                                 pool=SimpleNamespace(connection=connect))
    original_selection = module.ExportRepository._selection
    class Repository:
        _selection = staticmethod(original_selection)

        def __new__(cls, settings):
            return repository

    monkeypatch.setattr(module, "ExportRepository", Repository)
    monkeypatch.setattr(module, "_agent_names", lambda: {})
    monkeypatch.setattr(module, "casefold_mapping", lambda conn: {"65": "a"})
    def versions(conn, where, parameters, check):
        assert conn is connection
        assert not observations.get("connection_closed")
        observations["versions_in_snapshot"] = True
        return iter(())
    monkeypatch.setattr(module, "version_rows", versions)

    class Facts:
        @staticmethod
        def load(conn, observed_at, **scope):
            assert conn is connection
            observations.update(observed_at=observed_at, scope=scope)
            return Facts()

        def facts(self, where="TRUE", params=()):
            observations.update(fact_where=where, fact_params=params)
            return "(SELECT * FROM scoped_facts WHERE tag=%s) f", ["fact-placeholder"]

    monkeypatch.setattr(module, "FactSource", Facts)
    metadata = {}
    rows = list(module.messages({"mode": "rounds", "filters": {"as_of": locked}}, lambda: None, metadata=metadata))
    assert metadata == {"snapshot_at": now.isoformat(), "filter_contract_version": 1, "fact_version": module.FACT_VERSION, "casefold_map": {"65": "a"}}
    assert rows[0]["snapshot_at"] == now.isoformat()
    assert observations["scope"]["round_filter"].startswith("r.round_id IN (SELECT r.round_id")
    assert observations["fact_where"].startswith("f.round_id IN (SELECT r.round_id")
    assert observations["fact_params"] == observations["scope"]["round_params"]
    assert observations["observed_at"].isoformat() == locked
    assert "LEFT JOIN (SELECT * FROM scoped_facts WHERE tag=%s) f" in observations["sql"]
    assert "fact-placeholder" in observations["parameters"]
    assert observations["sql"].count("%s") == len(observations["parameters"])
    assert observations["connection_closed"] and observations["repository_closed"]
    assert observations["versions_in_snapshot"]
