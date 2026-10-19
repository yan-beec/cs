"""Presence durability, exact membership periods and permissions in a disposable DB."""

from datetime import date, datetime, timedelta, timezone
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import psycopg

from quality.presence.repository import PresenceRepository
from quality.tests import test_query_database as fixture


BASE = datetime(2026, 10, 15, 16, 0, tzinfo=timezone.utc)


@unittest.skipUnless(os.environ.get("PROCESSING_TEST_DATABASE") == "1", "Requires an isolated presence test database")
class PresenceDatabaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.QueryDatabaseTest.setUpClass.__func__(cls)
        cls.admin.execute("ALTER ROLE archive_presence LOGIN PASSWORD 'isolated-presence'")

    @classmethod
    def tearDownClass(cls):
        fixture.QueryDatabaseTest.tearDownClass.__func__(cls)

    def setUp(self):
        self.source = "presence-test-" + uuid.uuid4().hex
        self.writer = PresenceRepository(SimpleNamespace(connection_kwargs={**self.settings.connection_kwargs,
            "user": "archive_presence", "password": "isolated-presence"}))
        self.writer.open()
        self.addCleanup(self.writer.close)

    def append(self, minute, *agents, source=None, account=1, at=None):
        return self.writer.append({"status": "ready", "source_key": source or self.source, "account_id": account,
            "sampled_at": (at or BASE+timedelta(minutes=minute, seconds=10)).isoformat(),
            "agents": [{"agent_id": str(agent_id), "agent_name": name, "agent_name_source": "chatwoot",
                        "availability_status": status} for agent_id, name, status in agents]})

    def rows(self, table):
        from psycopg import sql
        return self.admin.execute(sql.SQL("SELECT * FROM {} WHERE source_key=%s ORDER BY agent_id").format(
            sql.Identifier(*table.split("."))), (self.source,)).fetchall()

    def test_sampling_deduplicates_original_minute_and_separates_unknown_from_missing(self):
        first = self.append(0, (1, "A", "online"))
        self.assertEqual(first["inserted_samples"], 1)
        duplicate = self.append(0, (2, "replacement must not apply", "offline"), at=BASE+timedelta(seconds=59))
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual([row["agent_id"] for row in duplicate["agents"]], ["1"])
        self.append(1, (1, "A renamed", "busy"), (2, "B", "offline"))
        self.append(2, (1, "A renamed", "unknown"), (2, "B", "offline"))
        ignored = self.append(0, (3, "old", "offline"))
        self.assertTrue(ignored["duplicate"])
        self.assertEqual(len(self.rows("archive.agent_presence_samples")), 5)
        result = self.writer.daily(date(2026, 10, 16), self.source, 1, BASE+timedelta(minutes=4, seconds=59))
        rows = {row["agent_id"]: row for row in result["agents"]}
        self.assertEqual([rows["1"][key] for key in ("expected_minutes", "sampled_minutes", "online_minutes",
            "busy_minutes", "offline_minutes", "unknown_minutes", "missing_minutes")], [4, 3, 1, 1, 0, 1, 1])
        self.assertEqual([rows["2"][key] for key in ("expected_minutes", "sampled_minutes", "offline_minutes", "missing_minutes")], [3, 2, 2, 1])
        self.assertEqual(datetime.fromisoformat(rows["1"]["first_seen_at"]), BASE+timedelta(seconds=10))
        self.assertEqual(datetime.fromisoformat(rows["1"]["last_sampled_at"]), BASE+timedelta(minutes=2, seconds=10))
        self.assertEqual(rows["1"]["agent_name_source"], "chatwoot")
        self.assertEqual(result["through_at"], (BASE+timedelta(minutes=4)).isoformat())
        self.assertEqual(result["status"], "ready")

    def test_empty_roster_removal_and_readdition_leave_previous_day_totals_unchanged(self):
        empty = self.append(1437)
        self.assertEqual(empty["inserted_samples"], 0)
        self.assertEqual(empty["agents"], [])
        self.append(1438, (1, "A", "online"))
        self.append(1439, (1, "A", "offline"))
        self.append(1440)
        now = BASE+timedelta(minutes=1502)
        before = self.writer.daily(date(2026, 10, 16), self.source, 1, now)["agents"][0]
        self.assertEqual((before["expected_minutes"], before["sampled_minutes"], before["missing_minutes"]), (2, 2, 0))
        removed = self.rows("ops.agent_presence_registry")[0]
        self.assertIsNotNone(removed["removed_at"])
        self.append(1500, (1, "A returned", "busy"))
        after = self.writer.daily(date(2026, 10, 16), self.source, 1, now)["agents"][0]
        for key in ("online_minutes", "busy_minutes", "offline_minutes", "unknown_minutes", "missing_minutes", "sampled_minutes", "expected_minutes", "last_sampled_at"):
            self.assertEqual(after[key], before[key], key)
        registry = self.rows("ops.agent_presence_registry")[0]
        self.assertEqual(registry["first_seen_at"], BASE+timedelta(minutes=1438, seconds=10))
        self.assertIsNone(registry["removed_at"])
        periods = sorted(self.rows("ops.agent_presence_periods"), key=lambda row: row["started_minute"])
        self.assertEqual(len(periods), 2)
        self.assertEqual(periods[0]["ended_minute"], BASE+timedelta(minutes=1440))
        self.assertIsNone(periods[1]["ended_minute"])
        current = self.writer.daily(date(2026, 10, 17), self.source, 1, now)["agents"][0]
        self.assertEqual((current["expected_minutes"], current["sampled_minutes"], current["missing_minutes"]), (2, 1, 1))

    def test_failed_transaction_rolls_back_monitor_sample_registry_and_period(self):
        with patch.object(self.writer, "_roster", side_effect=RuntimeError("injected before commit")):
            with self.assertRaises(RuntimeError):
                self.append(0, (1, "A", "online"))
        for table in ("archive.agent_presence_samples", "ops.agent_presence_registry", "ops.agent_presence_periods"):
            self.assertEqual(self.rows(table), [])
        self.assertEqual(self.admin.execute("SELECT count(*) AS n FROM ops.agent_presence_monitor WHERE source_key=%s", (self.source,)).fetchone()["n"], 0)
        self.assertEqual(self.append(0, (1, "A", "online"))["inserted_samples"], 1)

    def test_not_started_future_days_and_source_account_isolation(self):
        day = date(2026, 10, 16)
        now = BASE+timedelta(minutes=1)
        self.assertEqual(self.writer.daily(day, self.source, 1, now)["status"], "not_started")
        self.append(0, (1, "A", "online"))
        self.append(0, (1, "Other account", "offline"), account=2)
        self.append(0, (1, "Other source", "busy"), source=self.source+"-other")
        main = self.writer.daily(day, self.source, 1, now)["agents"][0]
        account = self.writer.daily(day, self.source, 2, now)["agents"][0]
        source = self.writer.daily(day, self.source+"-other", 1, now)["agents"][0]
        self.assertEqual((main["online_minutes"], main["offline_minutes"], main["busy_minutes"]), (1, 0, 0))
        self.assertEqual((account["online_minutes"], account["offline_minutes"], account["busy_minutes"]), (0, 1, 0))
        self.assertEqual((source["online_minutes"], source["offline_minutes"], source["busy_minutes"]), (0, 0, 1))
        future = self.writer.daily(date(2026, 10, 17), self.source, 1, now)
        self.assertEqual(future["agents"], [])
        self.assertEqual(future["agent_count"], 0)
        self.assertEqual(future["status"], "ready")

    def test_daily_caps_roster_with_full_count_including_removed_agents(self):
        self.append(0, *((index, f"Agent {index}", "online") for index in range(1, 201)))
        self.append(1, *((index, f"Agent {index}", "busy") for index in range(2, 202)))
        result = self.writer.daily(date(2026, 10, 16), self.source, 1, BASE+timedelta(minutes=2))
        self.assertEqual(result["agent_count"], 201)
        self.assertEqual(len(result["agents"]), 200)
        self.assertTrue(result["agents_truncated"])

    def test_roles_cannot_rewrite_samples_or_access_existing_chat_tables(self):
        self.append(0, (1, "A", "online"))
        with self.writer.pool.connection() as connection:
            for statement in ("SELECT * FROM archive.events LIMIT 0", "SELECT * FROM ops.round_index LIMIT 0",
                "UPDATE archive.agent_presence_samples SET agent_name='x' WHERE FALSE",
                "DELETE FROM archive.agent_presence_samples WHERE FALSE", "TRUNCATE archive.agent_presence_samples",
                "UPDATE ops.agent_presence_registry SET first_seen_at=first_seen_at WHERE FALSE",
                "UPDATE ops.agent_presence_periods SET started_minute=started_minute WHERE FALSE"):
                with self.subTest(statement=statement):
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        with connection.transaction():
                            connection.execute(statement)
        # Triggers protect new raw samples even from an owner accidentally issuing mutation SQL.
        for statement in ("UPDATE archive.agent_presence_samples SET agent_name='x' WHERE source_key=%s",
                          "DELETE FROM archive.agent_presence_samples WHERE source_key=%s",
                          "TRUNCATE archive.agent_presence_samples"):
            with self.subTest(owner_statement=statement):
                with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
                    with self.admin.transaction():
                        self.admin.execute("SET LOCAL ROLE archive_owner")
                        self.admin.execute(statement, (self.source,) if "%s" in statement else None)
        with self.admin.transaction():
            self.admin.execute("SET LOCAL ROLE archive_reader")
            self.assertEqual(self.admin.execute("SELECT count(*) AS n FROM archive.agent_presence_samples WHERE source_key=%s", (self.source,)).fetchone()["n"], 1)
        for role in ("archive_reader", "archive_ingest", "archive_quality"):
            with self.subTest(role=role):
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    with self.admin.transaction():
                        self.admin.execute("SET LOCAL ROLE " + role)
                        self.admin.execute("INSERT INTO archive.agent_presence_samples SELECT * FROM archive.agent_presence_samples WHERE FALSE")
        self.assertEqual(len(self.rows("archive.agent_presence_samples")), 1)

    def test_timeline_matches_daily_totals_and_keeps_scope_and_unfinished_minutes_separate(self):
        self.append(-1, (1, "A", "offline"))
        self.append(0, (1, "A", "online"))
        self.append(1, (1, "A", "busy"))
        self.append(2, (1, "A", "unknown"))
        self.append(0, (1, "Other account", "offline"), account=2)
        now = BASE+timedelta(minutes=4, seconds=59)
        result = self.writer.timeline(date(2026, 10, 16), self.source, 1, 1, now)
        daily = self.writer.daily(date(2026, 10, 16), self.source, 1, now)["agents"][0]
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["agent"]["agent_name"], "A")
        self.assertEqual(result["sample_count"], 3)
        self.assertEqual(result["status_minutes"], {"online": 1, "busy": 1, "offline": 0,
            "unknown": 1, "missing": 1, "not_monitored": 0, "future": 1436, "expired": 0})
        for status in ("online", "busy", "offline", "unknown", "missing"):
            self.assertEqual(result["status_minutes"][status], daily[status+"_minutes"])
        self.assertEqual(result["segments"][0]["start_at"], "2026-10-16T00:00:00+08:00")
        self.assertEqual(result["segments"][-1]["end_at"], "2026-10-17T00:00:00+08:00")
        self.assertEqual(result["segments"][-1]["end_minute"], 1440)
        other = self.writer.timeline(date(2026, 10, 16), self.source, 2, 1, now)
        self.assertEqual(other["status_minutes"]["offline"], 1)
        self.assertEqual(other["status_minutes"]["online"], 0)

    def test_timeline_labels_roster_absence_and_missing_agent_without_guessing_offline(self):
        day = date(2026, 10, 16)
        now = BASE+timedelta(minutes=4)
        self.assertEqual(self.writer.timeline(day, self.source, 1, 1, now)["status"], "not_started")
        self.append(0, (1, "A", "online"))
        self.append(1)
        self.append(2, (1, "A", "busy"))
        result = self.writer.timeline(day, self.source, 1, 1, now)
        self.assertEqual([(row["status"], row["start_minute"], row["end_minute"]) for row in result["segments"]],
            [("online", 0, 1), ("not_monitored", 1, 2), ("busy", 2, 3), ("missing", 3, 4), ("future", 4, 1440)])
        self.assertEqual(self.writer.timeline(day, self.source, 1, 999, now)["status"], "not_found")
        previous = self.writer.timeline(date(2026, 10, 15), self.source, 1, 1, now)
        self.assertEqual(previous["status_minutes"]["not_monitored"], 1440)
        self.assertEqual(previous["status_minutes"]["offline"], 0)
