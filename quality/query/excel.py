"""Streaming, formula-free Excel exports of archived observations.

``build_workbook`` consumes each selected dataset iterable once and writes to an
already-open binary destination. It returns record counts and sheet names. A
logical message may occupy several rows; concatenate its ordered 正文分段 cells
before reversing the documented [ICS-ESC] encoding. No source files are fetched.

ExcelLimitExceeded aborts the export rather than silently truncating a worksheet
or a non-body field. The caller owns removal of any incomplete destination.
"""

from contextlib import suppress
from datetime import date, datetime, timedelta, timezone
import json
from typing import BinaryIO

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from .responsibility import normalize_responsibility


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
    "reception_type": {"ai_only": "纯 AI 接待", "human": "人工介入", "unknown": "待确认"},
    "reply_composition": {"ai_only": "仅 AI 回复", "agent_only": "仅人工回复", "mixed": "AI 与人工回复", "none": "无回复"},
    "status": {"active": "未结束", "resolved": "已解决", "idle_boundary": "超时结束", "unknown": "未知", "unlinked": "待关联"},
    "rating_status": {"not_invited": "未邀请评价", "pending": "待评价", "rated": "已评价", "unlinked": "待关联"},
    "sender_role": {"contact": "客户", "agent": "客服", "bot": "机器人", "system": "系统", "unknown": "未知"},
    "direction": {"incoming": "入站", "outgoing": "出站", "activity": "活动消息", "unknown": "未知"},
    "handoff_result": {"requested": "已请求转人工", "completed": "已接入人工", "failed": "转人工失败", "unknown": "未知"},
    "identity_status": {"verified": "已验证业务身份", "contact": "按联系人识别", "unknown": "身份待确认"},
    "timestamp_status": {"known": "已知", "unknown": "未知"},
    "linkage": {"closed_at_or_invitation": "按关闭时间或评价邀请关联", "unknown": "归属未确认"},
    "agent_scope": {"responsible": "本轮责任坐席（ICS分配）", "replied": "本轮全部回复坐席", "assigned": "历史分配（非责任）", "current": "会话当前分配（非责任）"},
    "responsibility_basis": {"ics_assignment": "本轮 ICS 原始分配", "assignment_unobserved": "未记录 ICS 分配", "assignment_conflict": "ICS 分配证据冲突，待确认"},
    "time_field": {"started_at": "轮次开始时间", "resolved_at": "解决时间", "rating_at": "评价时间", "message_at": "消息时间"},
    "message_scope": {"all": "全部消息", "private": "私密备注", "contact": "客户", "agent": "客服", "bot": "机器人", "system": "系统", "unknown": "未知发送人"},
}
_FILTER_NAMES = {
    "source_key": "实例来源", "account_id": "账号 ID", "channel_key": "ICS 业务渠道", "inbox_id": "Chatwoot 收件箱 ID",
    "customer_query": "用户检索", "customer_id": "用户 ID", "conversation_id": "会话 ID",
    "round_id": "轮次 ID", "round_no": "轮次序号", "reception_type": "接待类型",
    "agent_id": "坐席 ID", "agent_scope": "坐席范围", "time_field": "时间口径",
    "start": "开始时间", "end": "结束时间", "rating_status": "评价状态", "rating": "评分",
    "keyword": "消息关键词", "message_scope": "消息范围", "has_attachment": "包含附件", "status": "轮次状态",
}
_MODES = {"rounds": "按轮次导出", "customers": "按用户导出", "unlinked-ratings": "待关联评价导出"}
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


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=lambda item: item.isoformat() if isinstance(item, (date, datetime)) else str(item))


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
            raise ExcelCellLimitExceeded("非正文文本超过 Excel 单元格长度上限，请缩小导出范围或检查元数据后重试；本次未生成完整文件。")
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


_ROUND_COLUMNS = [
    ("实例来源", 20), ("账号ID", 20), ("渠道", 20), ("用户ID", 25), ("用户名称", 22),
    ("Chatwoot联系人ID", 22), ("业务用户ID", 24), ("会话ID", 20), ("轮次ID", 25), ("轮次序号", 12),
    ("开始时间（北京时间）", 29), ("最后消息时间（北京时间）", 29), ("解决时间（北京时间）", 29),
    ("轮次关闭时间（北京时间）", 29), ("接待类型", 18), ("轮次已结束", 14), ("回复构成", 20), ("轮次状态", 16),
    ("责任坐席ID", 22), ("责任坐席名称", 24), ("责任依据", 32), ("ICS分配时间（北京时间）", 29),
    ("ICS分配事件ID", 22), ("ICS业务轮次ID", 35), ("ICS分配触发消息ID", 22),
    ("最后实际人工回复人ID（非责任）", 32), ("最后实际人工回复人名称（非责任）", 35),
    ("最后实际人工回复时间（北京时间）", 32), ("最后实际人工回复消息ID", 25),
    ("轮内最后分配坐席ID（非责任）", 30), ("轮内最后分配坐席名称（非责任）", 30), ("实际回复坐席", 35), ("曾经分配坐席（非责任）", 35), ("坐席记录", 70),
    ("转人工结果", 20), ("转人工原因", 40), ("请求转人工时间（北京时间）", 29), ("接入人工时间（北京时间）", 29),
    ("客户消息数", 14), ("机器人消息数", 14), ("客服消息数", 14), ("私密备注数", 14), ("附件数", 12),
    ("未捕获创建事件的消息数", 24), ("评价状态", 18), ("评分", 10), ("评价反馈", 60), ("评价时间（北京时间）", 29),
]
_CUSTOMER_COLUMNS = [
    ("实例来源", 20), ("账号ID", 20), ("用户ID", 25), ("用户名称", 22), ("Chatwoot联系人ID", 22),
    ("业务用户ID", 24), ("身份状态", 22), ("未验证身份标识", 30),
    ("首次消息时间（北京时间）", 29), ("最后消息时间（北京时间）", 29), ("轮次数", 12), ("逻辑消息数", 14),
]
_MESSAGE_COLUMNS = [
    ("实例来源", 20), ("账号ID", 20), ("渠道", 20), ("用户ID", 25), ("用户名称", 22),
    ("Chatwoot联系人ID", 22), ("业务用户ID", 24), ("会话ID", 20), ("轮次ID", 25), ("轮次序号", 12), ("消息ID", 22),
    ("发送时间（北京时间）", 29), ("时间状态", 14), ("发送人ID", 22), ("发送人名称", 22),
    ("发送人类型", 16), ("消息方向", 16), ("私密消息", 12), ("内容类型", 20), ("已删除或撤回", 16),
    ("捕获创建事件", 16), ("正文来自删除前观察", 22), ("正文观察状态", 22),
    ("分段序号", 12), ("分段总数", 12), ("正文分段", 85), ("附件元数据及原始URL", 80),
    ("原始快照ID", 22), ("最新快照ID", 22), ("原始事件ID", 22), ("最新事件ID", 22),
]
_RATING_COLUMNS = [
    ("实例来源", 20), ("账号ID", 20), ("会话ID", 20), ("评价键", 28), ("轮次ID", 25),
    ("邀请消息ID", 22), ("外部轮次ID", 25), ("评分", 10), ("评价备注", 80), ("评价状态", 18),
    ("归属依据", 30), ("评价时间（北京时间）", 29), ("轮次关闭时间（北京时间）", 29), ("快照ID", 22), ("事件ID", 22),
]


def _channel(value):
    return "未映射渠道" if value is None else value


def _agents(agents):
    replied, assigned, records = [], [], []
    for agent in agents or ():
        identity = f"{_text(agent.get('agent_name'))}（ID：{_text(agent.get('agent_id'))}）"
        if agent.get("replied"):
            replied.append(identity)
        if agent.get("was_assigned"):
            assigned.append(identity)
        records.append({
            "坐席ID": _text(agent.get("agent_id")), "姓名": agent.get("agent_name"),
            "曾经分配": _yes(agent.get("was_assigned")), "实际回复": _yes(agent.get("replied")),
            "私密备注": _yes(agent.get("noted")), "回复数": agent.get("reply_count"),
            "首次回复时间": _beijing(agent.get("first_reply_at")), "末次回复时间": _beijing(agent.get("last_reply_at")),
        })
    return "\n".join(replied), "\n".join(assigned), _json(records)


def _round_values(row):
    row = normalize_responsibility(row)
    counts = row.get("counts") or {}
    replied, assigned, agents = _agents(row.get("agents"))
    reception = _enum("reception_type", row.get("reception_type"))
    return [
        row.get("source_key"), _text(row.get("account_id")), _channel(row.get("channel_key")), _text(row.get("customer_id")), row.get("customer_name"),
        _text(row.get("contact_id")), _text(row.get("business_user_id")), _text(row.get("conversation_id")), _text(row.get("round_id")), row.get("round_no"),
        _beijing(row.get("started_at")), _beijing(row.get("last_message_at")), _beijing(row.get("resolved_at")), _beijing(row.get("closed_at")),
        reception, _yes(row.get("classification_finalized")), _enum("reply_composition", row.get("reply_composition")), _enum("status", row.get("status")),
        _text(row.get("responsible_agent_id")), row.get("responsible_agent_name"), _enum("responsibility_basis", row.get("responsibility_basis")),
        _beijing(row.get("responsible_assigned_at")), _text(row.get("responsibility_event_id")),
        _text(row.get("responsibility_external_round_id")), _text(row.get("responsibility_trigger_message_id")),
        _text(row.get("last_public_agent_id")), row.get("last_public_agent_name"),
        _beijing(row.get("last_public_reply_at")), _text(row.get("last_public_message_id")),
        _text(row.get("current_assignee_id")), row.get("current_assignee_name"), replied, assigned, agents,
        _enum("handoff_result", row.get("handoff_result")), row.get("handoff_reason"), _beijing(row.get("handoff_requested_at")), _beijing(row.get("handoff_completed_at")),
        counts.get("contact"), counts.get("bot"), counts.get("agent"), counts.get("private"), counts.get("attachments"),
        row.get("observed_only_message_count"), _enum("rating_status", row.get("rating_status")), row.get("rating"), row.get("rating_feedback"), _beijing(row.get("rating_at")),
    ]


def _customer_values(row):
    return [
        row.get("source_key"), _text(row.get("account_id")), _text(row.get("customer_id")), row.get("display_name"), _text(row.get("contact_id")),
        _text(row.get("business_user_id")), _enum("identity_status", row.get("identity_status")), row.get("unverified_identifier"),
        _beijing(row.get("first_message_at")), _beijing(row.get("last_message_at")), row.get("round_count"), row.get("message_count"),
    ]


def _message_values(row):
    content = row.get("content")
    observed = "未观察到正文" if content is None else "空文本" if content == "" else "已观察到正文"
    return [
        row.get("source_key"), _text(row.get("account_id")), _channel(row.get("channel_key")), _text(row.get("customer_id")), row.get("customer_name"),
        _text(row.get("contact_id")), _text(row.get("business_user_id")), _text(row.get("conversation_id")), _text(row.get("round_id")), row.get("round_no"), _text(row.get("message_id")),
        _beijing(row.get("sent_at")), _enum("timestamp_status", row.get("timestamp_status")), _text(row.get("sender_id")), row.get("sender_name"),
        _enum("sender_role", row.get("sender_role")), _enum("direction", row.get("direction")), _yes(row.get("is_private")), row.get("content_type"),
        _yes(row.get("is_deleted")), _yes(row.get("has_creation_event")), _yes(row.get("content_is_pre_delete")), observed,
    ]


def _rating_values(row):
    return [
        row.get("source_key"), _text(row.get("account_id")), _text(row.get("conversation_id")), row.get("rating_key"), _text(row.get("round_id")),
        _text(row.get("invitation_message_id")), _text(row.get("external_round_id")), row.get("rating"), row.get("feedback"),
        _enum("rating_status", row.get("status")), _enum("linkage", row.get("linkage")),
        _beijing(row.get("rating_at") if row.get("rating_at") is not None else row.get("source_time")),
        _beijing(row.get("closed_at")), _text(row.get("snapshot_id")), _text(row.get("event_id")),
    ]


def _explanation(table, filters, exported_at, mode, stats):
    table.append(["导出类型", _MODES[mode], mode])
    table.append(["导出时间（北京时间）", _beijing(exported_at), exported_at.isoformat()])
    scope = {
        "rounds": "包含所选轮次的全部已归档消息，不限当前列表分页。列表消息条件用于选择轮次，不再次裁剪轮次内消息。",
        "customers": "包含所选用户的完整已归档历史。列表筛选用于选择用户，用户历史不再次套用时间、关键词等列表筛选，不限当前分页。",
        "unlinked-ratings": "仅包含待关联评价，不把无法确认归属的评价自动分配到某个轮次，不限当前分页。",
    }
    table.append(["数据范围", scope[mode], ""])
    if not filters:
        table.append(["筛选条件", "本次导出类型内无额外筛选", "{}"])
    for key, value in filters.items():
        if key in ("start", "end"):
            shown = _beijing(value)
        elif key in _ENUMS:
            shown = _enum(key, value)
        elif key == "has_attachment":
            shown = _yes(value)
        elif key == "channel_key" and value == "unmapped":
            shown = "未映射渠道"
        else:
            shown = _text(value)
        table.append(["筛选：" + _FILTER_NAMES.get(key, key), shown, value.isoformat() if isinstance(value, (date, datetime)) else _json(value)])
    for key, label in (("customers", "用户记录数"), ("rounds", "轮次记录数"), ("messages", "逻辑消息数"), ("message_rows", "消息分段行数"), ("ratings", "待关联评价数")):
        table.append([label, stats[key], "不含表头"])
    table.append(["数据行总数", stats["customers"] + stats["rounds"] + stats["message_rows"] + stats["ratings"], "消息正文分段后逐行计数，不含表头及导出说明"])
    notes = [
        ("原始记录口径", "数据是已归档的观察记录。未捕获的原始创建、编辑前或删除前内容无法通过本次导出重建。创建事件、删除前正文和未知时间分别标明。"),
        ("时间口径", "可识别的时间统一显示北京时间（Asia/Shanghai，UTC+08:00）。无时区时间按北京时间解释；无法识别的源时间保留原值，缺失时间留空。"),
        ("消息身份", "按实例来源、账号ID、消息ID共同识别消息。ID 均按文本保存，保留大整数及前导零；用户不会按同名自动合并。"),
        ("长正文还原", "正文按不超过 32767 个 UTF-16 单元拆成多行，表中分段序号从 1 开始。按完整消息身份及分段序号拼接正文分段，再按文本转义规则解码。其他列在分段行中重复。"),
        ("文本转义", "普通文本原样保存。含 XML 非法字符、控制字符或以 [ICS-ESC] 开头的文本带 [ICS-ESC] 前缀。解码时先移除此前缀，再从左到右将双反斜杠还原为单反斜杠、\\uXXXX 还原为对应 Unicode 字符。制表、换行、回车保留。"),
        ("公式与链接", "所有外来字符串显式保存为文本，包括以 =、+、-、@ 开头的内容。文件不含公式，不对原文添加单引号。附件仅导出元数据和原始 URL，不下载文件，不生成超链接公式。"),
        ("长度与行数上限", "单工作表最多 1048576 行（含表头）。超出行数，或非正文文本超出 32767 个 UTF-16 单元时，整次导出明确失败，不截断、不返回成功的部分文件。"),
        ("接待统计口径", "未触发转人工的轮次立即计为纯 AI 接待；本轮出现转人工请求、确认接管或人工公开回复后改为人工介入，不等待轮次结束。转人工结果仍区分请求与已接入。"),
        ("责任坐席口径", "本轮责任坐席固定为 ICS 成功分配时记录的坐席；后续 Chatwoot 改派、他人回复或消息编辑均不改变责任人。缺少有出处的 ICS 分配记录或证据冲突时留空，不以 Chatwoot 当前坐席、最后回复人或旧评价卡补齐。实际发送人、消息正文及回复历史按原始观察记录分别导出；分配时间未知时留空。"),
        ("空值与未知", "未观察到的 ID、日期与指标留空；渠道未识别时明确显示未映射渠道。轮次是否结束单独记录，不影响接待分类。"),
    ]
    for label, text in notes:
        table.append([label, text, ""])


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
        explanation = _Table(workbook, "导出说明", [("条目", 28), ("说明或数值", 105), ("原始筛选值", 65)], default_height=45)
        tables = [explanation]
        if mode == "unlinked-ratings":
            ratings = _Table(workbook, "待关联评价", _RATING_COLUMNS)
            tables.append(ratings)
            for row in dataset.get("ratings", ()):
                ratings.append(_rating_values(row))
                stats["ratings"] += 1
        else:
            if mode == "customers":
                customers = _Table(workbook, "用户概览", _CUSTOMER_COLUMNS)
                tables.append(customers)
                for row in dataset.get("customers", ()):
                    customers.append(_customer_values(row))
                    stats["customers"] += 1
            rounds = _Table(workbook, "轮次概览", _ROUND_COLUMNS)
            tables.append(rounds)
            for row in dataset.get("rounds", ()):
                rounds.append(_round_values(row))
                stats["rounds"] += 1
            messages = _Table(workbook, "消息明细", _MESSAGE_COLUMNS, default_height=48)
            tables.append(messages)
            for row in dataset.get("messages", ()):
                body = _escape_text(_text(row.get("content")))
                segments = sum(1 for _ in _chunks(body))
                head = _message_values(row)
                tail = [_json(row.get("attachments") or []), _text(row.get("original_snapshot_id")), _text(row.get("latest_snapshot_id")),
                        _text(row.get("original_event_id")), _text(row.get("latest_event_id"))]
                for index, chunk in enumerate(_chunks(body), 1):
                    messages.append(head + [index, segments, _EncodedText(chunk)] + tail)
                    stats["message_rows"] += 1
                stats["messages"] += 1
        _explanation(explanation, filters, exported_at, mode, stats)
        for table in tables:
            table.finish()
        stats["sheets"] = workbook.sheetnames[:]
        workbook.save(destination)
        return stats
    finally:
        _dispose(workbook)
