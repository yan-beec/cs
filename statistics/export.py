"""Single-sheet data exports using the existing bounded archive export worker."""

from datetime import datetime, timezone
from decimal import Decimal
import os
from pathlib import Path
import tempfile
import time

from quality.query.export import ExportCancelled, ExportFile, ExportTooLarge
from .repository import StatisticsRepository


TIME_FIELDS = (("incoming", "进客量"), ("ai_only", "纯AI接待量"), ("handoff", "转人工量"),
               ("rejected", "转人工拒接量"), ("effective_human", "有效人工量"), ("low_rating", "低评分数（低于4分）"))
SEAT_FIELDS = (("first_response_seconds", "首响时长（秒）"), ("average_response_seconds", "平均响应时长（秒）"),
               ("dialogues", "对话量"), ("high_quality_dialogues", "高质量对话量"),
               ("assigned_inquiries", "发配量"), ("inquiries", "回复询盘总量"), ("average_rating", "平均评分"),
               ("low_rating", "低分数（低于4分）"), ("lost_inquiries", "流单数（转人工后未回）"),
               ("consultation_seconds", "咨询状态总时长（秒）"))
HUMAN_FIELDS = (("assigned_inquiries", "发配量"), ("dialogues", "对话量"),
                ("high_quality_dialogues", "高质量对话量"), ("inquiries", "回复询盘总量"),
                ("failed_inquiries", "质检不合格量"), ("failure_rate", "不合格率（%）"))
AI_FIELDS = (("total_rounds", "所有对话轮数"), ("handoff", "转人工量"), ("ai_only", "纯AI回复量"),
             ("handoff_failed", "转人工失败量"), ("failed_rounds", "不合格量"), ("failure_rate", "不合格率（%）"))
IDENTITY = ("坐席", "来源", "账号ID", "坐席ID")
TITLES = {"time": "时间统计", "agents": "坐席细化", "quality": "质检统计"}


def identity(agent):
    return [agent.get("agent_name") or str(agent["agent_id"]), agent["source_key"],
            str(agent["account_id"]), str(agent["agent_id"])]


def interval(bucket):
    return f'{bucket["start"]:%Y-%m-%d %H:%M} — {bucket["end"]:%Y-%m-%d %H:%M}'


def data_tables(kind, report, period, channels):
    """Only field headers and data; separate page tables share one worksheet."""
    if kind == "time":
        names = dict(channels)
        for key in report["channels"]:
            names.setdefault(key, "未映射渠道" if key == "unmapped" else key)
        fields = [name for _, name in TIME_FIELDS]
        def rows():
            for bucket, meta, row in zip(period.buckets, report["buckets"], report["rows"]):
                values = [row.get(key, 0) for key, _ in TIME_FIELDS] + [row["channels"].get(key, 0) for key in names]
                yield [interval(bucket), *([None] * len(values) if meta["future"] else values)]
            yield ["合计", *[report["totals"][key] for key, _ in TIME_FIELDS],
                   *[report["channels"].get(key, 0) for key in names]]
        yield ["时段（北京时间）", *fields, *[f"{name}进客量" for name in names.values()]], rows()
        return

    fields = SEAT_FIELDS if kind == "agents" else HUMAN_FIELDS
    yield [*IDENTITY, *[name for _, name in fields]], (
        [*identity(agent), *[agent.get(key) for key, _ in fields]] for agent in report["agents"])
    if kind == "quality":
        label = f'{report["start_date"]} 至 {report["end_date"]}'
        yield ["统计周期", *[name for _, name in AI_FIELDS]], [[label, *[report["ai"].get(key) for key, _ in AI_FIELDS]]]
        return

    def workload():
        for agent in report["agents"]:
            sampled = any(agent.get(f"{status}_minutes", 0) for status in ("online", "busy", "offline", "unknown"))
            yield [*identity(agent), agent["inquiries"], agent["replies"],
                   agent["online_minutes"] if sampled else None, agent["busy_minutes"] if sampled else None,
                   *[None if meta["future"] else agent["buckets"].get(bucket["key"], 0)
                     for bucket, meta in zip(period.buckets, report["buckets"])]]
    yield [*IDENTITY, "回复询盘合计", "公开回复数", "在线分钟", "忙碌分钟",
           *[interval(b) for b in period.buckets]], workload()
    yield ["时间（北京时间）", "可用客服量"], (
        [datetime.fromisoformat(point["key"]).strftime("%Y-%m-%d %H:%M"), point["online"]]
        for point in report["presence_points"])


def build_workbook(destination, kind, report, period, *, channels=(), check=lambda: None):
    # Reuse archive export's literal-text cells, Excel limits, style and cleanup.
    from openpyxl import Workbook
    from quality.query.excel import ExcelLimitExceeded, _Table, _dispose

    tables = list(data_tables(kind, report, period, channels))
    width = max(len(headers) for headers, _ in tables)
    if width > 16_384:
        raise ExcelLimitExceeded("导出超过 Excel 列数上限，请缩小日期范围。")
    workbook = Workbook(write_only=True)
    try:
        table = _Table(workbook, TITLES[kind], [(name, 40 if i == 0 else 22)
                                             for i, name in enumerate(tables[0][0])], default_height=30)
        for index, (headers, rows) in enumerate(tables):
            if index:
                table.append([])
                table.append(headers, header=True)
            for row in rows:
                check()
                table.append([float(v) if isinstance(v, Decimal) else v for v in row])
        if len(tables) == 1:
            table.finish()
        workbook.save(destination)
    finally:
        _dispose(workbook)


def create_export(pool, settings, kind, period, *, source_key=None, account_id=None,
                  channel_key=None, channels=(), cancelled=None):
    from quality.query.excel import ExcelLimitExceeded

    if kind not in TITLES:
        raise ValueError("Unsupported statistics export")
    if kind == "agents" and len(period.buckets) + 8 > 16_384:
        raise ExportTooLarge("导出超过 Excel 列数上限，请缩小日期范围。")
    deadline = time.monotonic() + settings.export_timeout_seconds
    def check():
        if cancelled is not None and cancelled.is_set():
            raise ExportCancelled()
        if time.monotonic() >= deadline:
            raise ExportTooLarge("导出用时过长，请缩小筛选范围后重试。")
    check()
    repository = StatisticsRepository(pool)
    scope = {"source_key": source_key, "account_id": account_id, "now": datetime.now(timezone.utc)}
    if kind == "time":
        report = repository.time_report(period, **scope, channel_key=channel_key)
    else:
        # One query snapshot includes every seat, independent of UI pagination.
        report = getattr(repository, "agent_report" if kind == "agents" else "quality_report")(
            period, **scope, page=1, page_size=settings.export_max_entities)
        if report["total"] > settings.export_max_entities:
            raise ExportTooLarge("匹配坐席数量超过导出上限，请缩小筛选范围。")
    check()
    descriptor, path = tempfile.mkstemp(prefix="ics-statistics-export-", suffix=".xlsx")
    os.close(descriptor)
    artifact = ExportFile(path, f'{TITLES[kind]}_{report["start_date"]}_{report["end_date"]}.xlsx')
    try:
        with open(path, "wb") as destination:
            build_workbook(destination, kind, report, period, channels=channels, check=check)
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
