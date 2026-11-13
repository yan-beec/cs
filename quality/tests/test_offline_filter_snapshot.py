"""核对筛选契约和快照字段穿过单文件白名单，尚不验收 C2 对照。"""
import json
import re
from dataclasses import fields

from quality.query.repository import ArchiveFilters
from quality.tests.test_offline_bundle import build, message, parse


def node_json_roundtrip(value):
    """让真实 Node 解析最终 JSON，捕获 Python 内比较无法发现的整数精度损失。"""
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("需要 Node 验证 JSON ID 精度")
    result = subprocess.run([node, "-e", "process.stdout.write(JSON.stringify(JSON.parse(require('node:fs').readFileSync(0,'utf8'))))"],
                            input=json.dumps(value), capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_fixed_context_and_review_ids_survive_node_json_boundary():
    from quality.query.filter_contract import filter_contract
    from quality.offline.filter_snapshot import safe_rating_review
    for identity in (2**53 + 1, 2**63 - 1):
        context = {"source_key": "one", "account_id": identity, "metric": "lost", "metric_seat": identity,
                   "has_attachment": False}
        contract = filter_contract(context)
        review = safe_rating_review({"round_id": identity, "revision": identity,
                                     "selected_rating": {"invitation_message_id": identity,
                                                         "external_round_id": identity, "rating": 4}})
        decoded = node_json_roundtrip({"contract": contract, "review": review})
        assert decoded["contract"]["fixed_context"]["account_id"] == str(identity)
        assert decoded["contract"]["fixed_context"]["metric_seat"] == str(identity)
        assert decoded["contract"]["fixed_context"]["has_attachment"] is False
        assert decoded["review"]["round_id"] == str(identity)
        assert decoded["review"]["revision"] == str(identity)
        assert decoded["review"]["selected_rating"]["invitation_message_id"] == str(identity)
        assert decoded["review"]["selected_rating"]["external_round_id"] == str(identity)
        assert decoded["review"]["selected_rating"]["rating"] == 4
        assert context["account_id"] == identity


def test_bundle_normalizes_all_identity_boundaries_before_node_reads_them(tmp_path):
    from quality.offline.bundle import build_bundle
    from quality.offline.media import MediaDownloader
    from quality.tests.test_offline_bundle import settings
    identity = 2**63 - 1
    row = message(identity, source_key="one", account_id=identity, inbox_id=identity,
                  conversation_id=identity, customer_id=identity, round_id=identity, sender_id=identity,
                  responsible_agent_id=identity, assigned_agent_id=identity, current_assignee_id=identity,
                  replied_agent_ids=[identity], assigned_agent_ids=[identity], round_no=2, round_rating=4,
                  round_rating_status="rated", rating_review={"selected_rating": {"invitation_message_id": identity}})
    scope = {"mode": "rounds", "filters": {"account_id": identity, "inbox_id": identity,
             "conversation_id": identity, "agent_id": identity, "round_no": 2, "rating": 4,
             "metric_seat": "", "has_attachment": False}}
    path = build_bundle(tmp_path / "work", [row, {"_type": "search_version", "source_key": "one",
                       "account_id": identity, "message_id": identity, "version": str(identity),
                       "offset": 0, "content": "旧正文", "last": True}], settings(tmp_path), MediaDownloader([]),
                       lambda **v: None, lambda: None, scope=scope)
    page, catalog, groups, _ = parse(path)
    options = json.loads(re.search(r'id="filter-options">(.*?)</script>', page).group(1))
    version = json.loads(re.search(r'id="v0">(.*?)</script>', page).group(1))
    data = node_json_roundtrip({"catalog": catalog, "message": groups[0][0], "options": options, "version": version})
    catalog = data["catalog"]
    filters = catalog["meta"]["scope"]["filters"]
    for key in ("account_id", "inbox_id", "conversation_id", "agent_id"):
        assert filters[key] == str(identity)
    assert filters["rating"] == 4 and filters["round_no"] == 2
    assert filters["metric_seat"] == "" and filters["has_attachment"] is False
    assert catalog["customers"][0]["id"] == str(identity)
    entry = catalog["entries"][0]
    for key in ("account_id", "inbox_id", "conversation_id", "round_id", "responsible_agent_id", "assigned_agent_id", "current_assignee_id"):
        assert entry[key] == str(identity)
    assert entry["replied_agent_ids"] == [str(identity)]
    assert entry["assigned_agent_ids"] == [str(identity)]
    assert data["message"]["message_id"] == str(identity)
    assert data["message"]["sender_id"] == str(identity)
    assert entry["rating_review"]["selected_rating"]["invitation_message_id"] == data["message"]["message_id"]
    assert data["options"]["agents"][0]["account_id"] == str(identity)
    assert data["version"]["message_id"] == str(identity)
    assert data["version"]["account_id"] == str(identity)
    assert scope["filters"]["account_id"] == identity


def test_contract_covers_parameters_and_preserves_empty_metric_seat():
    from quality.query.filter_contract import filter_contract
    contract = filter_contract({"metric": "lost", "metric_seat": "", "has_attachment": False})
    assert set(contract["parameters"]) == {field.name for field in fields(ArchiveFilters)}
    assert contract["fixed_context"]["metric_seat"] == ""
    assert contract["fixed_context"]["has_attachment"] is False
    assert contract["parameters"]["agent_scope"]["default"] == "responsible"
    assert len(contract["parameters"]["time_field"]["options"]) == 7
    window = filter_contract({"metric": "lost", "start": "a", "end": "b"})
    assert window["fixed_context"]["start"] == "a"
    assert window["parameters"]["start"]["fixed"] is True


def test_snapshot_fields_survive_bundle_and_sensitive_nested_fields_do_not(tmp_path):
    row = message(1, source_key="one", account_id="9007199254740993", inbox_id="8", channel_key="a",
                  business_user_id="biz", blacklist_ever=True, blacklist_active=False,
                  replied_agent_ids=["8", "9"], assigned_agent_ids=["7"], current_assignee_id="9",
                  rating_at="2026-10-01T00:00:00Z", filter_handoff_at=None, assigned_at=None,
                  is_human_reply=False, has_original_attachment=True, visitor_messages=6,
                  is_ai_only=False, is_handoff=True, busy_refused=False,
                  rating_review={"status": "invalid", "secret": "SENSITIVE"})
    page, catalog, rounds, _ = parse(build(tmp_path, [row]))
    assert catalog["meta"]["version"] == 3
    assert catalog["meta"]["filter_contract_version"] == 1
    assert catalog["meta"]["fact_version"] > 0
    assert catalog["customers"][0]["business_user_id"] == "biz"
    assert catalog["customers"][0]["blacklist_active"] is False
    entry = catalog["entries"][0]
    assert entry["replied_agent_ids"] == ["8", "9"]
    assert entry["assigned_agent_ids"] == ["7"]
    assert entry["visitor_messages"] == 6
    assert entry["account_id"] == "9007199254740993"
    assert rounds[0][0]["has_original_attachment"] is True
    assert rounds[0][0]["is_human_reply"] is False
    assert "SENSITIVE" not in page


def test_version_chunks_are_budgeted_without_inflating_messages(tmp_path):
    from quality.offline.bundle import BundleLimit
    import pytest
    version = {"_type": "search_version", "source_key": "one", "account_id": "1", "message_id": "1",
               "version": 0, "offset": 0, "content": "旧正文", "last": True}
    path = build(tmp_path, [message(1), version])
    page, catalog, _, _ = parse(path)
    assert catalog["meta"]["messages"] == 1
    assert catalog["meta"]["search_version_parts"] == 1
    assert json.loads(re.search(r'id="v0">(.*?)</script>', page).group(1))["content"] == "旧正文"
    with pytest.raises(BundleLimit):
        build(tmp_path / "limit", [message(1), {**version, "content": "x" * 5000}], max_bytes=1024)
    assert not (tmp_path / "limit/work/archive.html").exists()


def test_snapshot_query_uses_authoritative_sources_and_chunked_observations():
    from quality.offline.filter_snapshot import snapshot_columns, version_statement
    sql = snapshot_columns()
    for fragment in ("a.replied", "a.was_assigned", "current_assignee_id", "business_user_id",
                     "contact_block_state", "is_human_reply", "has_original_attachment", "visitor_messages"):
        assert fragment in sql
    versions = version_statement("m.customer_id=%s")
    assert "message_snapshots" in versions and "substring" in versions
    assert "GROUP BY" in versions and "generate_series" in versions
    assert "observed_at <" not in versions


def test_missing_filter_projection_fails_instead_of_silently_exporting():
    import pytest
    from quality.offline.filter_snapshot import validate_projection
    with pytest.raises(ValueError, match="replied_agent_ids"):
        validate_projection({})


def test_contract_options_match_current_admin_form():
    from pathlib import Path
    from quality.query.filter_contract import OPTIONS
    page = (Path(__file__).parents[2] / "ics_agent/app/admin.html").read_text(encoding="utf-8")
    for name in ("agent_scope", "time_field", "message_scope", "outcome", "dialogue", "blacklist",
                 "reception_type", "ai_review_status", "human_review_status", "rating_status"):
        select = re.search(r'<select[^>]*name="' + name + r'"[^>]*>(.*?)</select>', page, re.S).group(1)
        assert re.findall(r'<option value="([^"]+)"', select) == OPTIONS[name]


def test_contract_api_options_match_route_literals():
    from typing import get_args, get_origin, get_type_hints, Literal
    from quality.query.routes import archive_filters
    from quality.query.filter_contract import filter_contract
    parameters = filter_contract()["parameters"]
    for name, annotation in get_type_hints(archive_filters).items():
        candidates = (annotation, *get_args(annotation))
        literal = next((item for item in candidates if get_origin(item) is Literal), None)
        if literal is not None:
            assert parameters[name]["api_options"] == list(get_args(literal))


def test_composite_option_dictionary_includes_all_agent_sources(tmp_path):
    rows = [message(i, source_key=source, account_id="1", inbox_id="3", responsible_agent_id="7",
                    assigned_agent_id="8", replied_agent_ids=["9"], assigned_agent_ids=["10"],
                    current_assignee_id="11") for i, source in enumerate(("one", "two"))]
    page, catalog, _, _ = parse(build(tmp_path, rows))
    assert catalog["meta"]["options_block"] == "filter-options"
    options = json.loads(re.search(r'id="filter-options">(.*?)</script>', page).group(1))
    assert len(options["agents"]) == 10
    assert {item["source_key"] for item in options["inboxes"]} == {"one", "two"}


def test_version_cursor_batches_and_closes_on_cancellation():
    from contextlib import contextmanager
    from quality.offline.filter_snapshot import version_rows
    import pytest
    calls = []
    class Cursor:
        def execute(self, sql, parameters):
            calls.append(parameters)
        def fetchmany(self, size):
            assert size == 32
            return [{"source_key": "one", "account_id": 1, "message_id": 2, "version": 3,
                     "offset": 0, "content": "x", "last": True}]
    class Connection:
        @contextmanager
        def cursor(self, name):
            try:
                yield Cursor()
            finally:
                calls.append("closed")
    reads = 0
    def check():
        nonlocal reads
        reads += 1
        if reads > 3:
            raise RuntimeError("cancelled")
    with pytest.raises(RuntimeError, match="cancelled"):
        list(version_rows(Connection(), "m.source_key=%s", ["one"], check))
    assert calls == [["one"], "closed"]


def test_reader_can_parse_export_sql_in_isolated_database():
    import os
    import pytest
    if os.environ.get("PROCESSING_TEST_DATABASE") != "1":
        pytest.skip("需要隔离库环境")
    assert os.environ["ARCHIVE_POSTGRES_DB"].startswith("quality_query_test_")
    import psycopg
    from quality.query.config import QuerySettings
    from quality.offline.repository import message_statement
    from quality.offline.filter_snapshot import version_statement
    with psycopg.connect(**QuerySettings(_env_file=None).connection_kwargs) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        connection.execute(message_statement("FALSE") + " LIMIT 0")
        connection.execute(version_statement("FALSE") + " LIMIT 0")
        # 只读 CTE 数据验证数据库切块和同逻辑消息去重，不创建事件、不驱动作业。
        sql = version_statement("TRUE").replace("ops.message_index", "test_messages").replace("archive.message_snapshots", "test_snapshots")
        sql = sql.replace("WITH versions AS", """WITH test_messages AS (
            SELECT 'c1-readonly'::text AS source_key,1::bigint AS account_id,1::bigint AS message_id
        ), test_snapshots AS (
            SELECT 'c1-readonly'::text AS source_key,1::bigint AS account_id,1::bigint AS message_id,
                   n AS id,repeat('旧',70000) AS content FROM generate_series(1,65) n
        ), versions AS""")
        rows = connection.execute(sql).fetchall()
        assert len(rows) == 2
        assert rows[0][-2] + rows[1][-2] == "旧" * 70000
        assert (rows[0][-1], rows[1][-1]) == (False, True)
        from quality.offline.repository import scope_predicate
        for month, expected in (("2026-10", {1, 2}), ("2026-11", {3, 4})):
            where, parameters = scope_predicate({"mode": "month", "month": month})
            where = where.replace("ops.round_index", "test_rounds")
            sql = """WITH test_rounds(round_id,data,last_message_at) AS (VALUES
                ('old','{"closed_at":"2026-10-01T00:00:00+08:00"}'::jsonb,'2026-10-01T00:00:00+08:00'::timestamptz),
                ('new','{"resolved_at":"2026-11-01T00:00:00+08:00"}'::jsonb,'2026-11-01T00:00:00+08:00'::timestamptz)
            ), test_messages(message_id,round_id,sent_at) AS (VALUES
                (1,'old','2026-09-01T00:00:00+08:00'::timestamptz),(2,'old','2026-10-01T00:00:00+08:00'::timestamptz),
                (3,'new','2026-10-31T00:00:00+08:00'::timestamptz),(4,'new','2026-11-01T00:00:00+08:00'::timestamptz)
            ) SELECT m.message_id FROM test_messages m WHERE """ + where
            assert {row[0] for row in connection.execute(sql, parameters)} == expected


def test_cancel_while_extracting_versions_closes_source_and_removes_artifact(tmp_path):
    import pytest
    from quality.offline.bundle import build_bundle
    from quality.tests.test_offline_bundle import settings
    from quality.offline.media import MediaDownloader
    closed = []
    def rows():
        try:
            yield message(1)
            yield {"_type": "search_version", "content": "x"}
            raise RuntimeError("cancelled")
        finally:
            closed.append(True)
    with pytest.raises(RuntimeError, match="cancelled"):
        build_bundle(tmp_path / "work", rows(), settings(tmp_path), MediaDownloader([]), lambda **v: None, lambda: None)
    assert closed == [True]
    assert not (tmp_path / "work/archive.html").exists()
