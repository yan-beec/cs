"""Presence validation and UTC minute bounds without live services."""

from contextlib import nullcontext
from copy import deepcopy
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from quality.presence.repository import PresenceRepository, validate_snapshot


BASE = datetime(2026, 10, 15, 16, 0, 25, tzinfo=timezone.utc)


def snapshot(**changes):
    result = {"source_key": "chatwoot-test", "account_id": 1, "status": "ready", "sampled_at": BASE.isoformat(),
        "agents": [{"agent_id": "7", "agent_name": "Actual name", "agent_name_source": "chatwoot", "availability_status": "online"}]}
    result.update(changes)
    return result


def test_snapshot_retains_observation_time_scope_and_name_without_modifying_input():
    item = snapshot(sampled_at="2026-10-16T00:00:25+08:00")
    original = deepcopy(item)
    source, account, at, agents = validate_snapshot(item)
    assert (source, account, at) == ("chatwoot-test", 1, BASE)
    assert agents[0] == original["agents"][0]
    assert item == original


@pytest.mark.parametrize("changes", [
    {"status": "stale"}, {"status": "unavailable"}, {"source_key": ""}, {"source_key": " padded "},
    {"account_id": True}, {"account_id": 0}, {"sampled_at": "2026-10-16T00:00:00"},
    {"sampled_at": 1000}, {"agents": None}, {"agents": [{}] * 201},
    {"agents": [{"agent_id": "7"}, {"agent_id": 7}]}, {"agents": [{"agent_id": True}]},
    {"agents": [{"agent_id": "0"}]}, {"agents": [{"agent_id": "7", "account_id": 2}]},
    {"agents": [{"agent_id": "7", "source_key": "different"}]},
    {"agents": [{"agent_id": "7", "agent_name": "x" * 201}]},
    {"agents": [{"agent_id": "7", "availability_status": "unrecognized"}]},
    {"agents": [{"agent_id": "7", "agent_name_source": "inferred"}]},
])
def test_invalid_or_cached_presence_never_reaches_database(changes):
    repository = PresenceRepository.__new__(PresenceRepository)
    repository.pool = Mock()
    with pytest.raises(ValueError):
        repository.append(snapshot(**changes))
    repository.pool.connection.assert_not_called()


def test_successful_empty_roster_and_unknown_status_remain_valid_observations():
    assert validate_snapshot(snapshot(agents=[]))[3] == []
    row = validate_snapshot(snapshot(agents=[{"agent_id": 9}]))[3][0]
    assert row == {"agent_id": "9", "agent_name": None, "agent_name_source": "unobserved", "availability_status": "unknown"}


def test_presence_pool_has_independent_fixed_capacity_and_timeouts():
    settings = SimpleNamespace(connection_kwargs={"user": "archive_presence", "password": "test-password"})
    with patch("quality.presence.repository.ConnectionPool") as pool:
        PresenceRepository(settings)
    args = pool.call_args.kwargs
    assert (args["min_size"], args["max_size"], args["timeout"], args["open"]) == (0, 1, 1, False)
    assert args["kwargs"]["connect_timeout"] == 2
    assert args["kwargs"]["application_name"] == "ics-quality-presence"
    assert args["kwargs"]["options"] == "-c statement_timeout=3000 -c lock_timeout=500"


@pytest.mark.parametrize("day,through", [
    (date(2026, 10, 16), "2026-10-15T16:02:00+00:00"),
    (date(2026, 10, 15), "2026-10-15T16:00:00+00:00"),
    (date(2026, 10, 17), "2026-10-16T16:00:00+00:00"),
])
def test_daily_beijing_bounds_exclude_current_unfinished_minute_and_unknown_monitor(day, through):
    connection = Mock()
    connection.transaction.return_value = nullcontext()
    connection.execute.return_value.fetchone.return_value = None
    repository = PresenceRepository.__new__(PresenceRepository)
    repository.pool = Mock()
    repository.pool.connection.return_value = nullcontext(connection)
    result = repository.daily(day, "chatwoot-test", 1, datetime(2026, 10, 15, 16, 2, 59, tzinfo=timezone.utc))
    assert result["through_at"] == through
    assert result["status"] == "not_started" and result["monitoring_started_at"] is None
    assert result["timezone"] == "Asia/Shanghai" and result["day"] == day.isoformat()
    assert result["agents"] == [] and result["agent_count"] == 0 and not result["agents_truncated"]
