"""Fixed calendar retention shared by presence history and maintenance."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


BEIJING = ZoneInfo("Asia/Shanghai")
RETENTION_DAYS = 45
CLEANUP_HOUR = 4
CLEANUP_BATCH_SIZE = 2000
CLEANUP_MAX_BATCHES = 200


def retention_metadata(now: datetime) -> dict:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Presence retention requires a timezone-aware datetime")
    today = now.astimezone(BEIJING).date()
    return {
        "retention_days": RETENTION_DAYS,
        "oldest_available_day": (today - timedelta(days=RETENTION_DAYS - 1)).isoformat(),
        "cleanup_hour": CLEANUP_HOUR,
    }
