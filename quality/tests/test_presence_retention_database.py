"""Retention and immutable-history safeguards in an explicitly disposable DB.

Apply migrations 001-006 first. Uses the same opt-in and disposable database
name checks as test_query_database; never runs against the live archive.
"""

from datetime import datetime, timedelta
import os
from types import SimpleNamespace
import unittest
import uuid

import psycopg

from quality.presence.repository import BEIJING, PresenceRepository
from quality.presence.retention import retention_metadata
from quality.tests import test_query_database as fixture


@unittest.skipUnless(os.environ.get("PROCESSING_TEST_DATABASE") == "1", "Requires an isolated presence test database")
class PresenceRetentionDatabaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.QueryDatabaseTest.setUpClass.__func__(cls)
        cls.admin.execute("ALTER ROLE archive_presence LOGIN PASSWORD 'isolated-presence'")

    @classmethod
    def tearDownClass(cls):
        fixture.QueryDatabaseTest.tearDownClass.__func__(cls)

    def setUp(self):
        self.source = "presence-retention-test-" + uuid.uuid4().hex
        clock = self.admin.execute("""SELECT CURRENT_TIMESTAMP AS now,
            local_now::date AS cleanup_day,
            ((local_now::date + TIME '04:00') - INTERVAL '45 days')
                AT TIME ZONE 'Asia/Shanghai' AS cutoff,
            local_now::time < TIME '04:00' AS waiting
            FROM (SELECT CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai' AS local_now) clock""").fetchone()
        self.now = clock["now"]
        self.cleanup_day = clock["cleanup_day"]
        self.cutoff = clock["cutoff"]
        self.waiting = clock["waiting"]
        self.writer = self.connect_writer()

    def connect_writer(self):
        connection = psycopg.connect(**{**self.settings.connection_kwargs,
            "user": "archive_presence", "password": "isolated-presence",
            "row_factory": psycopg.rows.dict_row,
            "options": "-c statement_timeout=3000 -c lock_timeout=500"}, autocommit=True)
        self.addCleanup(connection.close)
        return connection

    def require_cleanup_window(self):
        if self.waiting:
            self.skipTest("Database Beijing clock is before 04:00; cleanup must wait")

    def cleanup(self, source=None, account=1, *, connection=None):
        self.require_cleanup_window()
        return (connection or self.writer).execute("SELECT * FROM ops.cleanup_presence_history(%s,%s)",
            (source or self.source, account)).fetchone()

    def run_cleanup(self, source=None, account=1, *, connection=None):
        self.require_cleanup_window()
        return (connection or self.writer).execute("SELECT * FROM ops.run_presence_cleanup(%s,%s)",
            (source or self.source, account)).fetchone()

    def cleanup_state(self, *, source=None, account=1):
        return self.admin.execute("""SELECT * FROM ops.agent_presence_cleanup_state
            WHERE source_key=%s AND account_id=%s""", (source or self.source, account)).fetchone()

    def seed_cleanup_state(self, *, source=None, account=1, day=None, cutoff=None,
                           batches=199, status="pending", samples=398000, periods=398000):
        self.admin.execute("""INSERT INTO ops.agent_presence_cleanup_state
            (source_key,account_id,cleanup_day,cutoff_at,batches,status,deleted_samples,deleted_periods,last_success_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (source or self.source, account, day or self.cleanup_day, cutoff or self.cutoff,
             batches, status, samples, periods, self.now-timedelta(days=3)))

    def sample(self, minute, *, source=None, account=1, agent="1", sampled_at=None, status="online"):
        self.admin.execute("""INSERT INTO archive.agent_presence_samples
            (source_key,account_id,agent_id,sampled_minute,sampled_at,agent_name,agent_name_source,availability_status)
            VALUES(%s,%s,%s,%s,%s,'Agent','chatwoot',%s)""",
            (source or self.source, account, agent, minute,
             sampled_at if sampled_at is not None else minute+timedelta(seconds=10), status))

    def registry(self, *, source=None, account=1, agent="1"):
        self.admin.execute("""INSERT INTO ops.agent_presence_registry
            (source_key,account_id,agent_id,first_seen_at,last_seen_at,agent_name,agent_name_source)
            VALUES(%s,%s,%s,%s,%s,'Agent','chatwoot') ON CONFLICT DO NOTHING""",
            (source or self.source, account, agent, self.cutoff-timedelta(days=10), self.cutoff))

    def period(self, start, end, *, source=None, account=1, agent="1"):
        self.registry(source=source, account=account, agent=agent)
        self.admin.execute("""INSERT INTO ops.agent_presence_periods
            (source_key,account_id,agent_id,started_minute,ended_minute) VALUES(%s,%s,%s,%s,%s)""",
            (source or self.source, account, agent, start, end))

    def sample_minutes(self, *, source=None, account=1):
        return [row["sampled_minute"] for row in self.admin.execute("""SELECT sampled_minute
            FROM archive.agent_presence_samples WHERE source_key=%s AND account_id=%s ORDER BY sampled_minute""",
            (source or self.source, account)).fetchall()]

    def seed_expired_history(self, count=2001, *, source=None, account=1):
        source = source or self.source
        self.registry(source=source, account=account)
        self.admin.execute("""INSERT INTO archive.agent_presence_samples
            (source_key,account_id,agent_id,sampled_minute,sampled_at,agent_name_source,availability_status)
            SELECT %s,%s,'1',%s-(%s+1-n)*interval '1 minute',
                %s-(%s+1-n)*interval '1 minute'+interval '10 seconds','unobserved','unknown'
            FROM generate_series(1,%s) n""", (source, account, self.cutoff, count, self.cutoff, count, count))
        self.admin.execute("""INSERT INTO ops.agent_presence_periods
            (source_key,account_id,agent_id,started_minute,ended_minute)
            SELECT %s,%s,'1',%s-(2*(%s+1-n))*interval '1 minute',
                %s-(2*(%s+1-n)-1)*interval '1 minute' FROM generate_series(1,%s) n""",
            (source, account, self.cutoff, count, self.cutoff, count, count))

    def test_cutoff_preserves_45_days_from_0400_and_crossing_or_open_periods(self):
        old = self.cutoff-timedelta(minutes=1)
        retained = self.cutoff+timedelta(minutes=1)
        for minute in (old, self.cutoff, retained):
            self.sample(minute)
        self.period(old-timedelta(minutes=2), old)
        self.period(old-timedelta(minutes=1), self.cutoff)
        self.period(old, retained)
        self.period(self.cutoff, None)
        # A DST-observing caller timezone must not change Beijing's cutoff.
        self.writer.execute("SET TIME ZONE 'America/Los_Angeles'")
        self.assertEqual(self.cleanup(), {"deleted_samples": 1, "deleted_periods": 2})
        self.assertEqual(self.sample_minutes(), [self.cutoff, retained])
        periods = self.admin.execute("""SELECT started_minute,ended_minute FROM ops.agent_presence_periods
            WHERE source_key=%s ORDER BY started_minute""", (self.source,)).fetchall()
        self.assertEqual(periods, [{"started_minute": old, "ended_minute": retained},
            {"started_minute": self.cutoff, "ended_minute": None}])
        self.assertEqual(self.admin.execute("SELECT count(*) AS n FROM ops.agent_presence_registry WHERE source_key=%s",
            (self.source,)).fetchone()["n"], 1)
        self.assertEqual(self.cleanup(), {"deleted_samples": 0, "deleted_periods": 0})
        state = self.cleanup_state()
        self.assertEqual((state["cleanup_day"], state["cutoff_at"], state["batches"], state["status"]),
            (self.cleanup_day, self.cutoff, 1, "completed"))
        self.assertEqual((state["deleted_samples"], state["deleted_periods"]), (1, 2))

    def test_scope_isolation_preserves_other_accounts_sources_and_chat_evidence(self):
        old = self.cutoff-timedelta(minutes=1)
        for source, account in ((self.source, 1), (self.source, 2), (self.source+"-other", 1)):
            self.sample(old, source=source, account=account)
            self.period(old-timedelta(minutes=1), old, source=source, account=account)
        event = self.admin.execute("""INSERT INTO archive.events
            (source_key,account_id,event_key,event_type,ingestion_kind,raw_body,payload)
            VALUES(%s,1,'retention-evidence','message_created','webhook',convert_to('{}','UTF8'),'{}') RETURNING id""",
            (self.source,)).fetchone()["id"]
        self.assertEqual(self.cleanup(), {"deleted_samples": 1, "deleted_periods": 1})
        self.assertEqual(self.sample_minutes(account=2), [old])
        self.assertEqual(self.sample_minutes(source=self.source+"-other"), [old])
        self.assertEqual(self.admin.execute("SELECT count(*) AS n FROM ops.agent_presence_periods WHERE source_key=%s AND account_id=2",
            (self.source,)).fetchone()["n"], 1)
        self.assertEqual(self.admin.execute("SELECT raw_body FROM archive.events WHERE id=%s", (event,)).fetchone()["raw_body"], b"{}")
        self.assertIsNone(self.cleanup_state(account=2))
        self.assertIsNone(self.cleanup_state(source=self.source+"-other"))

    def test_each_call_is_bounded_to_2000_rows_per_table_and_can_drain_repeatedly(self):
        self.require_cleanup_window()
        self.seed_expired_history()
        self.assertEqual(self.cleanup(), {"deleted_samples": 2000, "deleted_periods": 2000})
        state = self.cleanup_state()
        self.assertEqual((state["batches"], state["status"]), (1, "pending"))
        self.assertEqual(len(self.sample_minutes()), 1)
        result = self.run_cleanup()
        self.assertEqual((result["deleted_samples"], result["deleted_periods"]), (1, 1))
        self.assertEqual((result["batches"], result["cleanup_status"]), (2, "completed"))
        self.assertEqual((result["total_deleted_samples"], result["total_deleted_periods"]), (2001, 2001))
        self.assertEqual(self.cleanup(), {"deleted_samples": 0, "deleted_periods": 0})
        self.assertEqual(self.cleanup_state()["batches"], 2)

    def test_runtime_still_cannot_mutate_samples_or_become_owner(self):
        old = self.cutoff-timedelta(minutes=1)
        self.sample(old)
        # A caller-defined GUC cannot confer retention or owner privileges.
        self.writer.execute("SELECT set_config('quality.presence_retention','on',false)")
        for statement in ("DELETE FROM archive.agent_presence_samples WHERE source_key=%s",
            "UPDATE archive.agent_presence_samples SET agent_name='rewritten' WHERE source_key=%s",
            "TRUNCATE archive.agent_presence_samples", "DELETE FROM ops.agent_presence_periods WHERE FALSE",
            "SET ROLE archive_owner", "SELECT * FROM archive.events LIMIT 0"):
            with self.subTest(statement=statement):
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    self.writer.execute(statement, (self.source,) if "%s" in statement else None)
        self.assertEqual(self.sample_minutes(), [old])
        self.assertEqual(self.cleanup()["deleted_samples"], 1)

    def test_owner_trigger_rejects_retained_delete_every_update_and_truncate(self):
        self.sample(self.cutoff-timedelta(minutes=1))
        retained_minute = self.now.replace(second=0, microsecond=0)
        self.sample(retained_minute, sampled_at=self.now)
        for statement in ("DELETE FROM archive.agent_presence_samples WHERE source_key=%s",
            "UPDATE archive.agent_presence_samples SET agent_name='rewritten' WHERE source_key=%s",
            "TRUNCATE archive.agent_presence_samples"):
            with self.subTest(statement=statement):
                with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
                    with self.admin.transaction(force_rollback=True):
                        self.admin.execute("SET LOCAL ROLE archive_owner")
                        self.admin.execute(statement, (self.source,) if "%s" in statement else None)
        self.assertEqual(self.sample_minutes(), [self.cutoff-timedelta(minutes=1), retained_minute])
        # An ordinary admin statement (current_user != owner) also cannot bypass
        # the trigger merely by setting the same kind of arbitrary flag.
        with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
            with self.admin.transaction():
                self.admin.execute("SELECT set_config('quality.presence_retention','on',true)")
                self.admin.execute("DELETE FROM archive.agent_presence_samples WHERE source_key=%s AND sampled_minute<%s",
                    (self.source, self.cutoff))

    def test_cleanup_is_owner_defined_with_safe_search_path_and_no_public_execute(self):
        for name in ("cleanup_presence_history", "run_presence_cleanup"):
            with self.subTest(function=name):
                function = self.admin.execute("""SELECT p.prosecdef,r.rolname,p.proconfig,
                    pg_get_function_identity_arguments(p.oid) AS arguments,
                    EXISTS(SELECT 1 FROM aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
                        WHERE a.grantee=0 AND a.privilege_type='EXECUTE') AS public_execute
                    FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner
                    WHERE p.oid=%s::regprocedure""", ("ops."+name+"(text,bigint)",)).fetchone()
                self.assertTrue(function["prosecdef"])
                self.assertEqual(function["rolname"], "archive_owner")
                self.assertIn("search_path=pg_catalog, pg_temp", function["proconfig"])
                self.assertEqual(function["arguments"], "p_source_key text, p_account_id bigint")
                self.assertFalse(function["public_execute"])
            for role in ("archive_reader", "archive_ingest", "archive_quality", "archive_assignment"):
                with self.subTest(function=name, role=role):
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        with self.admin.transaction(force_rollback=True):
                            self.admin.execute("SET LOCAL ROLE " + role)
                            self.admin.execute("SELECT * FROM ops."+name+"(%s,1)", (self.source,))

    def test_cleanup_rejects_invalid_scope_without_deleting_any_rows(self):
        old = self.cutoff-timedelta(minutes=1)
        self.sample(old)
        for name in ("cleanup_presence_history", "run_presence_cleanup"):
            for source, account in ((None, 1), ("", 1), (" padded ", 1), ("a"*129, 1),
                (self.source, None), (self.source, 0), (self.source, -1)):
                with self.subTest(function=name, source=source, account=account):
                    with self.assertRaises(psycopg.errors.InvalidParameterValue):
                        self.writer.execute("SELECT * FROM ops."+name+"(%s,%s)", (source, account))
        self.assertEqual(self.sample_minutes(), [old])
        self.assertIsNone(self.cleanup_state())

    def test_competing_scope_lock_requests_retry_instead_of_false_completion(self):
        self.require_cleanup_window()
        old = self.cutoff-timedelta(minutes=1)
        self.sample(old)
        with self.admin.transaction():
            self.admin.execute("SELECT pg_advisory_xact_lock(175608,hashtext(%s||':'||1::text))", (self.source,))
            with self.assertRaises(psycopg.errors.LockNotAvailable):
                self.cleanup()
            self.assertEqual(self.sample_minutes(), [old])
            self.assertIsNone(self.cleanup_state())
        self.assertEqual(self.cleanup(), {"deleted_samples": 1, "deleted_periods": 0})
        self.assertEqual(self.cleanup_state()["batches"], 1)

    def test_locked_expired_row_rolls_back_entire_batch_and_allows_later_retry(self):
        self.require_cleanup_window()
        old = self.cutoff-timedelta(minutes=1)
        for existing in (False, True):
            source = self.source+str(existing)
            self.sample(old, source=source)
            self.period(old-timedelta(minutes=1), old, source=source)
            if existing:
                self.seed_cleanup_state(source=source)
            before = self.cleanup_state(source=source)
            for table in ("archive.agent_presence_samples", "ops.agent_presence_periods"):
                with self.subTest(existing_ledger=existing, locked_table=table):
                    with self.admin.transaction():
                        self.admin.execute("SELECT * FROM " + table + " WHERE source_key=%s FOR UPDATE", (source,))
                        with self.assertRaises(psycopg.errors.LockNotAvailable):
                            self.cleanup(source=source)
                        # A period lock follows sample deletion: both that deletion
                        # and ledger creation/progress must roll back on timeout.
                        self.assertEqual(self.sample_minutes(source=source), [old])
                        self.assertEqual(self.admin.execute("SELECT count(*) AS n FROM ops.agent_presence_periods WHERE source_key=%s",
                            (source,)).fetchone()["n"], 1)
                        self.assertEqual(self.cleanup_state(source=source), before)
            self.assertEqual(self.cleanup(source=source), {"deleted_samples": 1, "deleted_periods": 1})
            state = self.cleanup_state(source=source)
            self.assertEqual((state["batches"], state["status"]), (200 if existing else 1, "completed"))
            self.assertEqual((state["deleted_samples"], state["deleted_periods"]),
                (398001, 398001) if existing else (1, 1))

    def test_0400_boundary_preserves_actual_observation_at_and_after_cutoff(self):
        self.require_cleanup_window()
        observations = [self.cutoff-timedelta(microseconds=1), self.cutoff,
            self.cutoff+timedelta(microseconds=1), self.cutoff+timedelta(seconds=59, microseconds=999999),
            self.cutoff+timedelta(minutes=1)]
        for number, observed_at in enumerate(observations, 1):
            self.sample(observed_at.replace(second=0, microsecond=0), agent=str(number), sampled_at=observed_at)
        result = self.run_cleanup()
        self.assertEqual((result["deleted_samples"], result["deleted_periods"]), (1, 0))
        self.assertEqual(result["cutoff_at"], self.cutoff)
        retained = self.admin.execute("""SELECT sampled_at FROM archive.agent_presence_samples
            WHERE source_key=%s ORDER BY sampled_at""", (self.source,)).fetchall()
        self.assertEqual([row["sampled_at"] for row in retained], observations[1:])

    def test_owner_protects_actual_45_elapsed_days_even_in_an_older_floor_minute(self):
        # CURRENT_TIMESTAMP stays fixed for this transaction, making the exact
        # observation boundary deterministic without changing the server clock.
        with self.admin.transaction():
            threshold = self.admin.execute("SELECT CURRENT_TIMESTAMP - INTERVAL '1080 hours' AS cutoff").fetchone()["cutoff"]
            if threshold.second == 0 and threshold.microsecond == 0:
                self.skipTest("Exact minute clock cannot distinguish observation and floor minute")
            observed_at = threshold
            minute = observed_at.replace(second=0, microsecond=0)
            self.assertLess(minute, threshold)
            self.sample(minute, sampled_at=observed_at)
            with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
                with self.admin.transaction(force_rollback=True):
                    self.admin.execute("SET LOCAL ROLE archive_owner")
                    self.admin.execute("DELETE FROM archive.agent_presence_samples WHERE source_key=%s", (self.source,))
            self.assertEqual(self.sample_minutes(), [minute])

    def test_missed_cleanup_days_catch_up_with_today_cutoff_and_reset_daily_totals(self):
        self.require_cleanup_window()
        self.seed_cleanup_state(day=self.cleanup_day-timedelta(days=3), cutoff=self.cutoff-timedelta(days=3),
            batches=200, status="limited", samples=400000, periods=400000)
        ancient = self.cutoff-timedelta(days=60)
        old = self.cutoff-timedelta(minutes=1)
        retained = self.cutoff+timedelta(minutes=1)
        for minute in (ancient, old, retained):
            self.sample(minute)
        self.period(ancient-timedelta(minutes=1), ancient)
        self.period(old-timedelta(minutes=1), old)
        self.period(old, retained)
        self.period(retained, None)
        result = self.run_cleanup()
        self.assertEqual((result["deleted_samples"], result["deleted_periods"]), (2, 2))
        self.assertEqual((result["cleanup_day"], result["cutoff_at"]), (self.cleanup_day, self.cutoff))
        self.assertEqual((result["batches"], result["cleanup_status"]), (1, "completed"))
        self.assertEqual((result["total_deleted_samples"], result["total_deleted_periods"]), (2, 2))
        self.assertGreaterEqual(result["last_success_at"], self.now)
        self.assertEqual(self.sample_minutes(), [retained])
        self.assertEqual(self.admin.execute("SELECT count(*) AS n FROM ops.agent_presence_periods WHERE source_key=%s",
            (self.source,)).fetchone()["n"], 2)

    def test_200_batch_budget_persists_across_connections_and_legacy_or_new_function(self):
        self.require_cleanup_window()
        for last_batch in ("legacy", "new"):
            with self.subTest(last_batch=last_batch):
                source = self.source+last_batch
                self.seed_expired_history(source=source)
                # 199 committed batches represents a prior process's work.
                self.seed_cleanup_state(source=source)
                connection = self.connect_writer()
                method = self.cleanup if last_batch == "legacy" else self.run_cleanup
                result = method(source=source, connection=connection)
                self.assertEqual((result["deleted_samples"], result["deleted_periods"]), (2000, 2000))
                connection.close()
                before = self.cleanup_state(source=source)
                self.assertEqual((before["batches"], before["status"]), (200, "limited"))
                self.assertEqual((before["deleted_samples"], before["deleted_periods"]), (400000, 400000))
                # A new connection and either function share that same budget.
                restarted = self.connect_writer()
                self.assertEqual(self.cleanup(source=source, connection=restarted),
                    {"deleted_samples": 0, "deleted_periods": 0})
                result = self.run_cleanup(source=source, connection=restarted)
                self.assertEqual((result["deleted_samples"], result["deleted_periods"]), (0, 0))
                self.assertEqual((result["batches"], result["cleanup_status"]), (200, "limited"))
                self.assertEqual(self.cleanup_state(source=source), before)
                self.assertEqual(len(self.sample_minutes(source=source)), 1)
                self.assertEqual(self.admin.execute("SELECT count(*) AS n FROM ops.agent_presence_periods WHERE source_key=%s",
                    (source,)).fetchone()["n"], 1)
                restarted.close()
        # Another scope still has its independent daily budget.
        result = self.run_cleanup(account=2)
        self.assertEqual((result["batches"], result["cleanup_status"]), (1, "completed"))

    def test_runtime_roles_cannot_write_cleanup_ledger_or_reset_budget(self):
        self.seed_cleanup_state()
        before = self.cleanup_state()
        statements = (
            "INSERT INTO ops.agent_presence_cleanup_state SELECT * FROM ops.agent_presence_cleanup_state WHERE FALSE",
            "UPDATE ops.agent_presence_cleanup_state SET batches=0 WHERE source_key=%s",
            "DELETE FROM ops.agent_presence_cleanup_state WHERE source_key=%s",
            "TRUNCATE ops.agent_presence_cleanup_state",
        )
        for role in ("archive_presence", "archive_reader", "archive_ingest", "archive_quality", "archive_assignment"):
            for statement in statements:
                with self.subTest(role=role, statement=statement):
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        with self.admin.transaction(force_rollback=True):
                            self.admin.execute("SET LOCAL ROLE "+role)
                            self.admin.execute(statement, (self.source,) if "%s" in statement else None)
        self.assertEqual(self.cleanup_state(), before)

    def test_before_0400_waits_without_deleting_or_changing_any_cleanup_ledger(self):
        if not self.waiting:
            self.skipTest("This direct database-clock guard runs only before Beijing 04:00")
        old = self.cutoff-timedelta(days=1)
        for existing in (False, True):
            with self.subTest(existing_ledger=existing):
                source = self.source+str(existing)
                self.sample(old, source=source)
                self.period(old-timedelta(minutes=1), old, source=source)
                if existing:
                    self.seed_cleanup_state(source=source, day=self.cleanup_day-timedelta(days=1),
                        cutoff=self.cutoff-timedelta(days=1), batches=200, status="limited")
                before = self.cleanup_state(source=source)
                result = self.writer.execute("SELECT * FROM ops.run_presence_cleanup(%s,1)", (source,)).fetchone()
                self.assertEqual((result["deleted_samples"], result["deleted_periods"], result["cleanup_status"]),
                    (0, 0, "waiting"))
                self.assertEqual((result["cleanup_day"], result["cutoff_at"]), (self.cleanup_day, self.cutoff))
                self.assertEqual(self.writer.execute("SELECT * FROM ops.cleanup_presence_history(%s,1)", (source,)).fetchone(),
                    {"deleted_samples": 0, "deleted_periods": 0})
                self.assertEqual(self.cleanup_state(source=source), before)
                self.assertEqual(self.sample_minutes(source=source), [old])
                self.assertEqual(self.admin.execute("SELECT count(*) AS n FROM ops.agent_presence_periods WHERE source_key=%s",
                    (source,)).fetchone()["n"], 1)

    def test_oldest_retained_day_daily_and_timeline_hide_uncleaned_first_240_minutes(self):
        cutoff = retention_metadata(self.now)["retention_cutoff_at"]
        cutoff = datetime.fromisoformat(cutoff)
        day = cutoff.astimezone(BEIJING).date()
        start = cutoff-timedelta(hours=4)
        self.admin.execute("""INSERT INTO ops.agent_presence_monitor
            (source_key,account_id,started_at) VALUES(%s,1,%s)""", (self.source, start))
        self.period(start, None)
        self.sample(cutoff-timedelta(minutes=1), status="offline")
        self.sample(cutoff, sampled_at=cutoff, status="online")
        self.sample(cutoff+timedelta(minutes=1), status="busy")
        repository = PresenceRepository(SimpleNamespace(connection_kwargs={**self.settings.connection_kwargs,
            "user": "archive_presence", "password": "isolated-presence"}))
        repository.open()
        self.addCleanup(repository.close)
        daily = repository.daily(day, self.source, 1, self.now)
        timeline = repository.timeline(day, self.source, 1, 1, self.now)
        self.assertEqual((daily["status"], timeline["status"]), ("ready", "ready"))
        self.assertEqual(daily["expired_minutes"], 240)
        self.assertEqual(len(daily["agents"]), 1)
        agent = daily["agents"][0]
        self.assertEqual((agent["expected_minutes"], agent["sampled_minutes"], agent["missing_minutes"]), (1200, 2, 1198))
        self.assertEqual(timeline["sample_count"], 2)
        self.assertEqual(timeline["status_minutes"]["expired"], 240)
        self.assertEqual((timeline["segments"][0]["status"], timeline["segments"][0]["start_minute"],
            timeline["segments"][0]["end_minute"]), ("expired", 0, 240))
        for status in ("online", "busy", "offline", "unknown", "missing"):
            self.assertEqual(timeline["status_minutes"][status], agent[status+"_minutes"], status)
        self.assertEqual(timeline["status_minutes"]["offline"], 0)
        # No physical cleanup was needed to enforce the same read boundary.
        self.assertEqual(self.sample_minutes(), [cutoff-timedelta(minutes=1), cutoff, cutoff+timedelta(minutes=1)])
        self.assertIsNone(self.cleanup_state())

    def test_cleanup_transaction_rolls_back_both_sample_and_period_deletion(self):
        self.require_cleanup_window()
        old = self.cutoff-timedelta(minutes=1)
        for existing in (False, True):
            for name in ("legacy", "new"):
                with self.subTest(existing_ledger=existing, function=name):
                    source = self.source+str(existing)+name
                    self.sample(old, source=source)
                    self.period(old-timedelta(minutes=1), old, source=source)
                    if existing:
                        self.seed_cleanup_state(source=source)
                    before = self.cleanup_state(source=source)
                    with self.assertRaisesRegex(RuntimeError, "injected rollback"):
                        with self.writer.transaction():
                            result = (self.cleanup if name == "legacy" else self.run_cleanup)(source=source)
                            self.assertEqual((result["deleted_samples"], result["deleted_periods"]), (1, 1))
                            raise RuntimeError("injected rollback")
                    self.assertEqual(self.sample_minutes(source=source), [old])
                    self.assertEqual(self.admin.execute("SELECT count(*) AS n FROM ops.agent_presence_periods WHERE source_key=%s",
                        (source,)).fetchone()["n"], 1)
                    self.assertEqual(self.cleanup_state(source=source), before)
                    self.assertEqual(self.cleanup(source=source), {"deleted_samples": 1, "deleted_periods": 1})
                    state = self.cleanup_state(source=source)
                    self.assertEqual(state["batches"], 200 if existing else 1)
                    self.assertEqual((state["deleted_samples"], state["deleted_periods"]),
                        (398001, 398001) if existing else (1, 1))


if __name__ == "__main__":
    unittest.main()
