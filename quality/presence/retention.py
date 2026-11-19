"""Fixed daily cutoff for 45 days of actual presence observations."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


BEIJING = ZoneInfo("Asia/Shanghai")
RETENTION_DAYS = 45
CLEANUP_HOUR = 4
CLEANUP_BATCH_SIZE = 2000
CLEANUP_MAX_BATCHES = 200


def retention_metadata(now: datetime) -> dict:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Presence retention requires a timezone-aware datetime")
    local_now = now.astimezone(BEIJING)
    anchor = local_now.replace(hour=CLEANUP_HOUR, minute=0, second=0, microsecond=0)
    if local_now < anchor:
        anchor -= timedelta(days=1)
    cutoff = anchor - timedelta(days=RETENTION_DAYS)
    return {
        "retention_days": RETENTION_DAYS,
        "oldest_available_day": cutoff.date().isoformat(),
        "cleanup_hour": CLEANUP_HOUR,
        "retention_cutoff_at": cutoff.astimezone(timezone.utc).isoformat(),
    }
