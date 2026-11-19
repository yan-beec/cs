"""Synthetic round-trip checks for the streaming archive workbook exporter."""

from datetime import datetime, timezone
from io import BytesIO
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


@pytest.mark.parametrize("mode", ["rounds", "customers"])
def test_chat_export_is_one_filtered_sheet_with_no_metadata_and_single_pass(mode):
    customers = Once([{"display_name": "不应导出的概况"}])
    rounds = Once([{"handoff_reason": "不应导出的说明", "rating_feedback": "不应导出的评价"}])
    messages = Once([_message(message_id=1, customer_name="客户甲"), _message(message_id=2, content=None)])
    _, workbook, stats = _export({"customers": customers, "rounds": rounds, "messages": messages}, mode=mode,
                                 filters={"keyword": "不应导出的筛选参数"})
    assert workbook.sheetnames == ["聊天记录"]
    assert stats == {"customers": 0, "rounds": 0, "messages": 2, "message_rows": 2, "ratings": 0, "sheets": ["聊天记录"]}
    assert [customers.iterations, rounds.iterations, messages.iterations] == [0, 0, 1]
    sheet = workbook.active
    assert list(next(sheet.values)) == ["客户", "发送时间（北京时间）", "发送人", "发送人类型", "聊天内容", "会话ID", "消息ID", "消息标记", "附件", "会话内轮次", "用户累计咨询", "AI质检", "人工质检", "质检备注"]
    assert sheet.max_row == 3 and sheet.max_column == 14
    assert sheet.freeze_panes == "A2" and sheet.auto_filter.ref == "A1:N3"
    assert sheet.column_dimensions["E"].width >= 80
    assert sheet["A1"].font.bold and sheet["A1"].fill.fgColor.rgb == "00234E70"
    assert sheet["E2"].alignment.wrap_text
    assert _rows(sheet)[0]["客户"] == "客户甲"
    assert _rows(sheet)[1]["聊天内容"] is None
    assert "不应导出" not in str(list(sheet.values))


@pytest.mark.parametrize("mode,name", [("rounds", "聊天记录"), ("customers", "聊天记录"), ("unlinked-ratings", "待关联评价")])
def test_empty_exports_keep_only_the_header_and_filter(mode, name):
    _, workbook, stats = _export(mode=mode)
    assert workbook.sheetnames == [name]
    assert all(stats[key] == 0 for key in ("customers", "rounds", "messages", "message_rows", "ratings"))
    assert workbook.active.max_row == 1 and workbook.active.auto_filter.ref.endswith("1")
    assert workbook.active.freeze_panes == "A2"


@pytest.mark.parametrize("identifier", [LARGE_ID, "0001234567890123456789"])
def test_message_and_conversation_ids_keep_all_digits_as_text(identifier):
    _, workbook, _ = _export({"messages": [_message(conversation_id=identifier, message_id=identifier)]})
    sheet = workbook.active
    for label in ("会话ID", "消息ID"):
        column = [cell.value for cell in sheet[1]].index(label) + 1
        cell = sheet.cell(2, column)
        assert cell.value == str(identifier) and cell.data_type == "s" and cell.number_format == "@"


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@"])
def test_external_strings_remain_exact_text_and_never_formulas(prefix):
    payload = prefix + 'HYPERLINK("https://example.test","原文")'
    destination, workbook, _ = _export({"messages": [_message(content=payload, sender_name=payload, customer_name=payload)]})
    row = _rows(workbook.active)[0]
    assert row["客户"] == row["聊天内容"] == row["发送人"] == payload
    assert all(cell.data_type != "f" for sheet in workbook for cells in sheet for cell in cells)
    with ZipFile(destination) as archive:
        for name in archive.namelist():
            if name.startswith("xl/worksheets/") and name.endswith(".xml"):
                document = ElementTree.fromstring(archive.read(name))
                assert not document.findall(".//{*}f")
                assert not document.findall(".//{*}hyperlink")


def test_long_message_reconstructs_exact_text_with_emoji_illegal_characters_and_escapes():
    content = "=" + "😀" * 20_000 + "\x00\x1f\x85\ud800\ufffe\uffff" + r"\u0000\folder" + "尾" * 35_000
    _, workbook, stats = _export({"messages": Once([_message(content=content)])})
    rows = _rows(workbook.active)
    assert stats["messages"] == 1 and stats["message_rows"] == len(rows) > 2
    assert all(row["消息ID"] == str(LARGE_ID - 2) and row["会话ID"] == str(LARGE_ID - 1) for row in rows)
    chunks = [row["聊天内容"] or "" for row in rows]
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 32767 for chunk in chunks)
    assert _restore("".join(chunks)) == content
    assert workbook.active.auto_filter.ref == f"A1:N{len(rows) + 1}"


@pytest.mark.parametrize("content", ["", "x" * 32767, "x" * 32768, "😀" * 16383 + "a", "😀" * 16384, "[ICS-ESC]" + r"literal\u0000", "C:\\folder\\file.txt\t正文\n换行\r回车"],
                         ids=["empty", "bmp-limit", "bmp-split", "emoji-limit", "emoji-split", "prefix-collision", "ordinary-controls"])
def test_body_boundaries_prefix_collisions_and_ordinary_backslashes_round_trip(content):
    _, workbook, _ = _export({"messages": [_message(content=content)]})
    rows = _rows(workbook.active)
    assert _restore("".join(row["聊天内容"] or "" for row in rows)) == content
    assert all(len((row["聊天内容"] or "").encode("utf-16-le")) // 2 <= 32767 for row in rows)


def test_attachments_show_only_filenames_and_original_urls():
    attachment = {"attachment_key": "irrelevant-key", "source_attachment_id": str(LARGE_ID), "filename": "=1+1.png",
                  "media_type": "image/png", "source_url": "https://example.test/file?token=original&x=1",
                  "expected_byte_count": 123, "metadata": {"caption": "不应导出的元数据"}}
    destination, workbook, _ = _export({"messages": [_message(content=None, attachments=[attachment,
        {"filename": "另一个文件.pdf", "source_url": "https://example.test/two"}, {}])]})
    row = _rows(workbook.active)[0]
    assert row["聊天内容"] is None
    assert row["附件"] == "=1+1.png\nhttps://example.test/file?token=original&x=1\n\n另一个文件.pdf\nhttps://example.test/two"
    assert "irrelevant-key" not in str(row) and "不应导出" not in str(row)
    assert all(cell.hyperlink is None for cells in workbook.active for cell in cells)
    with ZipFile(destination) as archive:
        assert not any(name.startswith("xl/media/") or name.startswith("xl/externalLinks/") for name in archive.namelist())


def test_non_body_control_characters_and_encoding_prefix_are_also_reversible():
    values = {"sender_name": "[ICS-ESC]" + r"literal\u0000", "customer_name": "客户\udfff\uffff"}
    _, workbook, _ = _export({"messages": [_message(**values)]})
    row = _rows(workbook.active)[0]
    for key, label in (("sender_name", "发送人"), ("customer_name", "客户")):
        assert _restore(row[label]) == values[key]


@pytest.mark.parametrize("value,expected", [
    (NOW, "2026-10-16 09:02:03.456789"),
    ("2026-10-16T00:30:00-05:00", "2026-10-16 13:30:00"),
    ("2026-10-16T00:30:00Z", "2026-10-16 08:30:00"),
    ("2026-10-16T12:30:00", "2026-10-16 12:30:00"),
    (None, None), ("unrecognized time", "unrecognized time"),
])
def test_chat_times_use_beijing_and_unknown_times_stay_empty(value, expected):
    _, workbook, _ = _export({"messages": [_message(sent_at=value)]})
    assert _rows(workbook.active)[0]["发送时间（北京时间）"] == expected


@pytest.mark.parametrize("role,label", [("contact", "客户"), ("agent", "客服"), ("bot", "机器人"), ("system", "系统"), (None, "未知")])
def test_roles_and_private_deleted_messages_are_readable(role, label):
    _, workbook, _ = _export({"messages": [_message(sender_role=role, is_private=True, is_deleted=True)]})
    row = _rows(workbook.active)[0]
    assert row["发送人类型"] == label and row["消息标记"] == "内部备注、已删除或撤回"
    assert row["聊天内容"] == "完整原文"


def test_missing_names_fall_back_to_ids_or_role_without_inventing_names():
    _, workbook, _ = _export({"messages": [
        _message(sender_name=None, customer_name=None, contact_id="0012"),
        _message(sender_name=None, sender_id=None, sender_role="bot"),
    ]})
    first, second = _rows(workbook.active)
    assert first["发送人"] == "0001234567890123456789" and first["客户"] == "0012"
    assert second["发送人"] == "机器人" and second["客户"] == "customer-1"


def test_oversized_unrelated_metadata_is_not_exported_or_processed():
    _, workbook, _ = _export({"rounds": [{"agents": [{"agent_name": "敏感坐席" * 9000}]}],
                             "customers": [{"display_name": "敏感信息" * 9000}],
                             "messages": [_message(snapshot_id="x" * 40000)]},
                            filters={"keyword": "敏感关键词" * 9000})
    assert workbook.sheetnames == ["聊天记录"] and workbook.active.max_row == 2
    assert "敏感" not in str(list(workbook.active.values))


def test_unlinked_ratings_use_dedicated_sheet_and_keep_feedback_as_text():
    ratings = Once([{"source_key": "s", "account_id": LARGE_ID, "conversation_id": LARGE_ID, "rating_key": "k", "status": "unlinked",
                     "rating": 1, "feedback": "=私密评价", "source_time": NOW, "snapshot_id": LARGE_ID, "event_id": LARGE_ID}])
    _, workbook, stats = _export({"ratings": ratings}, mode="unlinked-ratings")
    assert workbook.sheetnames == ["待关联评价"] and stats["ratings"] == 1 and ratings.iterations == 1
    row = _rows(workbook.active)[0]
    assert row["账号ID"] == str(LARGE_ID) and row["事件ID"] == str(LARGE_ID)
    assert row["评价备注"] == "=私密评价" and row["评价状态"] == "待关联" and row["轮次ID"] is None
    assert row["评价时间（北京时间）"] == "2026-10-16 09:02:03.456789+08:00"


@pytest.mark.parametrize("dataset,mode", [
    ({"messages": [_message(sender_name="敏感坐席" * 9000)]}, "rounds"),
    ({"ratings": [{"feedback": "敏感评价" * 9000}]}, "unlinked-ratings"),
    ({"ratings": [{"feedback": "😀" * 16384}]}, "unlinked-ratings"),
    ({"messages": [_message(attachments=[{"source_url": "https://example.test/" + "x" * 32768}])]}, "customers"),
], ids=["sender-name", "rating-feedback", "rating-emoji", "attachment-url"])
def test_oversized_non_body_fields_fail_explicitly_without_leaking_content(dataset, mode):
    with pytest.raises(excel.ExcelCellLimitExceeded) as caught:
        _export(dataset, mode=mode)
    assert isinstance(caught.value, excel.ExcelLimitExceeded)
    assert "长度上限" in str(caught.value) and "敏感" not in str(caught.value)


def test_row_limit_includes_header_and_never_silently_drops_rows(monkeypatch):
    monkeypatch.setattr(excel, "MAX_EXCEL_ROWS", 40)
    _, workbook, stats = _export({"messages": (_message(message_id=index) for index in range(39))})
    assert workbook.active.max_row == 40 and stats["messages"] == 39
    with pytest.raises(excel.ExcelRowLimitExceeded, match="行数上限") as caught:
        _export({"messages": (_message(message_id=index) for index in range(40))})
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


@pytest.mark.parametrize("ai_status", ["passed", "failed", "pending"])
@pytest.mark.parametrize("human_status", ["passed", "failed", "pending"])
def test_independent_ai_and_human_verdicts_share_the_chat_sheet(ai_status, human_status):
    note = '=原文备注\n复核说明'
    _, workbook, _ = _export({"messages": [_message(ai_review_status=ai_status,
        human_review_status=human_status, review_remark=note, round_no=1, customer_round_seq=4),
        _message(round_id=None, customer_round_seq=9)]})
    rows = _rows(workbook.active)
    labels = {"passed": "合格", "failed": "不合格", "pending": "待定"}
    assert rows[0]["AI质检"] == labels[ai_status]
    assert rows[0]["人工质检"] == labels[human_status]
    assert rows[0]["质检备注"] == note
    assert rows[1]["AI质检"] is None and rows[1]["人工质检"] is None
    # 会话内轮次与用户累计咨询分列；未归轮消息不显示累计次序。
    assert rows[0]["会话内轮次"] == 1 and rows[0]["用户累计咨询"] == 4
    assert rows[1]["用户累计咨询"] is None
    assert workbook.active["N2"].data_type == "s"
    assert workbook.active.auto_filter.ref == "A1:N3"


def test_pure_ai_chat_export_leaves_human_review_blank():
    _, workbook, _ = _export({"messages": [_message(reception_type="ai_only",
        ai_review_status="passed", human_review_status=None)]})
    row = _rows(workbook.active)[0]
    assert row["AI质检"] == "合格"
    assert row["人工质检"] is None
