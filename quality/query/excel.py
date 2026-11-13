"""Streaming, formula-free Excel exports of archived observations.

``build_workbook`` consumes each selected dataset iterable once and writes to an
already-open binary destination. It returns record counts and sheet names. A
logical message may occupy several rows; concatenate its ordered 聊天内容 cells
before reversing the documented [ICS-ESC] encoding. No source files are fetched.

ExcelLimitExceeded aborts the export rather than silently truncating a worksheet
or a non-body field. The caller owns removal of any incomplete destination.
"""

from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import BinaryIO

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


MAX_EXCEL_ROWS = 1_048_576  # Includes the header.
MAX_CELL_UNITS = 32_767  # UTF-16 code units, including supplementary characters.
ESCAPE_PREFIX = "[ICS-ESC]"
BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")


class ExcelLimitExceeded(ValueError):
    """Safe, user-facing export limit error; never includes archived content."""


class ExcelRowLimitExceeded(ExcelLimitExceeded):
    """A worksheet would exceed Excel's total row limit."""


class ExcelCellLimitExceeded(ExcelLimitExceeded):
    """A non-body field cannot fit without losing text."""


class _EncodedText(str):
    """Text already escaped and split; do not escape its prefix a second time."""


_ENUMS = {
    "rating_status": {"not_invited": "未邀请评价", "pending": "待评价", "rated": "已评价", "unlinked": "待关联"},
    "sender_role": {"contact": "客户", "agent": "客服", "bot": "机器人", "system": "系统", "unknown": "未知"},
    "linkage": {"closed_at_or_invitation": "按关闭时间或评价邀请关联", "unknown": "归属未确认"},
    "review_status": {"passed": "合格", "failed": "不合格", "pending": "待定"},
}
_MODES = {"rounds", "customers", "unlinked-ratings"}
_HEADER_FONT = Font(name="Microsoft YaHei", size=11, bold=True, color="FFFFFF")
_BODY_FONT = Font(name="Microsoft YaHei", size=11, color="1F2937")
_HEADER_FILL = PatternFill("solid", fgColor="234E70")
_ALIGNMENT = Alignment(vertical="top", wrap_text=True)


def _text(value):
    return "" if value is None else str(value)


def _yes(value):
    return "未知" if value is None else "是" if value else "否"


def _enum(kind, value):
    if value is None or value == "":
        return "未知"
    return _ENUMS.get(kind, {}).get(value, f"未知（{value}）")


def _beijing(value):
    if value is None or value == "":
        return ""
    parsed = value
    if isinstance(parsed, str):
        try:
            parsed = datetime.fromisoformat(parsed.replace("Z", "+00:00"))
        except ValueError:
            return value  # Retain an unrecognized source value instead of inventing a date.
    if not isinstance(parsed, datetime):
        return _text(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING)
    return parsed.astimezone(BEIJING).isoformat(sep=" ", timespec="microseconds" if parsed.microsecond else "seconds")


def _needs_escape(character):
    number = ord(character)
    return ((number < 0x20 and character not in "\t\n\r") or
            0x7F <= number <= 0x9F or 0xD800 <= number <= 0xDFFF or number in (0xFFFE, 0xFFFF))


def _escape_text(value):
    # Ordinary text is unchanged, including leading =, +, -, @ and backslashes.
    # Prefix collisions are themselves encoded so decoding is unambiguous.
    if not value.startswith(ESCAPE_PREFIX) and not any(_needs_escape(character) for character in value):
        return value
    parts = [ESCAPE_PREFIX]
    for character in value:
        if character == "\\":
            parts.append("\\\\")
        elif _needs_escape(character):
            parts.append(f"\\u{ord(character):04X}")
        else:
            parts.append(character)
    return "".join(parts)


def _utf16_units(value):
    return len(value) + sum(ord(character) > 0xFFFF for character in value)


def _chunks(value):
    start = units = 0
    for index, character in enumerate(value):
        size = 2 if ord(character) > 0xFFFF else 1
        if units + size > MAX_CELL_UNITS:
            yield value[start:index]
            start, units = index, 0
        units += size
    yield value[start:]


def _cell(sheet, value, *, header=False):
    if isinstance(value, bool):
        value = _yes(value)
    # Large numeric values are also protected even if a future field is not
    # explicitly identified as an ID by a row builder.
    if isinstance(value, int) and abs(value) >= 10**15:
        value = str(value)
    is_number = isinstance(value, (int, float))
    if not is_number:
        value = str(value) if isinstance(value, _EncodedText) else _escape_text(_text(value))
        if _utf16_units(value) > MAX_CELL_UNITS:
            raise ExcelCellLimitExceeded("非正文文本超过 Excel 单元格长度上限，请缩小导出范围后重试；本次未生成完整文件。")
    cell = WriteOnlyCell(sheet, value=value)
    if not is_number:
        cell.data_type = "s"  # Explicit text defeats formula and hyperlink injection.
        cell.number_format = "@"
    else:
        cell.number_format = "#,##0" if isinstance(value, int) else "0.##########"
    cell.font = _HEADER_FONT if header else _BODY_FONT
    cell.alignment = _ALIGNMENT
    if header:
        cell.fill = _HEADER_FILL
    return cell


class _Table:
    def __init__(self, workbook, title, columns, *, default_height=36):
        self.sheet = workbook.create_sheet(title)
        self.columns = columns
        self.rows = 0
        self.sheet.freeze_panes = "A2"
        self.sheet.sheet_view.showGridLines = False
        self.sheet.sheet_format.defaultRowHeight = default_height
        self.sheet.row_dimensions[1].height = 30
        for index, (_, width) in enumerate(columns, 1):
            self.sheet.column_dimensions[get_column_letter(index)].width = width
        self.append([name for name, _ in columns], header=True)

    def append(self, values, *, header=False):
        if self.rows >= MAX_EXCEL_ROWS:
            raise ExcelRowLimitExceeded("导出超过 Excel 单工作表行数上限，请缩小筛选范围后重试；本次未生成完整文件。")
        cells = [_cell(self.sheet, value, header=header) for value in values]
        self.sheet.append(cells)
        self.rows += 1

    def finish(self):
        self.sheet.auto_filter.ref = f"A1:{get_column_letter(len(self.columns))}{self.rows}"


_MESSAGE_COLUMNS = [
    ("客户", 22), ("发送时间（北京时间）", 28), ("发送人", 22), ("发送人类型", 14),
    ("聊天内容", 85), ("会话ID", 22), ("消息ID", 22), ("消息标记", 20), ("附件", 65),
    ("会话内轮次", 12), ("用户累计咨询", 14), ("AI质检", 12), ("人工质检", 12), ("质检备注", 55),
]
_RATING_COLUMNS = [
    ("实例来源", 20), ("账号ID", 20), ("会话ID", 20), ("评价键", 28), ("轮次ID", 25),
    ("邀请消息ID", 22), ("外部轮次ID", 25), ("评分", 10), ("评价备注", 80), ("评价状态", 18),
    ("归属依据", 30), ("评价时间（北京时间）", 29), ("轮次关闭时间（北京时间）", 29), ("快照ID", 22), ("事件ID", 22),
]


def _attachment_text(attachments):
    # Export only information useful for reading the conversation, never raw JSON.
    files = []
    for attachment in attachments or ():
        name = _text(attachment.get("filename"))
        # 非图片/音视频不导出地址，只标注已拦截。
        url = "（非图片/视频文件，已安全拦截，不提供地址）" if attachment.get("blocked") else _text(attachment.get("source_url"))
        files.append("\n".join(value for value in (name, url) if value))
    return "\n\n".join(value for value in files if value)


def _message_values(row):
    role = _enum("sender_role", row.get("sender_role"))
    marks = []
    if row.get("is_private"):
        marks.append("内部备注")
    if row.get("is_deleted"):
        marks.append("已删除或撤回")
    return [
        row.get("customer_name") or _text(row.get("contact_id")) or _text(row.get("customer_id")),
        _beijing(row.get("sent_at")).removesuffix("+08:00"),
        row.get("sender_name") or _text(row.get("sender_id")) or role,
        role, None, _text(row.get("conversation_id")), _text(row.get("message_id")),
        "、".join(marks), _attachment_text(row.get("attachments")),
        row.get("round_no"),
        # 会话内轮次只在单个 Chatwoot 会话中递增；用户累计咨询按该客户全部轮次排序。
        row.get("customer_round_seq") if row.get("round_id") else None,
        _enum("review_status", row.get("ai_review_status") or "pending") if row.get("round_id") else "",
        _enum("review_status", row.get("human_review_status") or "pending")
        if row.get("round_id") and row.get("reception_type") != "ai_only" else "",
        row.get("review_remark") or "",
    ]


def _rating_values(row):
    return [
        row.get("source_key"), _text(row.get("account_id")), _text(row.get("conversation_id")), row.get("rating_key"), _text(row.get("round_id")),
        _text(row.get("invitation_message_id")), _text(row.get("external_round_id")), row.get("rating"), row.get("feedback"),
        _enum("rating_status", row.get("status")), _enum("linkage", row.get("linkage")),
        _beijing(row.get("rating_at") if row.get("rating_at") is not None else row.get("source_time")),
        _beijing(row.get("closed_at")), _text(row.get("snapshot_id")), _text(row.get("event_id")),
    ]


def _dispose(workbook):
    # openpyxl's public Workbook.close() alone does not remove write-only spool
    # files after an aborted export. Close generators and remove their spools.
    for sheet in workbook.worksheets:
        with suppress(Exception):
            sheet.close()
        writer = getattr(sheet, "_writer", None)
        if writer is not None:
            with suppress(Exception):
                writer.cleanup()
    with suppress(Exception):
        workbook.close()


def build_workbook(destination: BinaryIO, dataset: dict, *, filters: dict, exported_at: datetime, mode: str) -> dict:
    """Write a complete XLSX and return counts plus ``sheets: list[str]``.

    Valid modes: rounds, customers, unlinked-ratings. Input rows are flattened
    API dictionaries; only the iterables relevant to the selected mode are read.
    The destination remains open. ExcelLimitExceeded is safe for API responses.
    """
    if mode not in _MODES:
        raise ValueError("Unsupported archive export mode")
    workbook = Workbook(write_only=True)
    stats = {"rounds": 0, "customers": 0, "messages": 0, "message_rows": 0, "ratings": 0}
    try:
        if mode == "unlinked-ratings":
            # Retained for the legacy API; the chat UI has no rating export entry.
            table = _Table(workbook, "待关联评价", _RATING_COLUMNS)
            for row in dataset.get("ratings", ()):
                table.append(_rating_values(row))
                stats["ratings"] += 1
        else:
            table = _Table(workbook, "聊天记录", _MESSAGE_COLUMNS, default_height=48)
            for row in dataset.get("messages", ()):
                body = _escape_text(_text(row.get("content")))
                values = _message_values(row)
                for chunk in _chunks(body):
                    values[4] = _EncodedText(chunk)
                    table.append(values)
                    stats["message_rows"] += 1
                stats["messages"] += 1
        table.finish()
        stats["sheets"] = workbook.sheetnames[:]
        workbook.save(destination)
        return stats
    finally:
        _dispose(workbook)
