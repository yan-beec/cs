"""Build an exact minute-by-minute presence timeline without external services."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


BEIJING = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc
MINUTE = timedelta(minutes=1)
MINUTES_PER_DAY = 1440
SAMPLE_STATUSES = ("online", "busy", "offline", "unknown")
TIMELINE_STATUSES = SAMPLE_STATUSES + ("missing", "not_monitored", "future", "expired")


def _utc(value, field, *, minute_aligned=False):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    value = value.astimezone(UTC)
    if minute_aligned and (value.second or value.microsecond):
        raise ValueError(f"{field} must be aligned to a UTC minute")
    return value


def build_timeline(day, now, periods, samples, retention_cutoff=None):
    """Return all 1440 minutes of a Beijing day, grouped by adjacent status.

    Membership periods include their start and exclude their end. A sample is
    evidence for its own minute only, including when a period is unavailable.
    The current unfinished minute and every later minute always remain future.
    If supplied, the retention cutoff hides completed older minutes as expired,
    including samples still physically present while bounded cleanup catches up.
    Timestamps in the result use Beijing time; minute 1440 is next-day midnight.
    """
    if type(day) is not date:
        raise ValueError("Presence timeline requires a date")
    now_utc = _utc(now, "now")
    start = datetime.combine(day, time(), BEIJING).astimezone(UTC)
    end = start + timedelta(minutes=MINUTES_PER_DAY)
    through = max(start, min(end, now_utc.replace(second=0, microsecond=0)))
    completed_minutes = (through - start) // MINUTE
    cutoff = None if retention_cutoff is None else _utc(
        retention_cutoff, "retention_cutoff", minute_aligned=True)

    # Union periods with a difference array, so overlapping periods never count
    # a minute twice and work remains bounded even with many period rows.
    membership = [0] * (MINUTES_PER_DAY + 1)
    for period in periods:
        period_start = _utc(period["started_minute"], "started_minute", minute_aligned=True)
        period_end = period["ended_minute"]
        if period_end is not None:
            period_end = _utc(period_end, "ended_minute", minute_aligned=True)
            if period_end < period_start:
                raise ValueError("ended_minute cannot precede started_minute")
        else:
            period_end = end
        first = max(start, period_start)
        last = min(through, period_end)
        if first < last:
            membership[(first - start) // MINUTE] += 1
            membership[(last - start) // MINUTE] -= 1

    minutes = ["future"] * MINUTES_PER_DAY
    active_periods = 0
    for index in range(completed_minutes):
        active_periods += membership[index]
        minutes[index] = "missing" if active_periods else "not_monitored"

    observed = {}
    for sample in samples:
        sampled = _utc(sample["sampled_minute"], "sampled_minute", minute_aligned=True)
        status = sample["availability_status"]
        if status not in SAMPLE_STATUSES:
            raise ValueError("availability_status must be online, busy, offline, or unknown")
        if not start <= sampled < end:
            continue
        index = (sampled - start) // MINUTE
        previous = observed.get(index)
        if previous is not None and previous != status:
            raise ValueError("Conflicting presence samples for the same minute")
        observed[index] = status
        if index < completed_minutes:
            minutes[index] = status

    if cutoff is not None:
        expired_minutes = max(0, min(completed_minutes, (cutoff - start) // MINUTE))
        minutes[:expired_minutes] = ["expired"] * expired_minutes

    def timestamp(index):
        return (start + index * MINUTE).astimezone(BEIJING).isoformat()

    status_minutes = dict.fromkeys(TIMELINE_STATUSES, 0)
    segments = []
    first = 0
    for last in range(1, MINUTES_PER_DAY + 1):
        if last < MINUTES_PER_DAY and minutes[last] == minutes[first]:
            continue
        duration = last - first
        status = minutes[first]
        status_minutes[status] += duration
        segments.append({
            "status": status,
            "start_minute": first,
            "end_minute": last,
            "start_at": timestamp(first),
            "end_at": timestamp(last),
            "duration_minutes": duration,
        })
        first = last

    return {
        "day": day.isoformat(),
        "timezone": "Asia/Shanghai",
        "start_at": timestamp(0),
        "end_at": timestamp(MINUTES_PER_DAY),
        "through_at": through.astimezone(BEIJING).isoformat(),
        "segments": segments,
        "status_minutes": status_minutes,
        "granularity_minutes": 1,
    }
