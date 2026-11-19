"""Calendar reporting windows, always in Beijing time and end-exclusive."""

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
MAX_CUSTOM_DAYS = 366


@dataclass(frozen=True)
class ReportPeriod:
    mode: str
    selected: date
    start: datetime
    end: datetime
    grain: str
    buckets: tuple
    # True after refine() changed the window: edge days are partial, so day rollups
    # and the d/m/y reply variants no longer apply (see repository.composer).
    refined: bool = False

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
            if (end_date - selected).days + 1 > MAX_CUSTOM_DAYS:
                raise ValueError("自定义时间段最多 366 天（含起止日期），请缩小范围。")
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

    def refine(self, start_time: time | None = None, end_time: time | None = None):
        """二级时刻（R10/R13）：开始日期的 start_time 起，到结束日期的 end_time 那一分钟末（含）。

        只用于按日与自定义；end_time 表示“含该分钟”，内部转为半开区间（+1 分钟）。
        时段分桶裁剪到新窗口，空桶去掉。
        """
        if start_time is None and end_time is None:
            return self
        if self.mode not in {"day", "custom"}:
            raise ValueError("指定时刻仅用于按日或自定义统计周期。")
        last_day = (self.end - timedelta(days=1)).date()
        start = datetime.combine(self.start.date(), start_time, BEIJING) if start_time else self.start
        end = (datetime.combine(last_day, end_time.replace(second=0, microsecond=0), BEIJING) + timedelta(minutes=1)
               if end_time else self.end)
        if end <= start:
            raise ValueError("结束时间必须晚于开始时间。")
        buckets = tuple({**b, "start": max(b["start"], start), "end": min(b["end"], end)}
                        for b in self.buckets if b["start"] < end and b["end"] > start)
        if (start, end) == (self.start, self.end):
            return self
        return replace(self, start=start, end=end, buckets=buckets, refined=True)

    @staticmethod
    def next_month(value):
        return value.replace(year=value.year + 1, month=1) if value.month == 12 else value.replace(month=value.month + 1)

    def metadata(self, now):
        return {
            "mode": self.mode, "date": self.selected.isoformat(), "timezone": "Asia/Shanghai",
            "start_date": self.start.date().isoformat(),
            "end_date": (self.end - timedelta(microseconds=1)).date().isoformat(),
            # 闭区间显示到秒（R10）：结束为半开边界前一秒。
            "start_label": self.start.strftime("%Y-%m-%d %H:%M:%S"),
            "end_label": (self.end - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "start": self.start.isoformat(), "end": self.end.isoformat(), "grain": self.grain,
            "refined": self.refined,
            "generated_at": now.astimezone(BEIJING).isoformat(),
            "buckets": [{"key": b["key"], "label": b["label"], "start": b["start"].isoformat(), "end": b["end"].isoformat(),
                         "future": b["start"] > now,
                         "partial": b["start"] <= now < b["end"]} for b in self.buckets],
        }
