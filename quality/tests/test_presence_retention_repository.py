"""Retention date boundaries and the restricted, bounded cleanup call."""

from contextlib import nullcontext
from datetime import date, datetime, timezone
from unittest.mock import Mock

import pytest

from quality.presence.repository import PresenceRepository
from quality.presence.retention import retention_metadata


def repository():
    connection = Mock()
    connection.transaction.return_value = nullcontext()
    instance = PresenceRepository.__new__(PresenceRepository)
    instance.pool = Mock()
    instance.pool.connection.return_value = nullcontext(connection)
    return instance, connection


@pytest.mark.parametrize("now,oldest,cutoff", [
    ("2026-10-16T00:00:00+08:00", "2026-08-31", "2026-08-30T20:00:00+00:00"),
    ("2026-10-15T16:00:00+00:00", "2026-08-31", "2026-08-30T20:00:00+00:00"),
    ("2026-10-15T15:59:59+00:00", "2026-08-31", "2026-08-30T20:00:00+00:00"),
    ("2026-10-16T03:59:59.999999+08:00", "2026-08-31", "2026-08-30T20:00:00+00:00"),
    ("2026-10-16T04:00:00+08:00", "2026-09-01", "2026-08-31T20:00:00+00:00"),
    ("2026-10-16T23:59:59+08:00", "2026-09-01", "2026-08-31T20:00:00+00:00"),
    ("2028-03-01T04:00:00+08:00", "2028-01-16", "2028-01-15T20:00:00+00:00"),
    ("2028-03-01T03:59:59+08:00", "2028-01-15", "2028-01-14T20:00:00+00:00"),
    ("2027-01-01T04:00:00+08:00", "2026-11-17", "2026-11-16T20:00:00+00:00"),
])
def test_retention_uses_latest_due_beijing_four_am_anchor_minus_45_days(now, oldest, cutoff):
    assert retention_metadata(datetime.fromisoformat(now)) == {
        "retention_days": 45, "oldest_available_day": oldest, "cleanup_hour": 4,
        "retention_cutoff_at": cutoff}


def test_retention_rejects_ambiguous_local_time():
    with pytest.raises(ValueError, match="timezone-aware"):
        retention_metadata(datetime(2026, 10, 16))


@pytest.mark.parametrize("method", ["daily", "timeline"])
def test_expired_history_never_queries_or_relabels_it_as_missing(method):
    instance, connection = repository()
    arguments = [date(2026, 8, 31), "local", 1]
    if method == "timeline":
        arguments.append(7)
    arguments.append(datetime(2026, 10, 15, 20, tzinfo=timezone.utc))
    result = getattr(instance, method)(*arguments)
    assert result["status"] == "expired"
    assert result["retention_days"] == 45 and result["oldest_available_day"] == "2026-09-01"
    assert result["retention_cutoff_at"] == "2026-08-31T20:00:00+00:00"
    assert not result.get("agents") and not result.get("segments")
    instance.pool.connection.assert_not_called()


@pytest.mark.parametrize("method", ["daily", "timeline"])
def test_first_partially_retained_day_remains_queryable(method):
    instance, connection = repository()
    connection.execute.return_value.fetchone.return_value = None
    arguments = [date(2026, 9, 1), "local", 1]
    if method == "timeline":
        arguments.append(7)
    arguments.append(datetime(2026, 10, 15, 20, tzinfo=timezone.utc))
    result = getattr(instance, method)(*arguments)
    assert result["status"] == "not_started"
    instance.pool.connection.assert_called_once()


def test_cleanup_only_calls_the_scoped_database_function_in_one_transaction():
    instance, connection = repository()
    cutoff = datetime(2026, 8, 31, 20, tzinfo=timezone.utc)
    row = {"deleted_samples": 2000, "deleted_periods": 3, "cleanup_status": "pending",
           "cleanup_day": date(2026, 10, 16), "batches": 2,
           "total_deleted_samples": 4000, "total_deleted_periods": 5,
           "cutoff_at": cutoff, "last_success_at": None}
    connection.execute.return_value.fetchone.return_value = row
    assert instance.cleanup("local", "1") == {**row, "cutoff_at": cutoff.isoformat()}
    connection.transaction.assert_called_once_with()
    connection.execute.assert_called_once_with(
        "SELECT * FROM ops.run_presence_cleanup(%s,%s)", ("local", 1))


@pytest.mark.parametrize("row", [None, {}, {"deleted_samples": -1, "deleted_periods": 0},
    {"deleted_samples": 0, "deleted_periods": 2001}, {"deleted_samples": True, "deleted_periods": 0}])
def test_cleanup_rejects_invalid_or_unbounded_database_results(row):
    instance, connection = repository()
    connection.execute.return_value.fetchone.return_value = row
    with pytest.raises(RuntimeError, match="bounded batch"):
        instance.cleanup("local", 1)


@pytest.mark.parametrize("source,account", [("", 1), (" padded ", 1), ("local", True), ("local", 0)])
def test_invalid_cleanup_scope_never_opens_connection(source, account):
    instance, connection = repository()
    with pytest.raises(ValueError):
        instance.cleanup(source, account)
    instance.pool.connection.assert_not_called()
