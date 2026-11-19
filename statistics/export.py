"""统计报表 Excel 导出：与页面同一份数据，排版对齐页面。

工作表：
- 数据表：标题、统计周期与筛选、统计概况，再依次放页面上的各张表（含合计行）。时间统计、质检统计、用户统计各一张；
  坐席细化三张表形状差异大（坐席 × 指标、坐席 × 时段、分 × 时），拆为“坐席细化”“坐席回复询盘”“可用客服量”，
  各表列宽只为自己的表服务。
- “口径说明”：页面上的数据提示、各字段“?”说明与统计规则（statistics/glossary.py）。

排版：每张数据表有一个“内容宽度”（主表的列数），标题、概况卡片、版块条、说明与图表都对齐到这一宽度；
卡片按列宽均分铺满；图表两角锚定在单元格上，正好占满预留区域（不再浮在数据右侧留空）；列宽按内容定。

数值保持可计算：计数为整数，时长为 Excel 时间值（[h]:mm:ss），比率为百分比；缺失或未来时段显示“—”，
真实 0 仍为 0。文本单元格一律按文本写入（防公式注入），不写公式。
"""

from copy import copy
from datetime import datetime, timezone
from decimal import Decimal
import os
from pathlib import Path
import tempfile
import time
import unicodedata

from quality.query.export import ExportCancelled, ExportFile, ExportTooLarge
from . import glossary
from .repository import StatisticsRepository, classify, report_budget


TITLES = {"time": "时间统计", "agents": "坐席细化", "quality": "质检统计", "users": "用户统计"}
TIME_FIELDS = (("incoming", "进客量"), ("ai_only", "纯AI接待量"), ("handoff", "转人工量"),
               ("rejected", "转人工拒接量"), ("effective_human", "有效人工量"), ("low_rating", "低评分数（低于4分）"))
# (字段, 页面列名, 类型, 说明键)；类型：count 计数、duration 时长、decimal 两位小数、rate 百分比。
SEAT_FIELDS = (("first_response_seconds", "首响时长", "duration", "first_response"),
               ("average_response_seconds", "平均响应时长", "duration", "average_response"),
               ("dialogues", "对话量", "count", "dialogues"),
               ("high_quality_dialogues", "高质量对话量", "count", "high_quality_dialogues"),
               ("assigned_inquiries", "发配量", "count", "assigned_inquiries"),
               ("inquiries", "回复询盘总量", "count", "inquiries"),
               ("average_rating", "平均评分", "decimal", "average_rating"),
               ("low_rating", "低分数（低于4分）", "count", "low_rating"),
               ("lost_inquiries", "流单数（转人工后未回）", "count", "lost_inquiries"),
               ("consultation_seconds", "咨询状态总时长", "duration", "consultation_seconds"))
HUMAN_FIELDS = (("assigned_inquiries", "发配量", "count", "assigned_inquiries"),
                ("dialogues", "对话量", "count", "dialogues"),
                ("high_quality_dialogues", "高质量对话量", "count", "high_quality_dialogues"),
                ("inquiries", "回复询盘总量", "count", "inquiries"),
                ("failed_inquiries", "质检不合格量", "count", "failed_inquiries"),
                ("failure_rate", "不合格率", "rate", "human_failure_rate"))
AI_FIELDS = (("total_rounds", "所有对话轮数", "count"), ("handoff", "转人工量", "count"), ("ai_only", "纯AI回复量", "count"),
             ("handoff_failed", "转人工失败量", "count"), ("failed_rounds", "不合格量", "count"),
             ("failure_rate", "不合格率", "rate"))
BLACKLIST_LABELS = {"active": "拉黑中", "past": "曾被拉黑"}
MISSING = "—"
HELP_SHEET = "口径说明"
INFO_SEPARATOR = "　｜　"
AGENT_SHEETS = ("坐席回复询盘", "可用客服量")  # 坐席细化的第二、三张数据表
ROW_HEIGHT = 20     # 默认行高（磅）
CHART_ROWS = 16     # 折线图占用行数
CHART_COLUMNS = 10  # 左表右图时图表占用的列数

# ---------------------------------------------------------------- 样式
_FONT = "Microsoft YaHei"
_NAVY, _INK, _MUTED, _LINE = "1F3A5F", "1F2937", "6B7280", "D9DEE7"
_HEADER_FILL, _SECTION_FILL, _CARD_FILL, _ZEBRA_FILL, _TOTAL_FILL = "234E70", "E8EEF6", "F3F6FA", "F7F9FC", "E3EAF4"
_ALERT_FILL, _WARN_FILL = "FDE2E1", "FDEBD3"
NUMBER_FORMATS = {"count": "#,##0", "duration": "[h]:mm:ss", "decimal": "0.00", "rate": "0.00%", "people": "0.00"}


def _styles():
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    line = Side(style="thin", color=_LINE)
    box = Border(left=line, right=line, top=line, bottom=line)
    fill = lambda color: PatternFill("solid", fgColor=color)
    return {
        "title": dict(font=Font(name=_FONT, size=16, bold=True, color=_NAVY), alignment=Alignment(vertical="center")),
        "info": dict(font=Font(name=_FONT, size=10, color=_MUTED), alignment=Alignment(vertical="center", wrap_text=True)),
        "center": dict(font=Font(name=_FONT, size=10, color=_INK), border=box,
                       alignment=Alignment(horizontal="center", vertical="center")),
        "section": dict(font=Font(name=_FONT, size=12, bold=True, color=_NAVY), fill=fill(_SECTION_FILL),
                        alignment=Alignment(vertical="center"),
                        border=Border(left=Side(style="thick", color=_HEADER_FILL))),
        "section_fill": dict(fill=fill(_SECTION_FILL)),
        "header": dict(font=Font(name=_FONT, size=10, bold=True, color="FFFFFF"), fill=fill(_HEADER_FILL), border=box,
                       alignment=Alignment(horizontal="center", vertical="center", wrap_text=True)),
        "card_label": dict(font=Font(name=_FONT, size=9, color=_MUTED), fill=fill(_CARD_FILL), border=box,
                           alignment=Alignment(horizontal="center", vertical="center", wrap_text=True)),
        "card_value": dict(font=Font(name=_FONT, size=14, bold=True, color=_NAVY), fill=fill(_CARD_FILL), border=box,
                           alignment=Alignment(horizontal="center", vertical="center")),
        "text": dict(font=Font(name=_FONT, size=10, color=_INK), border=box,
                     alignment=Alignment(vertical="center", wrap_text=True)),
        "number": dict(font=Font(name=_FONT, size=10, color=_INK), border=box,
                       alignment=Alignment(horizontal="right", vertical="center")),
        "missing": dict(font=Font(name=_FONT, size=10, color="9CA3AF"), border=box,
                        alignment=Alignment(horizontal="center", vertical="center")),
        "zebra": dict(fill=fill(_ZEBRA_FILL)),
        "total": dict(font=Font(name=_FONT, size=10, bold=True, color=_NAVY), fill=fill(_TOTAL_FILL),
                      border=Border(left=line, right=line, top=Side(style="medium", color=_HEADER_FILL), bottom=line)),
        "alert_text": dict(font=Font(name=_FONT, size=10, bold=True, color="B42318")),
        "note": dict(font=Font(name=_FONT, size=9, color=_MUTED), alignment=Alignment(vertical="center")),
        "help_key": dict(font=Font(name=_FONT, size=10, bold=True, color=_INK), border=box,
                         alignment=Alignment(vertical="top", wrap_text=True)),
        "help_text": dict(font=Font(name=_FONT, size=10, color=_INK), border=box,
                          alignment=Alignment(vertical="top", wrap_text=True)),
    }


def _text_width(text):
    """Excel 列宽单位下的大致显示宽度：全角字符按 2、半角按 1。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in str(text or ""))


def _fit(texts, low, high, pad=2):
    """按内容定列宽（夹在 low～high 之间），避免短内容留白、长内容被折行。"""
    return max(low, min(high, max((_text_width(t) for t in texts), default=0) + pad))


def _partition(widths, parts):
    """把连续的列分成 parts 组，使各组总宽尽量相等；返回 (最大相对偏差, [(首列, 末列)])，列号从 1 开始。"""
    count, target = len(widths), sum(widths) / parts
    best = {}

    def solve(start, left):
        if left == 1:
            return abs(sum(widths[start:]) - target), [(start + 1, count)]
        key = (start, left)
        if key not in best:
            options = []
            for end in range(start + 1, count - left + 2):
                rest, groups = solve(end, left - 1)
                options.append((max(abs(sum(widths[start:end]) - target), rest), [(start + 1, end), *groups]))
            best[key] = min(options, key=lambda option: option[0])
        return best[key]

    deviation, groups = solve(0, parts)
    return deviation / target, groups


def card_layout(widths, count):
    """统计概况卡片排布：卡片铺满内容宽度、各卡宽度尽量一致；返回每行的 [(首列, 末列)]。"""
    columns, best = len(widths), None
    for rows in range(1, count + 1):
        per_row = -(-count // rows)
        if per_row > columns:
            continue
        sizes = [count // rows + (1 if index < count % rows else 0) for index in range(rows)]
        # 行数越少越好；各行卡片数不一致（如 3 + 2）看起来参差，额外扣分。
        score, layout = 0.12 * (rows - 1) + (0.25 if count % rows else 0), []
        for size in sizes:
            deviation, groups = _partition(widths, size)
            narrowest = min(sum(widths[first - 1:last]) for first, last in groups)
            score += deviation / rows + (1 if narrowest < 14 else 0)
            layout.append(groups)
        if best is None or score < best[0]:
            best = (score, layout)
        if rows >= 3:
            break
    return best[1]


class _Sheet:
    """写入式工作表：逐行写入并记录行号，统一按“内容宽度”排版标题、概况、版块条、图表与说明。

    widths 为全部列宽；content 为内容宽度（列数，默认全部列）——标题、概况卡片、版块条、说明与图表都对齐到这一宽度，
    不再各自向右或向下延伸。
    """

    def __init__(self, workbook, title, widths, check, freeze=None, content=None):
        from openpyxl.utils import get_column_letter

        self.sheet = workbook.create_sheet(title)
        self.check = check
        self.styles = _styles()
        self.style_cache = {}
        self.row = 0
        self.widths = list(widths)
        self.width = content or len(widths)
        self.letter = get_column_letter
        self.sheet.sheet_view.showGridLines = False
        self.sheet.sheet_format.defaultRowHeight = ROW_HEIGHT
        for index, width in enumerate(widths, 1):
            self.sheet.column_dimensions[get_column_letter(index)].width = width
        self.sheet.page_setup.orientation = "landscape"
        self.sheet.page_setup.paperSize = 9  # A4
        if sum(widths) <= 220:  # 不太宽的表打印时缩放到一页宽；很宽的表按原大小分页，避免字太小。
            self.sheet.page_setup.fitToWidth = 1
            self.sheet.page_setup.fitToHeight = 0
            self.sheet.sheet_properties.pageSetUpPr.fitToPage = True
        # 写入式工作表在写第一行时就输出视图与列宽，冻结窗格须在此设置。
        if freeze:
            self.sheet.freeze_panes = freeze

    @property
    def content_units(self):
        return sum(self.widths[:self.width])

    def lines(self, text, units):
        return max(1, -(-_text_width(text) // max(1, int(units * 0.92))))

    def cell(self, value, style="text", number_format=None, *extra):
        from openpyxl.cell import WriteOnlyCell
        from quality.query.excel import MAX_CELL_UNITS, ExcelCellLimitExceeded, _escape_text, _utf16_units

        is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
        if value is not None and not is_number:
            value = _escape_text(str(value))
            if _utf16_units(value) > MAX_CELL_UNITS:
                raise ExcelCellLimitExceeded("文本超过 Excel 单元格长度上限，请缩小导出范围后重试。")
        cell = WriteOnlyCell(self.sheet, value=value)
        if value is not None and not is_number:
            cell.data_type = "s"  # 明确为文本：用户名、坐席名等不会被当成公式或链接。
        # 同一组样式只计算一次，之后复制已登记的样式下标（大表导出时省去逐格查样式表）。
        key = (style, extra, number_format)
        cached = self.style_cache.get(key)
        if cached is None:
            for name in (style, *extra):
                for attribute, setting in self.styles[name].items():
                    setattr(cell, attribute, setting)
            if number_format:
                cell.number_format = number_format
            self.style_cache[key] = copy(cell._style)
        else:
            cell._style = copy(cached)
        return cell

    def append(self, cells, height=None):
        from quality.query.excel import MAX_EXCEL_ROWS, ExcelRowLimitExceeded

        self.check()
        if self.row >= MAX_EXCEL_ROWS:
            raise ExcelRowLimitExceeded("导出超过 Excel 单工作表行数上限，请缩小筛选范围后重试；本次未生成完整文件。")
        if height:  # 行高随该行一起写出，须在 append 之前设置。
            self.sheet.row_dimensions[self.row + 1].height = height
        self.sheet.append(cells)
        self.row += 1
        return self.row

    def blank(self, height=None):
        return self.append([], height)

    def merge(self, row, first, last):
        if last > first:
            self.sheet.merged_cells.add(f"{self.letter(first)}{row}:{self.letter(last)}{row}")

    def area(self, first_row, last_row, first_col, last_col):
        return f"{self.letter(first_col)}{first_row}:{self.letter(last_col)}{last_row}"

    def band(self, text, style, height=None):
        """一整行合并到内容宽度（标题、版块条、说明）；同一样式填满整段，边框与底色连续。"""
        cells = [self.cell(text, style)] + [self.cell(None, style) for _ in range(self.width - 1)]
        row = self.append(cells, height)
        self.merge(row, 1, self.width)
        return row

    # ------------------------------------------------------------ 版块
    def heading(self, title, info):
        """标题 + 筛选信息；信息放不下一行时在“｜”分隔处换行，不在词中间断开。"""
        self.band(title, "title", 30)
        parts, lines = info.split(INFO_SEPARATOR), []
        for part in parts:
            joined = f"{lines[-1]}{INFO_SEPARATOR}{part}" if lines else part
            if lines and _text_width(joined) <= self.content_units * 0.92:
                lines[-1] = joined
            else:
                lines.append(part)
        text = "\n".join(lines)
        self.band(text, "info", 16 * sum(self.lines(line, self.content_units) for line in lines) + 6)
        self.blank(8)

    def section(self, title):
        self.band(title, "section", 24)

    def note(self, text):
        self.band(text, "note", 15 * self.lines(text, self.content_units) + 5)

    def gap(self):
        self.blank(12)

    def cards(self, items):
        """统计概况：与页面卡片相同的名称与数值；卡片铺满内容宽度、宽度尽量一致，最多三行。"""
        self.section("统计概况")
        layout, start = card_layout(self.widths[:self.width], len(items)), 0
        for groups in layout:
            chunk = items[start:start + len(groups)]
            start += len(groups)
            labels, values = [], []
            for (first, last), (label, value, kind) in zip(groups, chunk):
                labels += [self.cell(label, "card_label")] + [self.cell(None, "card_label") for _ in range(last - first)]
                values += [self.value(value, kind, "card_value")] + [self.cell(None, "card_value")
                                                                     for _ in range(last - first)]
            label_lines = max(self.lines(label, sum(self.widths[first - 1:last]))
                              for (first, last), (label, _, _) in zip(groups, chunk))
            label_row, value_row = self.append(labels, 13 * label_lines + 10), self.append(values, 30)
            for first, last in groups:
                self.merge(label_row, first, last)
                self.merge(value_row, first, last)
        self.gap()

    def value(self, value, kind, style="number", *extra):
        """按类型写一个数值格；None 显示“—”。"""
        if value is None:
            return self.cell(MISSING, "missing" if style == "number" else style, None, *extra)
        if isinstance(value, Decimal):
            value = float(value)
        if kind == "duration":
            value = float(value) / 86400
        elif kind == "rate":
            value = float(value) / 100
        return self.cell(value, style, NUMBER_FORMATS[kind], *extra)

    def header_height(self, headers, first_col=1):
        """表头按列宽折行后的高度：同一表头行内各列一致，长列名不被截断。"""
        lines = max((self.lines(name, self.widths[first_col - 1 + index] - 1) for index, name in enumerate(headers)
                     if name), default=1)
        return max(24, 14 * lines + 10)

    def table(self, headers, rows, *, total=None, empty=None):
        """表头 + 数据（隔行底色）+ 可选合计行；返回 (首个数据行, 末个数据行)。rows 为单元格构造函数列表。"""
        self.append([self.cell(name, "header") for name in headers], self.header_height(headers))
        first = self.row + 1
        count = 0
        for count, build in enumerate(rows, 1):
            self.append(build(("zebra",) if count % 2 == 0 else ()))
        if not count and empty:
            self.merge(self.append([self.cell(empty, "missing")] + [self.cell(None, "missing")
                                                                     for _ in range(len(headers) - 1)]), 1, len(headers))
        last = first + count - 1
        if total:
            self.append(total(("total",)))
        return first, last

    def highlight(self, first, last, column, fill=_ALERT_FILL):
        """数据行中该列大于 0 的格子标浅色底（不改数值）。"""
        from openpyxl.formatting.rule import CellIsRule
        from openpyxl.styles import Font, PatternFill

        if last >= first:
            color = "B42318" if fill == _ALERT_FILL else "B54708"
            self.sheet.conditional_formatting.add(self.area(first, last, column, column), CellIsRule(
                operator="greaterThan", formula=["0"], fill=PatternFill("solid", fgColor=fill),
                font=Font(name=_FONT, size=10, bold=True, color=color)))

    def heat(self, first_row, last_row, first_col, last_col):
        from openpyxl.formatting.rule import ColorScaleRule

        if last_row >= first_row and last_col >= first_col:
            self.sheet.conditional_formatting.add(self.area(first_row, last_row, first_col, last_col), ColorScaleRule(
                start_type="num", start_value=0, start_color="FFFFFF", end_type="max", end_color="7FA7D9"))

    def bars(self, first, last, column):
        from openpyxl.formatting.rule import DataBarRule

        if last >= first:
            self.sheet.conditional_formatting.add(self.area(first, last, column, column), DataBarRule(
                start_type="num", start_value=0, end_type="max", color="9DB9E0", showValue=True))

    def chart_space(self, rows=CHART_ROWS):
        """为折线图预留的空行；返回 (首行, 行数)，图表正好铺在这块区域内。"""
        start = self.row + 1
        for _ in range(rows):
            self.blank(ROW_HEIGHT)
        return start, rows

    def line_chart(self, area, categories, series, *, y_title, columns=None, number_format="0"):
        """area: (首行, 行数)；columns: (首列, 末列)，默认内容宽度。图表两角锚定在单元格上，
        宽高随列宽行高精确铺满预留区域，任何缩放与分辨率下都不遮挡表格。
        categories: (列, 首行, 末行)；series: [(列, 表头行, 末行, 颜色)]。"""
        from openpyxl.chart import LineChart, Reference
        from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, TwoCellAnchor

        chart = LineChart()
        chart.title = None  # 版块标题已说明图名，图内不重复。
        chart.style = 2
        chart.y_axis.title = y_title
        chart.y_axis.number_format = number_format
        chart.y_axis.scaling.min = 0
        # 图例放在图表上方：放底部时会和倾斜的时间标签挤在一起重叠。
        chart.legend.position = "t"
        chart.legend.overlay = False
        for column, header_row, last_row, color in series:
            chart.add_data(Reference(self.sheet, min_col=column, min_row=header_row, max_row=last_row), titles_from_data=True)
            line = chart.series[-1]
            line.graphicalProperties.line.solidFill = color
            line.graphicalProperties.line.width = 22000
            line.smooth = False
        col, first, last = categories
        chart.set_categories(Reference(self.sheet, min_col=col, min_row=first, max_row=last))
        chart.x_axis.delete = False
        chart.y_axis.delete = False
        if len(series) == 1:
            chart.legend = None
        top, rows = area
        first_col, last_col = columns or (1, self.width)
        anchor = TwoCellAnchor(editAs="twoCell")
        anchor._from = AnchorMarker(col=first_col - 1, row=top - 1, colOff=0, rowOff=38100)
        # 右下角落在最后一列右缘、最后一行内（上下各留 3 磅），EMU：1 磅 = 12700。
        anchor.to = AnchorMarker(col=last_col, row=top - 2 + rows, colOff=0, rowOff=int(ROW_HEIGHT * 12700) - 38100)
        chart.anchor = anchor
        self.sheet.add_chart(chart)


# ---------------------------------------------------------------- 文本工具
def _beijing_seconds(value):
    from quality.query.excel import BEIJING

    if not value:
        return None
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S")


def period_label(report):
    """与页面状态栏相同的统计周期写法，后附精确起止（到秒，R10-13）。"""
    start, end = report["start_label"], report["end_label"]
    if report.get("refined") or report.get("mode") is None:
        short = None
    elif report["mode"] == "custom":
        short = f'{report["start_date"]} 至 {report["end_date"]}'
    elif report["mode"] == "day":
        short = report["date"]
    else:
        short = report["date"][:7] if report["mode"] == "month" else report["date"][:4]
    exact = f"{start} 至 {end[11:]}" if start[:10] == end[:10] else f"{start} 至 {end}"
    return f"{short}（{exact}）" if short else exact


def _info(kind, report, channel_name):
    parts = [f"统计周期：{period_label(report)}"]
    if kind in ("time", "users"):
        parts.append(f"渠道：{channel_name}")
    parts.append(f"数据生成：{_beijing_seconds(report['generated_at'])}")
    parts.append("北京时间，起止均含")
    return INFO_SEPARATOR.join(parts)


def _seat_name(agent):
    return agent.get("agent_name") or f'坐席 #{agent["agent_id"]}'


def _seat_id(agent):
    return f'{agent["source_key"]} / {agent["account_id"]} / #{agent["agent_id"]}'


def _weighted(agents, value_key, weight_key):
    weight = sum(int(a.get(weight_key) or 0) for a in agents)
    if not weight:
        return None
    return sum(float(a.get(value_key) or 0) * int(a.get(weight_key) or 0) for a in agents
               if a.get(value_key) is not None) / weight


# ---------------------------------------------------------------- 各报表
def _seat_widths(agents):
    """坐席名、“来源 / 账号 / 坐席ID”两列按实际内容定宽，不折行也不留大片空白。"""
    names = [_seat_name(a) for a in agents] + ["坐席", "合计"]
    ids = [_seat_id(a) for a in agents] + [f"全部 {len(agents)} 位坐席", "来源 / 账号"]
    return [_fit(names, 10, 24, 3), _fit(ids, 14, 36)]


def _time_book(workbook, check, info, report, period, names):
    fields = [*TIME_FIELDS, *[(f"channel:{key}", f"{name}进客量") for key, name in names.items()]]
    value = lambda row, key: row["channels"].get(key[8:], 0) if key.startswith("channel:") else row.get(key, 0)
    labels = [b["label"] + (" · 进行中" if m.get("partial") else "") for b, m in zip(period.buckets, report["buckets"])]
    sheet = _Sheet(workbook, TITLES["time"], [_fit([*labels, "时间区间"], 12, 24, 3)] + [12] * len(fields), check,
                   freeze="B1")
    sheet.heading(TITLES["time"], info)
    sheet.cards([(label, report["totals"][key], "count") for key, label in TIME_FIELDS])
    sheet.section("询盘趋势")
    area = sheet.chart_space()
    sheet.gap()
    sheet.section("时段数据")
    header_row = sheet.row + 1

    def row(label, meta, data):
        return lambda extra: [sheet.cell(label, "text", None, *extra),
                              *[sheet.value(None if meta["future"] else value(data, key), "count", "number", *extra)
                                for key, _ in fields]]

    rows = [row(label, m, d) for label, m, d in zip(labels, report["buckets"], report["rows"])]
    totals = {**report["totals"], "channels": report["channels"]}
    first, last = sheet.table(["时间区间", *[label for _, label in fields]], rows, total=lambda extra: [
        sheet.cell("合计", "text", None, *extra), *[sheet.value(value(totals, key), "count", "number", *extra)
                                                   for key, _ in fields]])
    sheet.highlight(first, last, 5, _WARN_FILL)   # 转人工拒接量
    sheet.highlight(first, last, 7)               # 低评分数
    colors = ["2F6BFF", "16A085", "8854D0", "E18422", "DB5479", "BA3428"]
    # 曲线只画到已发生的时段：未来时段是“—”，画进去会被 Excel 当作 0 掉到底。
    last = first + sum(1 for meta in report["buckets"] if not meta["future"]) - 1
    if last >= first:
        sheet.line_chart(area, (1, first, last), [(index + 2, header_row, last, colors[index])
                                                  for index in range(len(TIME_FIELDS))], y_title="轮次数")


def _agents_book(workbook, check, info, report, period):
    """坐席细化分三张数据表，各表列宽只为自己的表服务：
    坐席细化（概况 + 客服回复质量）、坐席回复询盘（坐席 × 时段）、可用客服量。"""
    summary, agents, buckets = report["presence_summary"], report["agents"], report["buckets"]
    seat = _seat_widths(agents)
    coverage = (min(100.0, summary["samples"] / summary["expected_samples"] * 100)
                if summary.get("expected_samples") else None)

    sheet = _Sheet(workbook, TITLES["agents"], seat + [11] * len(SEAT_FIELDS), check, freeze="C1")
    sheet.heading(TITLES["agents"], info)
    sheet.cards([("参与统计坐席", report["total"], "count"), ("回复询盘（按坐席去重）", report["total_inquiries"], "count"),
                 ("公开回复消息数", report["total_replies"], "count"), ("无ICS分配轮次", report["unassigned_inquiries"], "count"),
                 ("协助轮次（不计考核）", report["assisted_inquiries"], "count"), ("在线人数峰值", summary["peak_online"], "count"),
                 ("有采样的分钟数", summary["observed_minutes"], "count"), ("状态采样覆盖率", coverage, "rate")])
    sheet.section("客服回复质量")
    def quality_row(agent):
        return lambda extra: [sheet.cell(_seat_name(agent), "text", None, *extra), sheet.cell(_seat_id(agent), "text", None, *extra),
                              *[sheet.value(agent.get(key), kind, "number", *extra) for key, _, kind, _ in SEAT_FIELDS]]
    totals = {key: sum(int(a.get(key) or 0) for a in agents) for key, _, kind, _ in SEAT_FIELDS if kind == "count"}
    totals["consultation_seconds"] = sum(float(a.get("consultation_seconds") or 0) for a in agents)
    totals["first_response_seconds"] = _weighted(agents, "first_response_seconds", "first_response_samples")
    totals["average_response_seconds"] = _weighted(agents, "average_response_seconds", "response_samples")
    totals["average_rating"] = _weighted(agents, "average_rating", "rated_inquiries")
    first, last = sheet.table(["坐席", "来源 / 账号 / 坐席ID", *[label for _, label, _, _ in SEAT_FIELDS]],
        [quality_row(a) for a in agents], empty="当前时间范围没有坐席记录、公开人工回复、成功分配或已归属流单。",
        total=(lambda extra: [sheet.cell("合计", "text", None, *extra), sheet.cell(f"全部 {len(agents)} 位坐席", "text", None, *extra),
                              *[sheet.value(totals[key], kind, "number", *extra) for key, _, kind, _ in SEAT_FIELDS]]) if agents else None)
    sheet.highlight(first, last, 10)  # 低分数
    sheet.highlight(first, last, 11)  # 流单数
    if report.get("unassigned_lost_inquiries"):
        sheet.note(f'另有 {report["unassigned_lost_inquiries"]:,} 条无 ICS 分配坐席的流单（不计入任何坐席）。')
    if agents:
        sheet.note("合计：计数与咨询状态总时长为各坐席相加；首响、平均响应、平均评分按各坐席样本数加权，与页面口径一致。")

    # 坐席 × 时段：时段列按标签定窄列宽，整张表紧凑。
    work = _Sheet(workbook, AGENT_SHEETS[0], seat + [11] * 4 + [_fit([b["label"] for b in buckets], 6, 12)] * len(buckets),
                  check, freeze="C6")
    work.heading(TITLES["agents"], info)
    work.section("坐席回复询盘（按实际回复时间归入时段）")
    def workload_row(agent):
        sampled = any(agent.get(f"{s}_minutes", 0) for s in ("online", "busy", "offline", "unknown"))
        return lambda extra: [
            work.cell(_seat_name(agent), "text", None, *extra), work.cell(_seat_id(agent), "text", None, *extra),
            work.value(agent["inquiries"], "count", "number", *extra), work.value(agent["replies"], "count", "number", *extra),
            work.value(agent["online_minutes"] if sampled else None, "count", "number", *extra),
            work.value(agent["busy_minutes"] if sampled else None, "count", "number", *extra),
            *[work.value(None if meta["future"] else agent["buckets"].get(bucket["key"], 0), "count", "number", *extra)
              for bucket, meta in zip(period.buckets, buckets)]]
    sampled_agents = [a for a in agents if any(a.get(f"{s}_minutes", 0) for s in ("online", "busy", "offline", "unknown"))]
    def workload_total(extra):
        return [work.cell("合计", "text", None, *extra), work.cell(f"全部 {len(agents)} 位坐席", "text", None, *extra),
                work.value(sum(a["inquiries"] for a in agents), "count", "number", *extra),
                work.value(sum(a["replies"] for a in agents), "count", "number", *extra),
                work.value(sum(a["online_minutes"] for a in sampled_agents) if sampled_agents else None, "count", "number", *extra),
                work.value(sum(a["busy_minutes"] for a in sampled_agents) if sampled_agents else None, "count", "number", *extra),
                *[work.value(None if meta["future"] else sum(a["buckets"].get(bucket["key"], 0) for a in agents),
                             "count", "number", *extra) for bucket, meta in zip(period.buckets, buckets)]]
    first, last = work.table(["坐席", "来源 / 账号 / 坐席ID", "回复询盘合计", "公开回复数", "在线分钟", "忙碌分钟",
                              *[b["label"] for b in buckets]], [workload_row(a) for a in agents],
                             empty="当前时间范围没有坐席记录或公开人工回复。", total=workload_total if agents else None)
    work.heat(first, last, 7, 6 + len(buckets))
    work.note("回复询盘合计按坐席在整个周期内去重；同一轮跨时段回复会在各时段各计一次，所以各时段相加可能大于合计。")

    points = report["presence_points"]
    if period.mode == "day":
        # 按日：每分钟在线人数排成“分（行）× 时（列）”色阶矩阵，窄列一屏看完一天。
        grid: dict = {}
        for point in points:
            at = datetime.fromisoformat(point["key"])
            grid.setdefault(at.minute, {})[at.strftime("%H")] = None if point.get("future") else point.get("online")
        hours = sorted({h for minutes in grid.values() for h in minutes})
        presence = _Sheet(workbook, AGENT_SHEETS[1], [9] + [6] * len(hours), check, freeze="B6")
        presence.heading(TITLES["agents"], info)
        presence.section("可用客服量（每分钟在线人数：行为分钟、列为小时）")
        def presence_row(minute, by_hour):
            return lambda extra: [presence.cell(f"{minute:02d}分", "text", None, *extra),
                                  *[presence.cell(by_hour.get(h), "center", NUMBER_FORMATS["count"], *extra) for h in hours]]
        first, last = presence.table(["分 \\ 时", *[f"{h}时" for h in hours]], [presence_row(m, grid[m]) for m in sorted(grid)])
        presence.heat(first, last, 2, 1 + len(hours))
        presence.note("空白表示该分钟没有成功采样、已超出状态保留期或尚未到来；颜色越深在线人数越多。")
    else:
        # 按月/年/自定义：左表右图，图高与表高一致。
        labels = [p["label"] for p in points]
        widths = [_fit([*labels, "时段"], 10, 16, 3), 13, 2] + [10] * CHART_COLUMNS
        presence = _Sheet(workbook, AGENT_SHEETS[1], widths, check, freeze="A6")
        presence.heading(TITLES["agents"], info)
        presence.section("可用客服量（平均在线人数）")
        header_row = presence.row + 1
        rows = [lambda extra, p=p: [presence.cell(p["label"], "text", None, *extra),
                                    presence.value(None if p.get("future") else p.get("online"), "people", "number", *extra)]
                for p in points]
        first, last = presence.table(["时段", "平均在线人数"], rows)
        span = max(CHART_ROWS, presence.row - header_row + 1)
        while presence.row < header_row + span - 1:
            presence.blank(ROW_HEIGHT)
        last = first + sum(1 for p in points if not p.get("future")) - 1
        if last >= first:
            presence.line_chart((header_row, span), (1, first, last), [(2, header_row, last, "2F6BFF")], y_title="人数",
                                columns=(4, len(widths)), number_format="0.0")


def _quality_book(workbook, check, info, report):
    agents, ai = report["agents"], report["ai"]
    sheet = _Sheet(workbook, TITLES["quality"], _seat_widths(agents) + [12] * len(HUMAN_FIELDS), check, freeze="C1")
    sheet.heading(TITLES["quality"], info)
    sheet.cards([("人工回复询盘总量", report["total_inquiries"], "count"),
                 ("人工质检不合格量", report["total_failed_inquiries"], "count"),
                 ("人工不合格率", report["human_failure_rate"], "rate"),
                 ("所有对话轮数", ai["total_rounds"], "count"), ("AI质检不合格量", ai["failed_rounds"], "count"),
                 ("AI不合格率", ai["failure_rate"], "rate")])
    sheet.section("人工质检表")
    def row(agent):
        return lambda extra: [sheet.cell(_seat_name(agent), "text", None, *extra), sheet.cell(_seat_id(agent), "text", None, *extra),
                              *[sheet.value(agent.get(key), kind, "number", *extra) for key, _, kind, _ in HUMAN_FIELDS]]
    totals = {key: sum(int(a.get(key) or 0) for a in agents) for key, _, kind, _ in HUMAN_FIELDS if kind == "count"}
    totals["failure_rate"] = report["human_failure_rate"]
    first, last = sheet.table(["坐席", "来源 / 账号 / 坐席ID", *[label for _, label, _, _ in HUMAN_FIELDS]],
        [row(a) for a in agents], empty="当前时间范围没有坐席记录或询盘。",
        total=(lambda extra: [sheet.cell("合计", "text", None, *extra), sheet.cell(f"全部 {len(agents)} 位坐席", "text", None, *extra),
                              *[sheet.value(totals[key], kind, "number", *extra) for key, _, kind, _ in HUMAN_FIELDS]]) if agents else None)
    sheet.highlight(first, last, 7)
    sheet.highlight(first, last, 8)
    if agents:
        sheet.note("合计：计数为各坐席相加；不合格率 = 质检不合格量合计 ÷ 回复询盘总量合计，与页面概况一致。")
    sheet.gap()
    # AI 表与人工表同宽：统计周期占前两列（坐席、ID 列），六项指标对齐人工表的六列。
    sheet.section("AI质检报表（按轮次开始时间统计全部对话）")
    headers = ["统计周期", "", *[label for _, label, _ in AI_FIELDS]]
    header_row = sheet.append([sheet.cell(name, "header") for name in headers], sheet.header_height(headers))
    label = period_label(report)
    data_row = sheet.append([sheet.cell(label, "text"), sheet.cell(None, "text"),
                             *[sheet.value(ai.get(key), kind) for key, _, kind in AI_FIELDS]],
                            16 * sheet.lines(label, sum(sheet.widths[:2])) + 6)
    sheet.merge(header_row, 1, 2)
    sheet.merge(data_row, 1, 2)
    sheet.highlight(data_row, data_row, 7)
    sheet.highlight(data_row, data_row, 8)


def _users_book(workbook, check, info, report, names):
    users = report["users"]
    channel_names = lambda keys: "、".join(names.get(k, k) for k in keys)
    name = lambda u: u.get("display_name") or f'联系人 #{u["contact_id"]}'
    times = [_beijing_seconds(u[key]) or MISSING for u in users for key in ("first_assigned_at", "last_assigned_at")]
    widths = [8, _fit([name(u) for u in users] + ["用户"], 10, 28, 3), _fit([str(u["contact_id"]) for u in users], 10, 24),
              _fit([channel_names(u["channels"]) for u in users], 8, 24), 13, 14, 12, _fit(times, 12, 22), _fit(times, 12, 22), 10]
    sheet = _Sheet(workbook, TITLES["users"], widths, check, freeze="C1")
    sheet.heading(TITLES["users"], info)
    cards = [("成功转人工总次数", report["total_assignments"], "count"), ("有成功转人工的用户数", report["users_total"], "count"),
             ("前 100 名次数合计", report["ranked_assignments"], "count"), ("占总次数", report["ranked_share"], "rate")]
    if report.get("unidentified_assignments"):
        cards.append(("无法识别联系人（不参与排行）", report["unidentified_assignments"], "count"))
    sheet.cards(cards)
    sheet.section("用户成功转人工排行（前 100 名）")
    def row(user):
        blacklist = BLACKLIST_LABELS.get(user.get("blacklist_status"), "")
        flag = ("alert_text",) if user.get("blacklist_status") == "active" else ()
        return lambda extra: [
            sheet.value(user["rank"], "count", "center", *extra), sheet.cell(name(user), "text", None, *extra),
            sheet.cell(str(user["contact_id"]), "text", None, *extra), sheet.cell(channel_names(user["channels"]), "text", None, *extra),
            sheet.value(user["assignments"], "count", "number", *extra),
            sheet.value(user.get("daily_count"), "count", "number", *extra),
            sheet.value(user.get("weekly_count"), "count", "number", *extra),
            sheet.cell(_beijing_seconds(user["first_assigned_at"]) or MISSING, "center", None, *extra),
            sheet.cell(_beijing_seconds(user["last_assigned_at"]) or MISSING, "center", None, *extra),
            sheet.cell(blacklist, "center", None, *extra, *flag)]
    if report.get("before_earliest"):
        earliest = _beijing_seconds(report.get("earliest_assigned_at"))
        empty = (f"该时间之前无成功转人工记录（最早一条 ICS 成功分配记录：{earliest}）" if earliest
                 else "该时间之前无成功转人工记录（尚无任何 ICS 成功分配记录，请确认已开启分配同步 ARCHIVE_ASSIGNMENT_SYNC_ENABLED）")
    else:
        empty = "所选范围内没有成功转人工的用户。"
    first, last = sheet.table(["名次", "用户", "联系人ID", "渠道", "成功转人工次数", "近24小时计数", "当前周次计数", "区间内首次", "最近一次", "黑名单"],
                              [row(u) for u in users], empty=empty)
    sheet.bars(first, last, 5)
    if report.get("before_earliest") and users:
        sheet.note(f'所选区间早于最早一条 ICS 成功分配记录（{_beijing_seconds(report.get("earliest_assigned_at"))}），该时间之前无成功转人工记录。')


# ---------------------------------------------------------------- 口径说明
def _help_items(kind, report, names):
    """(标题, [(项目, 说明)]) 列表：数据提示、字段说明、统计规则。"""
    notices = []
    if kind == "time" and (report.get("processing") or {}).get("pending_jobs"):
        notices.append(("待处理任务", f'还有 {report["processing"]["pending_jobs"]:,} 条采集任务待处理，当前报表可能尚未包含最新数据。'))
    if kind == "agents" and report.get("retention_cutoff_at"):
        notices.append(("状态保留期", f'状态历史保留45天，当前可用起点为 {_beijing_seconds(report["retention_cutoff_at"])}。'
                                      "未监控、缺采样和已过期不补为离线或零；覆盖不足时，观测人数可能偏低。询盘归档不受45天保留期影响。"))
    if kind == "users":
        notices.append(("统计区间", f'{report["start_label"]} 至 {report["end_label"]}（北京时间，含起止）。'))
        counts = report.get("queue_counts") or {}
        if counts.get("available"):
            notices.append(("排队计数时刻", f'截至 {_beijing_seconds(counts["as_of"])}；当前周次从 '
                            f'{_beijing_seconds(counts["week_start"])} 起算（北京时间）。'))
        else:
            notices.append(("排队计数", "计数暂不可用，近24小时计数和当前周次计数显示为“—”。"))
    if kind == "time":
        fields = [(label, glossary.TIME_HELP[key]) for key, label in TIME_FIELDS]
        fields += [(f"{name}进客量", glossary.TIME_HELP["channel"]) for name in names.values()]
    elif kind == "agents":
        h = glossary.AGENT_HELP
        fields = [("参与统计坐席", h["seats"]), ("回复询盘（按坐席去重）", h["total_inquiries"]), ("公开回复消息数", h["replies"]),
                  ("无ICS分配轮次", h["unassigned_inquiries"]), ("协助轮次（不计考核）", h["assisted_inquiries"]),
                  ("在线人数峰值", h["peak_online"]), ("有采样的分钟数", h["observed_minutes"]), ("状态采样覆盖率", h["coverage"]),
                  *[(label, h[key]) for _, label, _, key in SEAT_FIELDS],
                  ("回复询盘合计", h["inquiries"]), ("公开回复数", h["replies"]), ("在线分钟", h["online_minutes"]),
                  ("忙碌分钟", h["busy_minutes"]), ("各时段回复询盘", h["bucket"]), ("可用客服量", h["online"])]
    elif kind == "quality":
        h = glossary.QUALITY_HELP
        fields = [("人工回复询盘总量", h["total_inquiries"]), ("人工质检不合格量", h["total_failed_inquiries"]),
                  ("人工不合格率", h["human_failure_rate"]), *[(label, h[key]) for _, label, _, key in HUMAN_FIELDS],
                  *[(label, h[f"ai_{key}"]) for key, label, _ in AI_FIELDS]]
    else:
        h = glossary.USER_HELP
        fields = [("成功转人工总次数", h["total_assignments"]), ("有成功转人工的用户数", h["users_total"]),
                  ("前 100 名次数合计", h["ranked_assignments"]), ("占总次数", h["ranked_share"]),
                  ("无法识别联系人", h["unidentified_assignments"]), ("名次", h["rank"]), ("用户", h["user"]),
                  ("成功转人工次数", h["assignments"]), ("近24小时计数", h["daily_count"]),
                  ("当前周次计数", h["weekly_count"]), ("区间内首次", h["first_assigned_at"]),
                  ("最近一次", h["last_assigned_at"]), ("黑名单", h["blacklist"])]
    rules = [(f"规则 {index}", text) for index, text in enumerate(glossary.RULES[kind], 1)]
    formats = [("数值格式", "时长为“时:分:秒”（Excel 时间值，可直接求和、排序）；比率为百分比；“—”表示没有数据或尚未到来的时段，真实 0 显示为 0。"),
               ("浅色标注", "红色底：低评分、流单、质检不合格等大于 0 的格子；橙色底：转人工拒接大于 0。蓝色深浅：数值越大颜色越深。只作提示，不改变数值。")]
    return [("数据提示", notices), ("字段说明", fields), ("统计规则", rules), ("阅读说明", formats)]


def _help_sheet(workbook, kind, report, names, check):
    sheet = _Sheet(workbook, HELP_SHEET, [26, 110], check)
    sheet.heading(f"{TITLES[kind]} · 口径说明", f"统计周期：{period_label(report)}{INFO_SEPARATOR}北京时间，起止均含")
    for title, items in _help_items(kind, report, names):
        if not items:
            continue
        sheet.section(title)
        for key, text in items:
            lines = max(sheet.lines(text, sheet.widths[1] - 2), sheet.lines(key, sheet.widths[0] - 2))
            sheet.append([sheet.cell(key, "help_key"), sheet.cell(text, "help_text")], min(409, 16 * lines + 6))
        sheet.gap()


# ---------------------------------------------------------------- 入口
def _channel_names(report, channels):
    names = dict(channels)
    for key in report.get("channels") or {}:
        names.setdefault(key, "未映射渠道" if key == "unmapped" else key)
    names.setdefault("unmapped", "未映射渠道")
    return names


def build_workbook(destination, kind, report, period, *, channels=(), channel_key=None, check=lambda: None):
    from openpyxl import Workbook
    from quality.query.excel import ExcelLimitExceeded, _dispose

    names = _channel_names(report, channels)
    table_names = dict(channels)
    if kind == "time":  # 页面列出已登记渠道与本期出现的渠道。
        for key in report.get("channels") or {}:
            table_names.setdefault(key, names[key])
    columns = {"time": 1 + len(TIME_FIELDS) + len(table_names), "agents": 6 + len(report.get("buckets") or ())}.get(kind, 0)
    if columns > 16_384:
        raise ExcelLimitExceeded("导出超过 Excel 列数上限，请缩小日期范围。")
    channel_key = channel_key or report.get("channel_key")
    channel_name = names.get(channel_key, channel_key) if channel_key else "全部渠道"
    info = _info(kind, report, channel_name)
    workbook = Workbook(write_only=True)
    try:
        if kind == "time":
            _time_book(workbook, check, info, report, period, table_names)
        elif kind == "agents":
            _agents_book(workbook, check, info, report, period)
        elif kind == "quality":
            _quality_book(workbook, check, info, report)
        else:
            _users_book(workbook, check, info, report, names)
        _help_sheet(workbook, kind, report, table_names if kind == "time" else names, check)
        workbook.save(destination)
    finally:
        _dispose(workbook)


def build_user_workbook(destination, report, *, channels=(), check=lambda: None):
    """用户统计：一个数据表（概况、前 100 名排行）+ 口径说明；不含逐条溯源明细（范围大时数据量过大）。"""
    build_workbook(destination, "users", report, None, channels=channels, check=check)


def create_export(pool, settings, kind, period, *, source_key=None, account_id=None,
                  channel_key=None, channels=(), cancelled=None):
    from quality.query.excel import ExcelLimitExceeded

    if kind not in TITLES:
        raise ValueError("Unsupported statistics export")
    if kind == "agents" and len(period.buckets) + 6 > 16_384:
        raise ExportTooLarge("导出超过 Excel 列数上限，请缩小日期范围。")
    deadline = time.monotonic() + settings.export_timeout_seconds
    def check():
        if cancelled is not None and cancelled.is_set():
            raise ExportCancelled()
        if time.monotonic() >= deadline:
            raise ExportTooLarge("导出用时过长，请缩小筛选范围后重试。")
    check()
    repository = StatisticsRepository(pool)
    budget = report_budget(seconds=deadline-time.monotonic(), abort_event=cancelled)
    scope = {"source_key": source_key, "account_id": account_id, "now": datetime.now(timezone.utc), "budget": budget}
    try:
        if kind == "time":
            report = repository.time_report(period, **scope, channel_key=channel_key)
        elif kind == "users":
            # 与页面同一快照规则：完整前 100 名排行，不导出逐条溯源明细。
            report = repository.user_report(period, **scope, channel_key=channel_key, full_ranking=True)
        else:
            # 一个查询快照覆盖全部坐席，独立于页面分页。
            report = getattr(repository, "agent_report" if kind == "agents" else "quality_report")(
                period, **scope, page=1, page_size=settings.export_max_entities)
            if report["total"] > settings.export_max_entities:
                raise ExportTooLarge("匹配坐席数量超过导出上限，请缩小筛选范围。")
    except Exception as error:
        reason = classify(budget, error)
        if reason == 'aborted':
            raise ExportCancelled() from None
        if reason in ('expired', 'statement'):
            raise ExportTooLarge("导出用时过长，请缩小筛选范围后重试。") from None
        raise
    check()
    descriptor, path = tempfile.mkstemp(prefix="ics-statistics-export-", suffix=".xlsx")
    os.close(descriptor)
    artifact = ExportFile(path, f'{TITLES[kind]}_{report["start_date"]}_{report["end_date"]}.xlsx')
    try:
        with open(path, "wb") as destination:
            build_workbook(destination, kind, report, period, channels=channels, channel_key=channel_key, check=check)
        check()
        if Path(path).stat().st_size > settings.export_max_bytes:
            raise ExportTooLarge("导出文件超过大小上限，请缩小筛选范围。")
        return artifact
    except ExcelLimitExceeded as error:
        artifact.cleanup()
        raise ExportTooLarge(str(error)) from None
    except BaseException:
        artifact.cleanup()
        raise
