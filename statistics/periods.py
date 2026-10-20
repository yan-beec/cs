"""Calendar reporting windows, always in Beijing time and end-exclusive."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class ReportPeriod:
    mode: str
    selected: date
    start: datetime
    end: datetime
    grain: str
    buckets: tuple

    @classmethod
    def build(cls, mode: str, selected: date, end_date: date | None = None):
        if mode not in {"day", "month", "year", "custom"} or not 2000 <= selected.year <= 2099:
            raise ValueError("请选择2000至2099年内的日期。")
        if mode == "custom":
            if end_date is None:
                raise ValueError("自定义时间段需同时选择起始日期和结束日期。")
            if not 2000 <= end_date.year <= 2099:
                raise ValueError("请选择2000至2099年内的日期。")
            if end_date < selected:
                raise ValueError("结束日期不能早于起始日期。")
        elif end_date is not None:
            raise ValueError("起止日期仅用于自定义统计周期。")
        day = selected if mode in {"day", "custom"} else selected.replace(day=1) if mode == "month" else selected.replace(month=1, day=1)
        start = datetime.combine(day, time(), BEIJING)
        if mode == "custom":
            end, grain = datetime.combine(end_date + timedelta(days=1), time(), BEIJING), "day"
        elif mode == "day":
            end, grain = start + timedelta(days=1), "hour"
        elif mode == "month":
            end, grain = cls.next_month(start), "day"
        else:
            end, grain = start.replace(year=start.year + 1), "month"
        buckets = []
        cursor = start
        while cursor < end:
            following = cls.next_month(cursor) if grain == "month" else cursor + timedelta(days=1) if grain == "day" else cursor + timedelta(hours=1)
            label = cursor.strftime("%Y-%m-%d") if mode == "custom" else cursor.strftime("%H:00") if mode == "day" else f"{cursor.day}日" if mode == "month" else f"{cursor.month}月"
            buckets.append({"key": cursor.isoformat(), "label": label, "start": cursor, "end": following})
            cursor = following
        return cls(mode, day, start, end, grain, tuple(buckets))

    @staticmethod
    def next_month(value):
        return value.replace(year=value.year + 1, month=1) if value.month == 12 else value.replace(month=value.month + 1)

    def metadata(self, now):
        return {
            "mode": self.mode, "date": self.selected.isoformat(), "timezone": "Asia/Shanghai",
            "start_date": self.start.date().isoformat(), "end_date": (self.end - timedelta(days=1)).date().isoformat(),
            "start": self.start.isoformat(), "end": self.end.isoformat(), "grain": self.grain,
            "generated_at": now.astimezone(BEIJING).isoformat(),
            "buckets": [{"key": b["key"], "label": b["label"], "future": b["start"] > now,
                         "partial": b["start"] <= now < b["end"]} for b in self.buckets],
        }
