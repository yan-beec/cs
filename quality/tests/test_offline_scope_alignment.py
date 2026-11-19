"""在隔离数据库中核对页面、预估和最终离线 HTML 的轮次与统计口径。"""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import os
import re

import pytest
from psycopg.types.json import Jsonb

from quality.offline import repository as offline
from quality.offline.bundle import build_bundle
from quality.offline.config import OfflineSettings
from quality.offline.media import MediaDownloader
from quality.query.repository import ArchiveFilters
from quality.tests import test_query_database as query_database
from quality.tests.test_processing import START, event, agent_reply

pytestmark = pytest.mark.skipif(os.environ.get("PROCESSING_TEST_DATABASE") != "1",
                                reason="需要独立的归档查询测试数据库")


@pytest.fixture
def case(monkeypatch):
    # 复用现有数据库安全检查和真实入库链路，不收集原测试类的全部测试。
    fixture = query_database.QueryDatabaseTest
    fixture.setUpClass()
    instance = fixture(methodName="runTest")
    instance.setUp()
    monkeypatch.setattr(offline, "_agent_names", lambda: {})
    try:
        yield instance
    finally:
        instance.doCleanups()
        fixture.tearDownClass()


def archive(tmp_path, request, *, split=False, monkeypatch=None):
    if split:
        import quality.offline.bundle as bundle
        monkeypatch.setattr(bundle, "PART_BYTES", 1)
    settings = OfflineSettings(_env_file=None, directory=str(tmp_path), min_free_bytes=0)
    counts = offline.estimate(request, settings)
    metadata = {}
    path = build_bundle(tmp_path / "work", offline.messages(request, lambda: None, settings, metadata),
                        settings, MediaDownloader([]), lambda **values: None, lambda: None,
                        scope=request, metadata=metadata)
    text = path.read_text(encoding="utf-8")
    blocks = {key: json.loads(value) for key, value in re.findall(
        r'<script type="application/json" id="([^"]+)">(.*?)</script>', text, re.S)}
    catalog = blocks.pop("catalog")
    rows = [{"_customer_id": catalog["customers"][entry["customer"]]["id"], **row}
            for entry in catalog["entries"] for part in range(entry["parts"])
            for row in blocks[f"g{entry['index']}.{part}"]]
    for field in ("rounds", "messages", "unrounded_messages"):
        assert catalog["meta"][field] == counts[field]
    assert catalog["meta"]["snapshot_at"] == metadata["snapshot_at"]
    return catalog, rows


def filtered(case, **changes):
    filters = ArchiveFilters(source_key=case.source, account_id=1, **changes)
    return filters, {"mode": "rounds", "view": "rounds", "summary": "隔离测试筛选", "filters": asdict(filters)}


def seed_human(case, *, assisted=False, lost=False):
    case.receive(event(1, 0, content="退款问题"), event(2, 2, content="请处理退款"))
    case.receive_assignment("8", minute=1)
    if not lost:
        case.receive(agent_reply(3, 3, agent_id=8))
        if assisted:
            case.receive(agent_reply(4, 4, agent_id=9))
        case.receive(event(5, 5, event_type="conversation_status_changed", status="resolved"))
    case.drain()
    return case.rows("ops.round_index")[0]["round_id"]


@pytest.mark.parametrize("changes", [
    {}, {"keyword": "退款"}, {"message_scope": "agent"},
    {"rating_status": "rated", "rating": 4, "rating_op": "lt"},
    {"agent_id": "8"}, {"assisted": False}, {"lost": True},
    {"as_of": START + timedelta(minutes=3)},
    {"metric": "assigned_inquiries", "metric_seat": "8", "start": START, "end": START + timedelta(days=1)},
])
def test_filtered_page_estimate_and_html_select_same_rounds(case, tmp_path, changes):
    round_id = seed_human(case)
    # 轮次评价没有邀请消息也必须导出；消息气泡无法替代这一来源。
    case.admin.execute("UPDATE ops.round_index SET rating_status='rated',rating=3,"
                       "data=data || %s WHERE round_id=%s", (Jsonb({"rating_feedback": "服务反馈"}), round_id))
    filters, request = filtered(case, **changes)
    page = case.reader.list_rounds(filters, page_size=100)
    catalog, rows = archive(tmp_path, request)
    entries = catalog["entries"]
    assert {entry["round_id"] for entry in entries} == {item["round_id"] for item in page["items"]}
    assert len(entries) == page["total"]
    for entry in entries:
        item = next(item for item in page["items"] if item["round_id"] == entry["round_id"])
        facts = item["statistics"]
        assert (entry["rating"], entry["rating_feedback"], entry["rated"]) == (3, "服务反馈", True)
        for key in ("assigned_agent_id", "responsible_agent_id", "assisted", "is_lost"):
            assert entry[key] == facts[key]
        for key in ("first_response_seconds", "average_response_seconds"):
            assert entry["service_metrics"][key] == facts[key]
    assert catalog["meta"]["summary"] == "隔离测试筛选"
    if changes.get("as_of"):
        assert catalog["meta"]["as_of"] == changes["as_of"].isoformat()


@pytest.mark.parametrize("assisted,lost", [(False, False), (True, False), (False, True)])
def test_assisted_lost_and_timing_match_card(case, tmp_path, monkeypatch, assisted, lost):
    seed_human(case, assisted=assisted, lost=lost)
    filters, request = filtered(case, assisted=assisted, lost=True if lost else None)
    card = case.reader.list_customers(filters)["items"][0]["latest_round"]
    catalog, _ = archive(tmp_path, request, split=True, monkeypatch=monkeypatch)
    entry = catalog["entries"][0]
    # 方案明确不展示咨询状态总时长；其余字段都复用页面统计口径。
    assert {key: value for key, value in entry["service_metrics"].items() if key != "consultation_seconds"} == {
        key: value for key, value in card["service_metrics"].items() if key != "consultation_seconds"}
    assert entry["assisted"] is assisted and entry["is_lost"] is lost
    assert entry["parts"] > 1 and catalog["meta"]["rounds"] == 1
    if not assisted and not lost:
        assert entry["service_metrics"]["first_response_seconds"] == 60


def test_customer_unrounded_or_lock_and_current_edited_body(case, tmp_path):
    case.receive(event(1, content="旧正文"), agent_reply(2, 6),
                 event(3, 1, conversation_id=22, sender="mystery"), event(4, 6, conversation_id=22, sender="mystery"),
                 event(5, 2, conversation_id=22, sender="mystery"),
                 event(6, 7, event_type="message_updated", message_id=1,
                       created_at=START.isoformat(), updated_at=(START+timedelta(minutes=7)).isoformat(),
                       content="锁定后修改的当前正文"))
    case.drain()
    case.admin.execute("UPDATE ops.message_index SET sent_at=NULL WHERE source_key=%s AND message_id=5", (case.source,))
    filters, request = filtered(case, as_of=START+timedelta(minutes=5), time_field="message_at")
    request["view"] = "customers"
    catalog, rows = archive(tmp_path, request)
    assert {row["message_id"] for row in rows} == {"1", "3", "5"}
    assert next(row for row in rows if row["message_id"] == "1")["content"] == "锁定后修改的当前正文"
    assert catalog["meta"]["rounds"] == 1 and catalog["meta"]["unrounded_messages"] == 2
    assert all(entry.get("service_metrics") is None for entry in catalog["entries"] if not entry["round_id"])
    page = case.reader.list_rounds(filters)
    assert {entry["round_id"] for entry in catalog["entries"] if entry["round_id"]} == {item["round_id"] for item in page["items"]}


def test_month_exports_whole_round_closed_at_and_unknown_time(case, tmp_path):
    before = "2026-10-31T15:59:00+00:00"
    boundary = "2026-10-31T16:00:00+00:00"
    ended = "2026-11-02T00:00:00+00:00"
    case.receive(event(1, created_at=before), agent_reply(2, 1, created_at=ended),
                 event(3, 2, conversation_id=22, created_at=before),
                 event(4, 3, conversation_id=22, created_at=boundary),
                 event(5, 4, conversation_id=22, created_at=boundary),
                 event(6, 5, conversation_id=32, created_at=before),
                 event(7, 6, conversation_id=42, created_at=boundary),
                 event(8, 7, conversation_id=12, sender="agent_bot", direction="outgoing", created_at=ended))
    case.drain()
    rounds = {row["conversation_id"]: row for row in case.rows("ops.round_index")}
    main = rounds[12]["round_id"]
    case.admin.execute("UPDATE ops.round_index SET started_at=%s,last_message_at=%s,status='resolved',"
                       "data=(data - 'resolved_at') || %s WHERE round_id=%s",
                       (before, ended, Jsonb({"closed_at": ended, "handoff_requested_at": before}), main))
    case.admin.execute("UPDATE ops.message_index SET round_id=NULL WHERE source_key=%s AND conversation_id=22", (case.source,))
    case.admin.execute("UPDATE ops.message_index SET sent_at=NULL WHERE source_key=%s AND message_id IN (5,8)", (case.source,))
    case.admin.execute("UPDATE ops.round_index SET last_message_at=%s WHERE source_key=%s AND conversation_id=42", (boundary, case.source))
    october, oct_rows = archive(tmp_path / "oct", {"mode": "month", "month": "2026-10"})
    november, nov_rows = archive(tmp_path / "nov", {"mode": "month", "month": "2026-11"})
    assert main not in {entry["round_id"] for entry in october["entries"]}
    entry = next(entry for entry in november["entries"] if entry["round_id"] == main)
    assert entry["messages"] == 3
    metrics = entry["service_metrics"]
    assert (metrics["human_phase_message_count"], metrics["human_phase_customer_count"], metrics["human_phase_agent_count"]) == (2, 1, 1)
    page = case.reader.list_rounds(ArchiveFilters(source_key=case.source, account_id=1, conversation_id=12))
    assert metrics["first_response_seconds"] == page["items"][0]["statistics"]["first_response_seconds"]
    assert entry["service_metrics"]["duration_seconds"] == 115260
    assert {"1", "2"} <= {row["message_id"] for row in nov_rows}
    # 北京时间月初边界归新月；未知时间的未归轮消息不属于任何月份。
    assert rounds[42]["round_id"] in {entry["round_id"] for entry in november["entries"]}
    assert rounds[32]["round_id"] in {entry["round_id"] for entry in october["entries"]}
    own_oct = [entry for entry in october["entries"] if entry["round_id"] in {r["round_id"] for r in rounds.values()}]
    assert all(entry["round_id"] != rounds[42]["round_id"] for entry in own_oct)
    assert not any(row["message_id"] == "5" for row in nov_rows)
    _, all_rows = archive(tmp_path / "all", {"mode": "all"})
    customer_ids = {row["customer_id"] for row in case.rows("ops.customer_index")}
    assert "5" in {row["message_id"] for row in all_rows if row["_customer_id"] in customer_ids}



def test_round_rating_current_value_and_unlinked_bubbles_stay_separate(case, tmp_path):
    closed = (START + timedelta(minutes=1)).isoformat()
    case.receive(event(1), event(2, 1, event_type="conversation_status_changed", status="resolved"),
        event(3, 2),
        event(4, 3, message_id=500, sender="agent_bot", direction="outgoing", content_type="input_csat",
              content_attributes={"ics_closed_at": closed}, csat_survey_response={"rating": 1, "message_id": 500}),
        event(5, 4, message_id=501, sender="agent_bot", direction="outgoing", content_type="input_csat",
              content_attributes={"ics_round_id": "unobserved-business-round", "rating": 2}))
    case.drain()
    rounds = sorted(case.rows("ops.round_index"), key=lambda row: row["round_no"])
    old, current = rounds[0]["round_id"], rounds[1]["round_id"]
    # 评价晚于锁定时刻提交；目录仍与页面一致采用生成快照的当前轮次评价。
    case.admin.execute("UPDATE ops.round_index SET rating_status='rated',rating=5,"
                       "data=data || %s WHERE round_id=%s", (Jsonb({"rating_feedback": "后来补充的反馈"}), old))
    filters, request = filtered(case, as_of=START + timedelta(minutes=2, seconds=30))
    catalog, rows = archive(tmp_path / "locked", request)
    entry = next(entry for entry in catalog["entries"] if entry["round_id"] == old)
    assert (entry["rating"], entry["rated"], entry["rating_feedback"]) == (5, True, "后来补充的反馈")
    assert not any(row["message_id"] in {"500", "501"} for row in rows)
    assert case.reader.list_rounds(filters)["items"][-1]["rating"] == 5
    _, unlocked_request = filtered(case)
    unlocked_request["view"] = "customers"
    catalog, rows = archive(tmp_path / "unlocked", unlocked_request)
    bubbles = {row["message_id"]: row["visitor_rating"] for row in rows}
    assert bubbles["500"]["rating"] == 1
    assert bubbles["501"] is None
    assert next(entry for entry in catalog["entries"] if entry["round_id"] == old)["rating"] == 5
    assert next(entry for entry in catalog["entries"] if entry["round_id"] == current)["rated"] is False
