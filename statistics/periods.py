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
    def build(cls, mode: str, selected: date):
        if mode not in {"day", "month", "year"} or not 2000 <= selected.year <= 2099:
            raise ValueError("请选择2000至2099年内的日期。")
        day = selected if mode == "day" else selected.replace(day=1) if mode == "month" else selected.replace(month=1, day=1)
        start = datetime.combine(day, time(), BEIJING)
        if mode == "day":
            end, grain = start + timedelta(days=1), "hour"
        elif mode == "month":
            end, grain = cls.next_month(start), "day"
        else:
            end, grain = start.replace(year=start.year + 1), "month"
        buckets = []
        cursor = start
        while cursor < end:
            following = cls.next_month(cursor) if mode == "year" else cursor + timedelta(days=1) if mode == "month" else cursor + timedelta(hours=1)
            label = cursor.strftime("%H:00") if mode == "day" else f"{cursor.day}日" if mode == "month" else f"{cursor.month}月"
            buckets.append({"key": cursor.isoformat(), "label": label, "start": cursor, "end": following})
            cursor = following
        return cls(mode, day, start, end, grain, tuple(buckets))

    @staticmethod
    def next_month(value):
        return value.replace(year=value.year + 1, month=1) if value.month == 12 else value.replace(month=value.month + 1)

    def metadata(self, now):
        return {
            "mode": self.mode, "date": self.selected.isoformat(), "timezone": "Asia/Shanghai",
            "start": self.start.isoformat(), "end": self.end.isoformat(), "grain": self.grain,
            "generated_at": now.astimezone(BEIJING).isoformat(),
            "buckets": [{"key": b["key"], "label": b["label"], "future": b["start"] > now,
                         "partial": b["start"] <= now < b["end"]} for b in self.buckets],
        }
