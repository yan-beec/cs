"""Synthetic round-trip checks for the streaming archive workbook exporter."""

from datetime import datetime, timezone
from io import BytesIO
import json
import re
from xml.etree import ElementTree
from zipfile import ZipFile

from openpyxl import load_workbook
import pytest

from quality.query import excel


NOW = datetime(2026, 10, 16, 1, 2, 3, 456789, tzinfo=timezone.utc)
LARGE_ID = 9_223_372_036_854_775_123


class Once:
    """Reject iterables being counted or traversed again before writing."""

    def __init__(self, rows):
        self.rows = rows
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        assert self.iterations == 1, "dataset iterator was consumed more than once"
        yield from self.rows

    def __len__(self):
        raise AssertionError("dataset must not be materialized before writing")


def _export(dataset=None, *, mode="rounds", filters=None):
    destination = BytesIO()
    stats = excel.build_workbook(destination, dataset or {}, filters=filters or {}, exported_at=NOW, mode=mode)
    assert not destination.closed
    destination.seek(0)
    return destination, load_workbook(destination), stats


def _rows(sheet):
    values = list(sheet.values)
    return [dict(zip(values[0], row)) for row in values[1:]]


def _restore(value):
    if not value.startswith("[ICS-ESC]"):
        return value
    body = value[len("[ICS-ESC]"):]

    def decode(match):
        token = match.group(0)
        return "\\" if token == "\\\\" else chr(int(token[2:], 16))

    return re.sub(r"\\\\|\\u[0-9A-F]{4}", decode, body)


def _message(**overrides):
    row = {
        "source_key": "source-a", "account_id": LARGE_ID, "conversation_id": LARGE_ID - 1,
        "customer_id": "customer-1", "message_id": LARGE_ID - 2, "round_id": "round-1", "round_no": 2,
        "sender_id": "0001234567890123456789", "sender_name": "客服甲", "sender_role": "agent",
        "direction": "outgoing", "is_private": False, "content": "完整原文", "sent_at": NOW,
        "has_creation_event": True, "timestamp_status": "known", "content_type": "text",
        "is_deleted": False, "content_is_pre_delete": False,
        "original_snapshot_id": LARGE_ID - 3, "latest_snapshot_id": LARGE_ID - 4,
        "original_event_id": LARGE_ID - 5, "latest_event_id": LARGE_ID - 6,
    }
    row.update(overrides)
    return row


def test_customers_mode_structure_styles_counts_and_single_pass_iterables():
    customers = Once([{"source_key": "s", "account_id": 1, "customer_id": "c", "display_name": "客户甲", "round_count": 1, "message_count": 2}])
    rounds = Once([{"source_key": "s", "account_id": 1, "round_id": "r", "round_no": 1, "agents": [], "counts": {"contact": 1, "agent": 1}}])
    messages = Once([_message(message_id=1), _message(message_id=2, content=None)])
    _, workbook, stats = _export({"customers": customers, "rounds": rounds, "messages": messages}, mode="customers", filters={"keyword": "原文"})
    assert workbook.sheetnames == ["导出说明", "用户概览", "轮次概览", "消息明细"]
    assert stats == {"customers": 1, "rounds": 1, "messages": 2, "message_rows": 2, "ratings": 0, "sheets": workbook.sheetnames}
    assert [customers.iterations, rounds.iterations, messages.iterations] == [1, 1, 1]
    for sheet in workbook:
        assert sheet.freeze_panes == "A2"
        assert sheet.auto_filter.ref.endswith(str(sheet.max_row))
        assert sheet.column_dimensions["A"].width >= 20
        assert sheet["A1"].font.bold and sheet["A1"].fill.fgColor.rgb == "00234E70"
        assert sheet["A2"].alignment.wrap_text
    notes = {row["条目"]: row["说明或数值"] for row in _rows(workbook["导出说明"])}
    assert notes["逻辑消息数"] == 2 and notes["消息分段行数"] == 2 and notes["数据行总数"] == 4
    assert "完整已归档历史" in notes["数据范围"]
    assert "无法" in notes["原始记录口径"] and "重建" in notes["原始记录口径"]
    assert "1048576" in notes["长度与行数上限"]


@pytest.mark.parametrize("mode,names", [
    ("rounds", ["导出说明", "轮次概览", "消息明细"]),
    ("customers", ["导出说明", "用户概览", "轮次概览", "消息明细"]),
    ("unlinked-ratings", ["导出说明", "待关联评价"]),
])
def test_empty_exports_keep_headers_and_document_zero_records(mode, names):
    _, workbook, stats = _export(mode=mode)
    assert workbook.sheetnames == names
    assert all(stats[key] == 0 for key in ("customers", "rounds", "messages", "message_rows", "ratings"))
    for sheet in list(workbook)[1:]:
        assert sheet.max_row == 1 and sheet.auto_filter.ref.endswith("1")


def test_large_and_zero_prefixed_ids_are_text_in_every_record_type():
    data = {
        "customers": [{"account_id": LARGE_ID, "customer_id": "000003", "contact_id": LARGE_ID, "business_user_id": "000004"}],
        "rounds": [{"account_id": LARGE_ID, "conversation_id": LARGE_ID, "round_id": LARGE_ID, "customer_id": "000003", "current_assignee_id": LARGE_ID,
                    "responsible_agent_id": LARGE_ID, "responsibility_event_id": LARGE_ID - 1, "responsibility_basis": "ics_assignment",
                    "responsibility_trigger_message_id": "000123", "last_public_message_id": LARGE_ID}],
        "messages": [_message()],
    }
    _, workbook, _ = _export(data, mode="customers")
    for name, labels in {
        "用户概览": ["账号ID", "Chatwoot联系人ID", "业务用户ID", "用户ID"],
        "轮次概览": ["账号ID", "会话ID", "轮次ID", "轮内最后分配坐席ID（非责任）", "责任坐席ID", "ICS分配事件ID", "ICS分配触发消息ID", "最后实际人工回复消息ID"],
        "消息明细": ["账号ID", "会话ID", "消息ID", "发送人ID", "原始快照ID", "最新快照ID", "原始事件ID", "最新事件ID"],
    }.items():
        sheet = workbook[name]
        header = [cell.value for cell in sheet[1]]
        for label in labels:
            cell = sheet.cell(2, header.index(label) + 1)
            assert cell.data_type == "s" and cell.number_format == "@"
            assert isinstance(cell.value, str)
    assert _rows(workbook["用户概览"])[0]["业务用户ID"] == "000004"
    assert _rows(workbook["消息明细"])[0]["消息ID"] == str(LARGE_ID - 2)
    assert _rows(workbook["消息明细"])[0]["发送人ID"] == "0001234567890123456789"


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@"])
def test_external_strings_remain_exact_text_and_never_formulas(prefix):
    payload = prefix + 'HYPERLINK("https://example.test","原文")'
    data = {"customers": [{"display_name": payload}], "rounds": [{"source_key": payload, "handoff_reason": payload}],
            "messages": [_message(content=payload, sender_name=payload)]}
    destination, workbook, _ = _export(data, mode="customers", filters={"keyword": payload})
    assert _rows(workbook["用户概览"])[0]["用户名称"] == payload
    assert _rows(workbook["消息明细"])[0]["正文分段"] == payload
    assert _rows(workbook["消息明细"])[0]["发送人名称"] == payload
    assert _rows(workbook["轮次概览"])[0]["转人工原因"] == payload
    for sheet in workbook:
        assert all(cell.data_type != "f" for row in sheet for cell in row)
    with ZipFile(destination) as archive:
        for name in archive.namelist():
            if name.startswith("xl/worksheets/") and name.endswith(".xml"):
                document = ElementTree.fromstring(archive.read(name))
                assert not document.findall(".//{*}f")
                assert not document.findall(".//{*}hyperlink")


def test_long_message_reconstructs_exact_text_with_emoji_illegal_characters_and_escapes():
    content = "=" + "😀" * 20_000 + "\x00\x1f\x85\ud800\ufffe\uffff" + r"\u0000\folder" + "尾" * 35_000
    _, workbook, stats = _export({"messages": Once([_message(content=content)])})
    rows = _rows(workbook["消息明细"])
    assert stats["messages"] == 1 and stats["message_rows"] == len(rows) > 2
    assert [row["分段序号"] for row in rows] == list(range(1, len(rows) + 1))
    assert all(row["分段总数"] == len(rows) for row in rows)
    assert all(row["实例来源"] == "source-a" and row["账号ID"] == str(LARGE_ID) and row["消息ID"] == str(LARGE_ID - 2) for row in rows)
    chunks = [row["正文分段"] or "" for row in rows]
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 32767 for chunk in chunks)
    assert _restore("".join(chunks)) == content


@pytest.mark.parametrize("content", ["", "x" * 32767, "x" * 32768, "😀" * 16383 + "a", "😀" * 16384, "[ICS-ESC]" + r"literal\u0000", "C:\\folder\\file.txt\t正文\n换行\r回车"],
                         ids=["empty", "bmp-limit", "bmp-split", "emoji-limit", "emoji-split", "prefix-collision", "ordinary-controls"])
def test_body_boundaries_prefix_collisions_and_ordinary_backslashes_round_trip(content):
    _, workbook, _ = _export({"messages": [_message(content=content)]})
    rows = _rows(workbook["消息明细"])
    assert _restore("".join(row["正文分段"] or "" for row in rows)) == content
    assert all(len((row["正文分段"] or "").encode("utf-16-le")) // 2 <= 32767 for row in rows)


def test_attachment_metadata_and_original_urls_are_preserved_without_hyperlinks():
    attachment = {"attachment_key": "a", "source_attachment_id": str(LARGE_ID), "filename": "=1+1.png", "media_type": "image/png",
                  "source_url": "https://example.test/file?token=original&x=1", "expected_byte_count": 123,
                  "metadata": {"caption": "原始说明", "source_integer": LARGE_ID}}
    destination, workbook, _ = _export({"messages": [_message(attachments=[attachment])]})
    assert json.loads(_rows(workbook["消息明细"])[0]["附件元数据及原始URL"]) == [attachment]
    assert all(cell.hyperlink is None for row in workbook["消息明细"] for cell in row)
    with ZipFile(destination) as archive:
        assert not any(name.startswith("xl/media/") or name.startswith("xl/externalLinks/") for name in archive.namelist())


def test_non_body_control_characters_and_encoding_prefix_are_also_reversible():
    values = {"source_key": "source\x00a", "sender_name": "[ICS-ESC]" + r"literal\u0000", "customer_name": "客户\udfff\uffff"}
    _, workbook, _ = _export({"messages": [_message(**values)]}, filters={"keyword": "查询\x01"})
    row = _rows(workbook["消息明细"])[0]
    for key, label in (("source_key", "实例来源"), ("sender_name", "发送人名称"), ("customer_name", "用户名称")):
        assert _restore(row[label]) == values[key]
    notes = {row["条目"]: row for row in _rows(workbook["导出说明"])}
    assert _restore(notes["筛选：消息关键词"]["说明或数值"]) == "查询\x01"


def test_times_filters_enum_labels_unknown_channel_and_observation_flags():
    rounds = [{"reception_type": "ai_only", "classification_finalized": False, "reply_composition": "mixed", "status": "active", "rating_status": "pending",
               "started_at": NOW, "resolved_at": "2026-10-16T00:30:00-05:00", "handoff_result": "completed", "rating": 5,
               "agents": [{"agent_id": LARGE_ID, "agent_name": "甲", "replied": True, "was_assigned": False, "noted": True, "reply_count": 1}]}]
    filters = {"start": datetime(2026, 10, 15, tzinfo=timezone.utc), "end": "2026-10-17T08:00:00+08:00", "time_field": "started_at", "has_attachment": False}
    _, workbook, _ = _export({"rounds": rounds, "messages": [_message(is_deleted=True, has_creation_event=False, content_is_pre_delete=True, sent_at=None)]}, filters=filters)
    row = _rows(workbook["轮次概览"])[0]
    assert row["开始时间（北京时间）"] == "2026-10-16 09:02:03.456789+08:00"
    assert row["解决时间（北京时间）"] == "2026-10-16 13:30:00+08:00"
    assert row["接待类型"] == "纯 AI 接待" and row["回复构成"] == "AI 与人工回复"
    assert row["轮次已结束"] == "否" and row["轮次状态"] == "未结束"
    assert "分类已确定" not in row
    assert row["评价状态"] == "待评价" and row["转人工结果"] == "已接入人工"
    assert str(LARGE_ID) in row["实际回复坐席"] and row["曾经分配坐席（非责任）"] is None
    message = _rows(workbook["消息明细"])[0]
    assert message["渠道"] == "未映射渠道" and message["发送时间（北京时间）"] is None
    assert message["已删除或撤回"] == "是" and message["捕获创建事件"] == "否" and message["正文来自删除前观察"] == "是"
    notes = {row["条目"]: row for row in _rows(workbook["导出说明"])}
    assert notes["筛选：开始时间"]["说明或数值"] == "2026-10-15 08:00:00+08:00"
    assert notes["筛选：时间口径"]["说明或数值"] == "轮次开始时间"
    assert notes["筛选：包含附件"]["说明或数值"] == "否"
    assert "不等待轮次结束" in notes["接待统计口径"]["说明或数值"]


def test_active_human_request_exports_as_human_without_claiming_completed_handoff():
    _, workbook, _ = _export({"rounds": [{
        "reception_type": "human", "classification_finalized": False,
        "status": "active", "handoff_result": "requested", "reply_composition": "none",
    }]}, filters={"reception_type": "human"})
    row = _rows(workbook["轮次概览"])[0]
    assert row["接待类型"] == "人工介入"
    assert row["转人工结果"] == "已请求转人工"
    assert row["轮次已结束"] == "否" and row["回复构成"] == "无回复"
    assert row["接入人工时间（北京时间）"] is None
    notes = {row["条目"]: row for row in _rows(workbook["导出说明"])}
    assert notes["筛选：接待类型"]["说明或数值"] == "人工介入"


def test_submitted_rating_feedback_is_preserved_as_text_in_round_export():
    _, workbook, _ = _export({"rounds": [{
        "rating_status": "rated", "rating": 5, "rating_feedback": "=客户反馈\n很好",
    }]}, filters={"rating": 5})
    row = _rows(workbook["轮次概览"])[0]
    assert row["评分"] == 5 and row["评价反馈"] == "=客户反馈\n很好"
    feedback_index = next(cell.column for cell in workbook["轮次概览"][1] if cell.value == "评价反馈")
    assert workbook["轮次概览"].cell(2, feedback_index).data_type == "s"


def test_round_responsibility_is_fixed_to_ics_and_actual_replies_remain_separate():
    _, workbook, _ = _export({"rounds": [
        {"current_assignee_id": "8", "responsible_agent_id": "7", "responsible_agent_name": "=ICS分配者",
         "responsible_assigned_at": NOW, "responsibility_event_id": LARGE_ID, "responsibility_trigger_message_id": "000123",
         "responsibility_external_round_id": "business-1", "responsibility_basis": "ics_assignment",
         "last_public_agent_id": "9", "last_public_agent_name": "实际回复人", "last_public_reply_at": NOW, "last_public_message_id": LARGE_ID-1},
        {"current_assignee_id": "8", "responsibility_basis": "assignment_unobserved"},
        {"current_assignee_id": "8", "responsibility_basis": "assignment_conflict"},
    ]}, filters={"agent_id": "7", "agent_scope": "responsible"})
    replied, no_reply, unknown = _rows(workbook["轮次概览"])
    assert replied["责任坐席ID"] == "7" and replied["轮内最后分配坐席ID（非责任）"] == "8"
    assert replied["责任坐席名称"] == "=ICS分配者"
    assert replied["ICS分配事件ID"] == str(LARGE_ID)
    assert replied["ICS分配触发消息ID"] == "000123" and replied["ICS业务轮次ID"] == "business-1"
    assert replied["ICS分配时间（北京时间）"] == "2026-10-16 09:02:03.456789+08:00"
    assert replied["责任依据"] == "本轮 ICS 原始分配"
    assert replied["最后实际人工回复人ID（非责任）"] == "9"
    assert replied["最后实际人工回复消息ID"] == str(LARGE_ID-1)
    assert no_reply["责任坐席ID"] is None and no_reply["责任坐席名称"] is None
    assert no_reply["责任依据"] == "未记录 ICS 分配"
    assert unknown["责任坐席ID"] is None and unknown["责任坐席名称"] is None
    assert unknown["责任依据"] == "ICS 分配证据冲突，待确认"
    notes = {row["条目"]: row["说明或数值"] for row in _rows(workbook["导出说明"])}
    assert notes["筛选：坐席范围"] == "本轮责任坐席（ICS分配）"
    assert "后续 Chatwoot 改派" in notes["责任坐席口径"]
    assert all(cell.data_type != "f" for row in workbook["轮次概览"] for cell in row)


@pytest.mark.parametrize("basis", [None, "last_public_agent_reply", "current_assignee", "assignment_unobserved", "assignment_conflict"])
def test_legacy_and_unconfirmed_projections_never_export_as_ics_responsibility(basis):
    legacy = {"responsibility_basis": basis, "responsible_agent_id": "9", "responsible_agent_name": "旧回复人",
        "responsible_assigned_at": NOW, "responsibility_event_id": 123, "responsibility_external_round_id": "stale",
        "responsibility_trigger_message_id": "1", "last_public_agent_id": "9", "last_public_agent_name": "实际回复人"}
    _, workbook, _ = _export({"rounds": [legacy]})
    row = _rows(workbook["轮次概览"])[0]
    for column in ("责任坐席ID", "责任坐席名称", "ICS分配时间（北京时间）", "ICS分配事件ID", "ICS业务轮次ID", "ICS分配触发消息ID"):
        assert row[column] is None
    assert row["责任依据"] == ("ICS 分配证据冲突，待确认" if basis == "assignment_conflict" else "未记录 ICS 分配")
    assert row["最后实际人工回复人ID（非责任）"] == "9"
    assert row["最后实际人工回复人名称（非责任）"] == "实际回复人"
    assert legacy["responsible_agent_id"] == "9"


def test_unlinked_ratings_use_dedicated_sheet_and_keep_feedback_as_text():
    ratings = Once([{"source_key": "s", "account_id": LARGE_ID, "conversation_id": LARGE_ID, "rating_key": "k", "status": "unlinked",
                     "rating": 1, "feedback": "=私密评价", "source_time": NOW, "snapshot_id": LARGE_ID, "event_id": LARGE_ID}])
    _, workbook, stats = _export({"ratings": ratings}, mode="unlinked-ratings")
    assert workbook.sheetnames == ["导出说明", "待关联评价"] and stats["ratings"] == 1
    assert ratings.iterations == 1
    row = _rows(workbook["待关联评价"])[0]
    assert row["账号ID"] == str(LARGE_ID) and row["事件ID"] == str(LARGE_ID)
    assert row["评价备注"] == "=私密评价" and row["评价状态"] == "待关联" and row["轮次ID"] is None
    assert row["评价时间（北京时间）"] == "2026-10-16 09:02:03.456789+08:00"


@pytest.mark.parametrize("dataset,mode,filters", [
    ({"rounds": [{"agents": [{"agent_id": "1", "agent_name": "敏感坐席" * 9000}]}]}, "rounds", {}),
    ({"ratings": [{"feedback": "敏感评价" * 9000}]}, "unlinked-ratings", {}),
    ({"ratings": [{"feedback": "😀" * 16384}]}, "unlinked-ratings", {}),
    ({"messages": [_message(attachments=[{"source_url": "https://example.test/" + "x" * 32768}])]}, "rounds", {}),
    ({}, "rounds", {"keyword": "敏感关键词" * 9000}),
], ids=["agent-record", "rating-feedback", "rating-emoji", "attachment-metadata", "filter-keyword"])
def test_oversized_non_body_fields_fail_explicitly_without_leaking_content(dataset, mode, filters):
    with pytest.raises(excel.ExcelCellLimitExceeded) as caught:
        _export(dataset, mode=mode, filters=filters)
    assert isinstance(caught.value, excel.ExcelLimitExceeded)
    assert "长度上限" in str(caught.value) and "敏感" not in str(caught.value)


def test_row_limit_includes_header_and_never_silently_drops_rows(monkeypatch):
    monkeypatch.setattr(excel, "MAX_EXCEL_ROWS", 40)
    _, workbook, stats = _export({"rounds": ({} for _ in range(39))})
    assert workbook["轮次概览"].max_row == 40 and stats["rounds"] == 39
    with pytest.raises(excel.ExcelRowLimitExceeded, match="行数上限") as caught:
        _export({"rounds": ({} for _ in range(40))})
    assert isinstance(caught.value, excel.ExcelLimitExceeded)


def test_message_continuation_rows_also_obey_excel_row_limit(monkeypatch):
    monkeypatch.setattr(excel, "MAX_EXCEL_ROWS", 40)
    with pytest.raises(excel.ExcelRowLimitExceeded, match="行数上限"):
        _export({"messages": [_message(content="x" * (32767 * 40))]})


def test_unknown_mode_is_rejected_before_destination_is_written():
    destination = BytesIO()
    with pytest.raises(ValueError, match="Unsupported"):
        excel.build_workbook(destination, {}, filters={}, exported_at=NOW, mode="invalid")
    assert destination.getvalue() == b""
