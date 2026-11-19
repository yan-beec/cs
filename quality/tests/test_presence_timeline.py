"""Exact Beijing-day timeline semantics with no database or running services."""

from collections import Counter
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

import pytest

from quality.presence.timeline import TIMELINE_STATUSES, build_timeline


DAY = date(2026, 10, 16)
START = datetime(2026, 10, 15, 16, tzinfo=timezone.utc)
MINUTE = timedelta(minutes=1)
END = START + timedelta(days=1)


def period(first, last=None):
    return {"started_minute": START + first * MINUTE,
            "ended_minute": None if last is None else START + last * MINUTE}


def sample(minute, status="online"):
    return {"sampled_minute": START + minute * MINUTE, "availability_status": status}


def expand(result):
    return [segment["status"] for segment in result["segments"]
            for _ in range(segment["duration_minutes"])]


def test_beijing_day_includes_utc_previous_date_and_excludes_next_day_boundary():
    result = build_timeline(DAY, END, [], [sample(-1, "busy"), sample(0),
                                         sample(1439, "offline"), sample(1440, "busy")])
    assert result["day"] == "2026-10-16"
    assert result["timezone"] == "Asia/Shanghai"
    assert result["start_at"] == "2026-10-16T00:00:00+08:00"
    assert result["end_at"] == "2026-10-17T00:00:00+08:00"
    assert result["through_at"] == result["end_at"]
    assert expand(result) == ["online"] + ["not_monitored"] * 1438 + ["offline"]


def test_last_minute_ends_at_next_midnight_and_is_one_completed_minute():
    result = build_timeline(DAY, END, [period(1439)], [sample(1439, "busy")])
    assert result["segments"][-1] == {
        "status": "busy", "start_minute": 1439, "end_minute": 1440,
        "start_at": "2026-10-16T23:59:00+08:00", "end_at": "2026-10-17T00:00:00+08:00",
        "duration_minutes": 1,
    }
    assert result["status_minutes"]["busy"] == 1


def test_samples_only_describe_their_own_minute_and_gaps_stay_missing():
    result = build_timeline(DAY, START + 6 * MINUTE, [period(0)],
                            [sample(0), sample(3, "offline")])
    assert expand(result)[:7] == ["online", "missing", "missing", "offline", "missing", "missing", "future"]
    assert result["status_minutes"]["offline"] == 1
    assert result["status_minutes"]["missing"] == 4


def test_observed_unknown_is_distinct_from_an_absent_sample():
    result = build_timeline(DAY, START + 3 * MINUTE, [period(0)], [sample(1, "unknown")])
    assert expand(result)[:3] == ["missing", "unknown", "missing"]
    assert result["status_minutes"]["unknown"] == 1
    assert result["status_minutes"]["missing"] == 2


def test_minutes_before_monitoring_enabled_are_not_monitored():
    result = build_timeline(DAY, START + 6 * MINUTE, [period(3)], [sample(4)])
    assert expand(result)[:7] == ["not_monitored"] * 3 + ["missing", "online", "missing", "future"]


def test_removed_and_readded_agent_preserves_unmonitored_gap_and_exclusive_end():
    result = build_timeline(DAY, START + 9 * MINUTE, [period(1, 3), period(5, 7)], [])
    assert expand(result)[:10] == ["not_monitored", "missing", "missing", "not_monitored",
                                 "not_monitored", "missing", "missing", "not_monitored",
                                 "not_monitored", "future"]
    assert result["status_minutes"]["missing"] == 4


@pytest.mark.parametrize("second,microsecond", [(0, 0), (59, 999999)])
def test_current_minute_and_later_minutes_remain_future_even_with_samples(second, microsecond):
    now = (START + 3 * MINUTE).replace(second=second, microsecond=microsecond)
    result = build_timeline(DAY, now, [period(0)], [sample(2), sample(3, "busy"), sample(4, "offline")])
    assert result["through_at"] == "2026-10-16T00:03:00+08:00"
    assert expand(result)[:5] == ["missing", "missing", "online", "future", "future"]
    assert result["status_minutes"]["busy"] == result["status_minutes"]["offline"] == 0


@pytest.mark.parametrize("now", [START - timedelta(days=2), START - timedelta(microseconds=1), START])
def test_days_that_have_not_started_are_entirely_future(now):
    result = build_timeline(DAY, now, [period(0)], [sample(0)])
    assert result["through_at"] == result["start_at"]
    assert result["status_minutes"]["future"] == 1440
    assert len(result["segments"]) == 1


def test_past_day_is_complete_and_open_period_is_clipped_to_day():
    result = build_timeline(DAY, END + timedelta(days=2), [period(-100)], [])
    assert result["through_at"] == result["end_at"]
    assert result["status_minutes"]["missing"] == 1440
    assert result["status_minutes"]["future"] == 0


def test_real_samples_override_absent_or_incorrect_membership_projection():
    result = build_timeline(DAY, START + 4 * MINUTE, [period(2, 3)],
                            [sample(0, "online"), sample(1, "busy"), sample(3, "offline")])
    assert expand(result)[:4] == ["online", "busy", "missing", "offline"]


def test_adjacent_equal_statuses_merge_across_observations_and_periods():
    result = build_timeline(DAY, START + 7 * MINUTE, [period(0, 4), period(4, 7)],
                            [sample(0), sample(1), sample(5, "busy"), sample(6, "busy")])
    assert [(row["status"], row["start_minute"], row["end_minute"])
            for row in result["segments"]] == [
                ("online", 0, 2), ("missing", 2, 5), ("busy", 5, 7), ("future", 7, 1440)]


def test_overlapping_periods_are_unioned_and_zero_length_or_outside_periods_are_empty():
    result = build_timeline(DAY, START + 8 * MINUTE,
                            [period(-5, 2), period(1, 4), period(3, 6), period(7, 7),
                             period(-3, -1), period(1440)], [])
    assert expand(result)[:8] == ["missing"] * 6 + ["not_monitored"] * 2
    assert result["status_minutes"]["missing"] == 6


def test_timestamps_from_different_timezones_are_compared_as_instants():
    offset = timezone(timedelta(hours=-5))
    records = [sample(0, "unknown"), sample(1)]
    for row in records:
        row["sampled_minute"] = row["sampled_minute"].astimezone(offset)
    periods = [period(0, 3)]
    for key in ("started_minute", "ended_minute"):
        periods[0][key] = periods[0][key].astimezone(timezone(timedelta(hours=8)))
    result = build_timeline(DAY, (START + 3 * MINUTE).astimezone(offset), periods, records)
    assert expand(result)[:4] == ["unknown", "online", "missing", "future"]


def test_every_minute_is_covered_exactly_once_and_counters_match_segments():
    records = [sample(index, ("online", "busy", "offline", "unknown")[index % 4])
               for index in range(0, 800, 3)]
    result = build_timeline(DAY, START + 1000 * MINUTE,
                            [period(-10, 200), period(400, 700), period(900)], records)
    cursor = 0
    counts = Counter()
    previous = None
    for segment in result["segments"]:
        assert segment["start_minute"] == cursor
        assert segment["status"] != previous
        cursor = segment["end_minute"]
        assert segment["duration_minutes"] == cursor - segment["start_minute"] > 0
        begin = datetime.fromisoformat(segment["start_at"])
        end = datetime.fromisoformat(segment["end_at"])
        assert end - begin == segment["duration_minutes"] * MINUTE
        assert begin.astimezone(timezone.utc) == START + segment["start_minute"] * MINUTE
        counts[segment["status"]] += segment["duration_minutes"]
        previous = segment["status"]
    assert cursor == 1440
    assert len(expand(result)) == 1440
    assert set(result["status_minutes"]) == set(TIMELINE_STATUSES)
    assert result["status_minutes"] == {status: counts[status] for status in TIMELINE_STATUSES}
    assert sum(result["status_minutes"].values()) == 1440
    assert result["granularity_minutes"] == 1


def test_input_rows_are_not_modified_and_identical_duplicate_samples_coalesce():
    periods = [period(0, 3)]
    records = [sample(1), sample(1)]
    original = deepcopy((periods, records))
    result = build_timeline(DAY, START + 3 * MINUTE, iter(periods), iter(records))
    assert (periods, records) == original
    assert result["status_minutes"]["online"] == 1


def test_conflicting_same_minute_samples_are_rejected_regardless_of_input_order():
    for records in ([sample(1), sample(1, "offline")], [sample(1, "offline"), sample(1)]):
        with pytest.raises(ValueError, match="Conflicting"):
            build_timeline(DAY, END, [], records)


@pytest.mark.parametrize("day", ["2026-10-16", START, None])
def test_day_must_be_a_date_without_time(day):
    with pytest.raises(ValueError, match="requires a date"):
        build_timeline(day, END, [], [])


@pytest.mark.parametrize("field", ["now", "started_minute", "ended_minute", "sampled_minute"])
def test_naive_timestamps_are_rejected_without_guessing_their_timezone(field):
    periods = [period(0, 3)]
    records = [sample(1)]
    now = END
    if field == "now":
        now = now.replace(tzinfo=None)
    elif field == "sampled_minute":
        records[0][field] = records[0][field].replace(tzinfo=None)
    else:
        periods[0][field] = periods[0][field].replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone-aware"):
        build_timeline(DAY, now, periods, records)


@pytest.mark.parametrize("field", ["started_minute", "ended_minute", "sampled_minute"])
def test_observation_and_membership_timestamps_must_be_minute_aligned(field):
    periods = [period(0, 3)]
    records = [sample(1)]
    row = records[0] if field == "sampled_minute" else periods[0]
    row[field] += timedelta(seconds=1)
    with pytest.raises(ValueError, match="aligned"):
        build_timeline(DAY, END, periods, records)


def test_reversed_period_is_rejected():
    with pytest.raises(ValueError, match="cannot precede"):
        build_timeline(DAY, END, [period(4, 2)], [])


@pytest.mark.parametrize("status", ["missing", "not_monitored", "future", "expired", "away", None])
def test_only_observed_presence_statuses_are_accepted_as_samples(status):
    with pytest.raises(ValueError, match="availability_status"):
        build_timeline(DAY, END, [], [sample(1, status)])


def test_oldest_retained_day_marks_only_minutes_before_four_expired():
    result = build_timeline(DAY, END, [period(0)],
                            [sample(239, "offline"), sample(240, "online"), sample(241, "unknown")],
                            retention_cutoff=START + 240 * MINUTE)
    assert expand(result)[:243] == ["expired"] * 240 + ["online", "unknown", "missing"]
    assert result["status_minutes"]["expired"] == 240
    assert result["status_minutes"]["offline"] == 0
    assert result["status_minutes"]["missing"] == 1198
    assert sum(result["status_minutes"].values()) == 1440
    assert result["segments"][0]["end_at"] == "2026-10-16T04:00:00+08:00"


@pytest.mark.parametrize("periods", [[], [period(300)]])
def test_expired_minutes_are_distinct_from_missing_and_unmonitored_minutes(periods):
    result = build_timeline(DAY, END, periods, [], retention_cutoff=START + 240 * MINUTE)
    assert expand(result)[:240] == ["expired"] * 240
    assert expand(result)[240] == "not_monitored"
    assert result["status_minutes"]["expired"] == 240


@pytest.mark.parametrize("cutoff,expired", [(START - MINUTE, 0), (START, 0), (END, 1440), (END + MINUTE, 1440)])
def test_retention_cutoff_is_clipped_to_the_requested_day(cutoff, expired):
    result = build_timeline(DAY, END, [period(0)], [], retention_cutoff=cutoff)
    assert result["status_minutes"]["expired"] == expired
    assert result["status_minutes"]["missing"] == 1440 - expired
    assert len(result["segments"]) == 1


def test_future_still_has_precedence_over_expiry_and_observed_samples():
    result = build_timeline(DAY, START + 2 * MINUTE, [period(0)], [sample(2, "offline")],
                            retention_cutoff=START + 3 * MINUTE)
    assert expand(result)[:4] == ["expired", "expired", "future", "future"]
    assert result["status_minutes"]["expired"] == 2
    assert result["status_minutes"]["future"] == 1438


def test_retention_cutoff_in_other_timezone_is_compared_as_an_instant():
    cutoff = (START + 240 * MINUTE).astimezone(timezone(timedelta(hours=-5)))
    result = build_timeline(DAY, END, [], [], retention_cutoff=cutoff)
    assert result["status_minutes"]["expired"] == 240
    assert result["segments"][1]["start_minute"] == 240


@pytest.mark.parametrize("cutoff", [START.replace(tzinfo=None), START + timedelta(seconds=1), "2026-10-16T04:00:00+08:00"])
def test_retention_cutoff_requires_an_aware_minute_boundary(cutoff):
    with pytest.raises(ValueError, match="retention_cutoff"):
        build_timeline(DAY, END, [], [], retention_cutoff=cutoff)
